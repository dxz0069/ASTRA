from contextlib import asynccontextmanager
import os
import secrets as _secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from astra import __version__
from astra.server import db
from astra.server.routers import export, hints, intents, projects, settings

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


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if _AUTH_TOKEN and request.url.path.startswith("/api"):
        auth_header = request.headers.get("authorization", "")
        api_key_header = request.headers.get("x-api-key", "")
        provided = auth_header.removeprefix("Bearer ").strip() or api_key_header.strip()
        # 安全审计：constant-time 比较——普通 != 会泄露 token 长度/前缀（计时攻击）
        if not _secrets.compare_digest(provided, _AUTH_TOKEN):
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


@app.middleware("http")
async def body_size_limit_middleware(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > _MAX_BODY_SIZE:
        return JSONResponse(
            status_code=413,
            content={"detail": f"Request body too large (max {_MAX_BODY_SIZE // 1024}KB)"},
        )
    return await call_next(request)


app.include_router(settings.router)
app.include_router(projects.router)
app.include_router(hints.router)
app.include_router(intents.router)
app.include_router(export.router)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
