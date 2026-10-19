"""The outreach-mcp service: automation API without browser login.

    /api/v1/...              REST (JSON), OpenAPI at /api/v1/openapi.json
    /mcp                     MCP over Streamable HTTP
    /healthz                 unauthenticated health check

Shares the database and the pipeline with the app and worker containers."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .apiv1 import mcp_server, rest, security
from .apiv1.service import ApiError
from .db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("outreach-mcp")

_mcp_app = mcp_server.http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # refuse to start with a missing or short token instead of running unprotected
    security.check_config()
    init_db()
    async with mcp_server.mcp.session_manager.run():
        yield


app = FastAPI(
    title="Outreach API",
    version=__version__,
    description="Automatisierung ohne Browser-Login. Authorization: Bearer <token>. "
    "Schreibende Aufrufe liefern ohne confirm=true nur eine Vorschau.",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url="/api/v1/openapi.json",
)
# HTTPBearer only documents the scheme in OpenAPI; ApiGuard does the check
app.include_router(rest.router, prefix="/api/v1", dependencies=[Depends(HTTPBearer(auto_error=False))])


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok", "version": __version__}


@app.exception_handler(ApiError)
async def _api_error(request: Request, exc: ApiError):
    return JSONResponse(exc.body(), status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    details = [{"loc": list(e.get("loc", [])), "msg": str(e.get("msg", "")).removeprefix("Value error, ")} for e in exc.errors()]
    return JSONResponse({"error": {"code": "validation_error", "message": "Ungültige Eingabe", "details": details}}, status_code=422)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    return JSONResponse({"error": {"code": code, "message": str(exc.detail)}}, status_code=exc.status_code)


@app.middleware("http")
async def _headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


# /mcp is served by the MCP SDK's Starlette app; mounted last so the routes above win
app.mount("/", _mcp_app)
# outermost: HTTPS, IP allowlist, bearer token, rate limit
app.add_middleware(security.ApiGuard)

