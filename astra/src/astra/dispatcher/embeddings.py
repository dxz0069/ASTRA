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
    """批量嵌入；不可用/失败返回 None（调用方降级为纯 token 打分）。"""
    if not texts:
        return []
    missing = [t for t in texts if t not in _vector_cache]
    if missing:
        vectors = (
            _embed_via_api(missing)
            if os.environ.get("ASTRA_EMBED_API_KEY")
            else _embed_via_local(missing) if os.environ.get("ASTRA_EMBED_MODEL")
            else None
        )
        if vectors is None or len(vectors) != len(missing):
            return None
        with _cache_lock:
            for text, vec in zip(missing, vectors):
                _vector_cache[text] = vec
    with _cache_lock:
        return [_vector_cache[t] for t in texts]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
