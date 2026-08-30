from __future__ import annotations

"""语义嵌入层——星尘记忆的语义召回引擎（可选组件）。

纯 token 重叠打分召回不了同义表述（“SQL 注入的教训”召回不了 "MySQL 注入"）。
本模块在环境变量显式开启时提供文本向量，供 context.py 做混合相关度打分；
任何失败（未配置/网络异常/依赖缺失）都静默降级为 None，调用方回退纯 token 打分。

两种接入方式（优先级从高到低）：
1. ASTRA_EMBED_API_KEY：OpenAI 兼容 /embeddings 端点
   （可选 ASTRA_EMBED_API_URL，默认智谱开放平台；ASTRA_EMBED_MODEL 默认 embedding-3）；
2. ASTRA_EMBED_MODEL 指向本地 sentence-transformers 模型名（如 bge-small-zh-v1.5）。

持久化（ASTRA_EMBED_PERSIST，默认开）：向量按文本 sha1 落 SQLite sidecar
（ASTRA_VECTOR_CACHE，默认 ~/.local/share/astra/vector-cache.sqlite3），进程重启后
免重算——语义召回从"进程缓存重启即失"升级为跨会话复用。float32 BLOB 存储，
仅查询行进内存；规模有界（超上限删最旧）；文件损坏自愈（删除重建）。
"""

import hashlib
import json
import os
import sqlite3
import threading
from array import array
from pathlib import Path
from urllib import request as _urlrequest

_DEFAULT_API_URL = "https://open.bigmodel.cn/api/paas/v4/embeddings"
_cache_lock = threading.Lock()
_vector_cache: dict[str, list[float]] = {}
_CACHE_MAX = 8192  # 审计修复②：有界缓存（条），超限整体重建
_api_failure_count = 0
_MAX_API_FAILURES = 3  # 连续失败后熔断，本进程不再尝试（避免拖慢调度循环）

# ---------------- 持久向量缓存（SQLite sidecar） ----------------

_PERSIST_DIR = Path.home() / ".local" / "share" / "astra"
_PERSIST_MAX_ROWS = 20000  # 磁盘缓存上限（条）——超限删最旧（遗忘式淘汰）
_COMPACT_EVERY = 500  # 每累计写入 N 条检查一次规模
_disk_conn: sqlite3.Connection | None = None
_disk_writes = 0


def embeddings_enabled() -> bool:
    return bool(os.environ.get("ASTRA_EMBED_API_KEY") or (
        os.environ.get("ASTRA_EMBED_MODEL") and not os.environ.get("ASTRA_EMBED_API_KEY")
    ))


def _embed_via_api(texts: list[str]) -> list[list[float]] | None:
    global _api_failure_count
    if _api_failure_count >= _MAX_API_FAILURES:
        return None
    api_key = os.environ.get("ASTRA_EMBED_API_KEY", "")
    url = os.environ.get("ASTRA_EMBED_API_URL", _DEFAULT_API_URL)
    model = os.environ.get("ASTRA_EMBED_MODEL", "embedding-3")
    payload = json.dumps({"model": model, "input": texts}).encode("utf-8")
    req = _urlrequest.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with _urlrequest.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        vectors = [item["embedding"] for item in data["data"]]
        _api_failure_count = 0
        return vectors
    except Exception:  # noqa: BLE001 - 任何网络/协议错误都降级
        _api_failure_count += 1
        return None


def _embed_via_local(texts: list[str]) -> list[list[float]] | None:
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError:
        return None
    model_name = os.environ.get("ASTRA_EMBED_MODEL", "bge-small-zh-v1.5")
    try:
        model = SentenceTransformer(model_name)
        return [vec.tolist() for vec in model.encode(texts, show_progress_bar=False)]
    except Exception:  # noqa: BLE001
        return None


# ---------------- 持久化实现 ----------------

def _persist_enabled() -> bool:
    return os.environ.get("ASTRA_EMBED_PERSIST", "1") not in ("0", "false", "no")


def _persist_path() -> Path:
    env_path = os.environ.get("ASTRA_VECTOR_CACHE")
    return Path(env_path) if env_path else _PERSIST_DIR / "vector-cache.sqlite3"


def _text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _disk_connection() -> sqlite3.Connection | None:
    """惰性打开持久缓存连接（调用方须已持 _cache_lock）；损坏文件自愈删除重建。"""
    global _disk_conn
    if _disk_conn is not None:
        return _disk_conn
    if not _persist_enabled():
        return None
    path = _persist_path()

    def _open() -> sqlite3.Connection:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.execute("CREATE TABLE IF NOT EXISTS vecs (h TEXT PRIMARY KEY, v BLOB)")
        return conn

    try:
        _disk_conn = _open()
    except (sqlite3.Error, OSError):
        try:
            path.unlink(missing_ok=True)  # 损坏自愈：删除重建空库
            _disk_conn = _open()
        except (sqlite3.Error, OSError):
            return None
    return _disk_conn


def _decode_blob(blob: bytes) -> list[float] | None:
    if not blob or len(blob) % 4:
        return None
    floats = array("f")
    try:
        floats.frombytes(blob)
    except ValueError:
        return None
    return floats.tolist()


def _disk_lookup(texts: list[str]) -> dict[str, list[float]]:
    """按文本哈希批量查持久缓存；不可用/异常一律返回空（纯降级，绝不抛）。"""
    with _cache_lock:
        conn = _disk_connection()
        if conn is None:
            return {}
        by_hash: dict[str, list[str]] = {}
        for t in texts:
            by_hash.setdefault(_text_hash(t), []).append(t)
        hits: dict[str, list[float]] = {}
        try:
            hashes = list(by_hash)
            for i in range(0, len(hashes), 128):
                chunk = hashes[i : i + 128]
                marks = ",".join("?" * len(chunk))
                for h, blob in conn.execute(
                    f"SELECT h, v FROM vecs WHERE h IN ({marks})", chunk
                ):
                    vec = _decode_blob(blob)
                    if vec is None:
                        continue
                    for t in by_hash.get(h, []):
                        hits[t] = vec
        except sqlite3.Error:
            return {}
    return hits


def _disk_store(pairs: list[tuple[str, list[float]]]) -> None:
    """新向量落盘（INSERT OR REPLACE，同文本幂等覆盖）；周期检查规模上限。"""
    global _disk_writes
    with _cache_lock:
        conn = _disk_connection()
        if conn is None:
            return
        try:
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO vecs (h, v) VALUES (?, ?)",
                    [(_text_hash(t), array("f", vec).tobytes()) for t, vec in pairs],
                )
            _disk_writes += len(pairs)
            if _disk_writes >= _COMPACT_EVERY:
                _disk_writes = 0
                count = conn.execute("SELECT COUNT(*) FROM vecs").fetchone()[0]
                if count > _PERSIST_MAX_ROWS:
                    with conn:
                        conn.execute(
                            "DELETE FROM vecs WHERE rowid NOT IN "
                            "(SELECT rowid FROM vecs ORDER BY rowid DESC LIMIT ?)",
                            (_PERSIST_MAX_ROWS,),
                        )
        except sqlite3.Error:
            pass  # 落盘失败不影响本次嵌入结果


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """批量嵌入；不可用/失败返回 None（调用方降级为纯 token 打分）。

    审计修复①：禁用判定前移——未配置嵌入源时即使缓存命中也返回 None（原实现
    缓存全命中会绕过禁用语义，行为随进程历史漂移=不可复现）。
    审计修复②：缓存有界（_CACHE_MAX 条，超限清空重建）——全局字典无界增长在
    6h 高负载跑分中吃内存。
    审计修复③：远端失败时已缓存文本仍可用（部分可用优于全量弃用）；
    持久缓存把该语义扩展到跨进程（重启后磁盘命中免重算）。
    """
    if not texts:
        return []
    api_key = os.environ.get("ASTRA_EMBED_API_KEY")
    local_model = os.environ.get("ASTRA_EMBED_MODEL")
    if not api_key and not local_model:
        return None  # 未配置嵌入源：恒定降级，与进程历史无关
    with _cache_lock:
        cached = {t: _vector_cache[t] for t in texts if t in _vector_cache}
    missing = [t for t in texts if t not in cached]
    if missing:
        disk_hits = _disk_lookup(missing)
        if disk_hits:
            with _cache_lock:
                if len(_vector_cache) + len(disk_hits) > _CACHE_MAX:
                    _vector_cache.clear()
                _vector_cache.update(disk_hits)
            cached.update(disk_hits)
            missing = [t for t in texts if t not in cached]
    if missing:
        vectors = _embed_via_api(missing) if api_key else _embed_via_local(missing)
        if vectors is not None and len(vectors) == len(missing):
            _disk_store(list(zip(missing, vectors)))
            with _cache_lock:
                if len(_vector_cache) + len(missing) > _CACHE_MAX:
                    _vector_cache.clear()
                for text, vec in zip(missing, vectors):
                    _vector_cache[text] = vec
                cached.update({t: _vector_cache[t] for t in missing})
    if any(t not in cached for t in texts):
        return None
    return [cached[t] for t in texts]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
