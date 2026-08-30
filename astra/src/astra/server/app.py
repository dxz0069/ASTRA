from contextlib import asynccontextmanager
import os
import secrets as _secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from astra import __version__
from astra.server import db
from astra.server.routers import export, hints, projects, settings, steps

STATIC_DIR = Path(__file__).parent / "static"

# 安全审计 C1：API-Key 认证——ASTRA_AUTH_TOKEN 环境变量设置后全部 API 路由
# 必须携带 Authorization: Bearer <token> 或 X-API-Key: <token>。
# 未设置 = 本地开发模式（不鉴权），设置 = 生产模式（强制鉴权）。
_AUTH_TOKEN = os.environ.get("ASTRA_AUTH_TOKEN", "")

# 安全审计 H1：请求体大小限制（防 OOM）——uvicorn 默认不限制
_MAX_BODY_SIZE = int(os.environ.get("ASTRA_MAX_BODY_SIZE", str(2 * 1024 * 1024)))  # 2MB


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.configure(db.DEFAULT_DB)
    yield


app = FastAPI(
    title="ASTRA",
    description="Fact-graph based collaborative exploration protocol",
    version=__version__,
    lifespan=lifespan,
    # 安全审计 M6：生产环境禁用 API 文档端点（防止攻击面暴露）
    docs_url=None if os.environ.get("ASTRA_DISABLE_DOCS") == "1" else "/docs",
    redoc_url=None if os.environ.get("ASTRA_DISABLE_DOCS") == "1" else "/redoc",
    openapi_url=None if os.environ.get("ASTRA_DISABLE_DOCS") == "1" else "/openapi.json",
)


# 审计22轮：@app.middleware 后注册者在外层先执行——注册序即倒序执行序。
# 目标执行序（外→内）：auth → body 限制 → 安全头。auth 必须最外：
# 旧序 body 在 auth 之前，未认证请求也被完整读入最多 2MB 体才吃 401，
# 无凭证频率攻击白嫖内存/CPU。安全头最内层：出站方向仍给所有响应（含 401）加头。
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """审计修复（CWE-693）：补关键安全响应头。

    X-Frame-Options/X-Content-Type-Options/Referrer-Policy 全量附加；
    CSP 分三档：UI 页面（/）用允许自源资源的最小集（样式需 unsafe-inline——
    图标 sprite 的隐藏 style 属性在标记内）；/static 资源不加 CSP；
    其余 API 响应用最严格的 default-src 'none'。
    HSTS 仅在 TLS 下有意义，本地 http 部署不加。
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path == "/":
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'",
        )
    elif not request.url.path.startswith("/static"):
        response.headers.setdefault(
            "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
        )
    return response


@app.middleware("http")
async def body_size_limit_middleware(request: Request, call_next):
    # 审计修复①：畸形 content-length（非数字）原样 int() 会抛 500——改 400 拒收
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared = int(content_length)
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Malformed Content-Length"})
        if declared > _MAX_BODY_SIZE:
            return JSONResponse(
                status_code=413,
                content={"detail": f"Request body too large (max {_MAX_BODY_SIZE // 1024}KB)"},
            )
    # 审计修复②：chunked 传输无 content-length，原检查可绕过——实际读取时按上限
    # 截断（有界读入并缓存 _body，下游 json() 复用缓存，防绕过防 OOM）
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > _MAX_BODY_SIZE:
            return JSONResponse(
                status_code=413,
                content={"detail": f"Request body too large (max {_MAX_BODY_SIZE // 1024}KB)"},
            )
    request._body = body
    return await call_next(request)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # 审计修复（表述不符）：路由实际挂在根路径（/projects、/settings…），并无 /api
    # 前缀——原 startswith("/api") 判断永不命中，认证是死代码。改为 token 设置时
    # 保护全部路径（生产模式 docs 已禁用；/ 与 /static 属管理 UI，锁住符合预期）
    if _AUTH_TOKEN:
        auth_header = request.headers.get("authorization", "")
        api_key_header = request.headers.get("x-api-key", "")
        provided = auth_header.removeprefix("Bearer ").strip() or api_key_header.strip()
        # 安全审计：constant-time 比较——普通 != 会泄露 token 长度/前缀（计时攻击）
        if not _secrets.compare_digest(provided, _AUTH_TOKEN):
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


app.include_router(settings.router)
app.include_router(projects.router)
app.include_router(hints.router)
app.include_router(steps.router)
app.include_router(export.router)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
