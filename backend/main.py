from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, ORJSONResponse
from fastapi.staticfiles import StaticFiles

from qa_agent.api import router
from qa_agent.config import settings
from qa_agent.human_api import router as human_router


app = FastAPI(
    title="General QA Agent",
    version="4.1.0",
    default_response_class=ORJSONResponse,
)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.include_router(human_router)


@app.middleware("http")
async def cache_policy(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# Production/local mode serves the prebuilt React app from the same process,
# avoiding an idle Vite/Node process during normal use.
if settings.dashboard_dist.exists():
    assets = settings.dashboard_dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        requested = (settings.dashboard_dist / full_path).resolve()
        root = settings.dashboard_dist.resolve()
        if full_path and requested.is_file() and root in requested.parents:
            return FileResponse(requested)
        return FileResponse(root / "index.html")

