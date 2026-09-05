"""Assembly. The only place that knows all the pieces exist."""
from __future__ import annotations

import contextlib
import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import Config, ROOT
from ..host import Host
from . import assets, browse, exports, routes, ws

WEB = ROOT / "web"


def _quiet_healthchecks() -> None:
    """Keep `GET /health` out of the access log.

    The container health check asks every 20 seconds, forever, and when nothing
    is happening it is the only thing that ever speaks. Left in, that is ~4,300
    lines a day, and `docker logs quorum` — the first place anybody looks when a
    feature has broken — becomes a wall of 200s with the traceback buried
    somewhere in it. Not a cosmetic complaint: the SAM tracker failure in this
    tree sat in that log for two days behind exactly this.

    Done here rather than in `__main__` because uvicorn applies its own
    `dictConfig` while starting, which happens after the CLI has run and before
    this: a filter added any earlier is configured away.
    """
    import logging

    class NoHealth(logging.Filter):
        def filter(self, record):                       # noqa: A003 - logging's name
            return "/health" not in str(record.args or ())

    # By name, not by `isinstance`: the class is defined afresh on every call,
    # so a second app in the same process would never recognise the first's
    # filter and would stack another one on the logger.
    log = logging.getLogger("uvicorn.access")
    if not any(type(f).__name__ == "NoHealth" for f in log.filters):
        log.addFilter(NoHealth())


def create_app(cfg: Config | None = None, plugin_settings: dict | None = None) -> FastAPI:
    cfg = cfg or Config.load()
    host = Host(cfg, plugin_settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        host.hub.bind(asyncio.get_running_loop())
        _quiet_healthchecks()
        host.run_startup()
        yield

    app = FastAPI(title="Quorum", version="0.1.0", lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.host = host
    # mask payloads are runs of digits; they compress ~5x and this is the hot path
    app.add_middleware(GZipMiddleware, minimum_size=2048, compresslevel=5)

    app.include_router(routes.router, prefix="/api")
    app.include_router(assets.router, prefix="/api", tags=["assets"])
    app.include_router(exports.router, prefix="/api", tags=["exports"])
    app.include_router(browse.router, prefix="/api", tags=["browse"])
    app.include_router(ws.router)

    # plugin routes, namespaced; plugin ES modules and assets, served read-only
    for pid, plugin in host.plugins.items():
        if plugin.route.routes:
            app.include_router(plugin.route, prefix=f"/api/p/{pid}", tags=[pid])
        webdir = (plugin.dir or Path()) / "web"
        if webdir.is_dir():
            app.mount(f"/plugins/{pid}/web", StaticFiles(directory=webdir), name=f"web-{pid}")

    app.mount("/core", StaticFiles(directory=WEB / "core"), name="core")

    # There is no build step here on purpose — a plugin's UI is a file the
    # server hands out. The cost of that is browser caching: without a hint,
    # Chrome heuristically caches a .js with no Cache-Control, and you end up
    # debugging yesterday's code in today's tab. `no-cache` does not mean "do
    # not cache", it means "revalidate first", so this stays a 304 and costs
    # nothing.
    @app.middleware("http")
    async def revalidate_code(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(("/core/", "/plugins/")) or request.url.path == "/app.css":
            response.headers["cache-control"] = "no-cache"
        return response

    @app.get("/health")
    def health():
        return {"ok": True, "plugins": list(host.plugins), "problems": host.plugin_problems}

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    @app.get("/app.css")
    def appcss():
        return FileResponse(WEB / "app.css", media_type="text/css")

    @app.exception_handler(404)
    async def spa(request, exc):
        # deep links (/capture/3/identity) are the client's business
        if request.url.path.startswith(("/api", "/plugins", "/core", "/ws")):
            return JSONResponse({"detail": getattr(exc, "detail", "not found")}, status_code=404)
        return FileResponse(WEB / "index.html")

    return app
