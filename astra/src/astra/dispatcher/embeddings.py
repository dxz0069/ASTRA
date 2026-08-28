from __future__ import annotations

"""语义嵌入层——星尘记忆的语义召回引擎（可选组件）。

纯 token 重叠打分召回不了同义表述（“SQL 注入的教训”召回不了 "MySQL 注入”）。
本模块在环境变量显式开启时提供文本向量，供 context.py 做混合相关度打分；
任何失败（未配置/网络异常/依赖缺失）都静默降级为 None，调用方回退纯 token 打分。

两种接入方式（优先级从高到低）：
1. ASTRA_EMBED_API_KEY：OpenAI 兼容 /embeddings 端点
   （可选 ASTRA_EMBED_API_URL，默认智谱开放平台；ASTRA_EMBED_MODEL 默认 embedding-3）；
2. ASTRA_EMBED_MODEL 指向本地 sentence-transformers 模型名（如 bge-small-zh-v1.5）。

进程内按文本内容做向量缓存；同批请求一次 API 调用。
"""

import json
import os
import threading
from urllib import request as _urlrequest

_DEFAULT_API_URL = "https://open.bigmodel.cn/api/paas/v4/embeddings"
_cache_lock = threading.Lock()
_vector_cache: dict[str, list[float]] = {}
_CACHE_MAX = 8192  # 审计修复②：有界缓存（条），超限整体重建
_api_failure_count = 0
_MAX_API_FAILURES = 3  # 连续失败后熔断，本进程不再尝试（避免拖慢调度循环）


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


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """批量嵌入；不可用/失败返回 None（调用方降级为纯 token 打分）。

    审计修复①：禁用判定前移——未配置嵌入源时即使缓存命中也返回 None（原实现
    缓存全命中会绕过禁用语义，行为随进程历史漂移=不可复现）。
    审计修复②：缓存有界（_CACHE_MAX 条，超限清空重建）——全局字典无界增长在
    6h 高负载跑分中吃内存。
    审计修复③：远端失败时已缓存文本仍可用（部分可用优于全量弃用）。
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
        vectors = _embed_via_api(missing) if api_key else _embed_via_local(missing)
        if vectors is not None and len(vectors) == len(missing):
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
