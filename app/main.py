from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .ai.client import ai_client
from .config import settings
from .db import init_db
from .routers import (
    assignments,
    auth,
    companion,
    enrollments,
    google,
    learning,
    onlyoffice,
    professor,
    social,
    tasks,
    tests,
    workspace,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(social.router)
app.include_router(assignments.router)
app.include_router(professor.router)
app.include_router(enrollments.router)
app.include_router(tests.router)
app.include_router(learning.router)
app.include_router(tasks.router)
app.include_router(companion.router)
app.include_router(google.router)
app.include_router(workspace.router)
app.include_router(onlyoffice.router)


@app.get("/api/health")
def health() -> dict[str, object]:
    return {
        "ok": True,
        "ai_available": ai_client.available,
        "ai_provider": settings.ai_provider,
        "model": settings.ai_model,
        "google_available": google.configured(),
        "companion_download": (settings.downloads_dir / "companion.exe").exists(),
        "onlyoffice_available": onlyoffice.configured(),
    }


# 보조 프로그램 설치 파일. backend/downloads/companion.exe 를 두면 /downloads/companion.exe 로 받는다.
if settings.downloads_dir.is_dir():
    app.mount("/downloads", StaticFiles(directory=settings.downloads_dir), name="downloads")


# 빌드된 React 정적 파일을 같은 서버에서 서비스한다. 없으면 API만 돈다.
_dist = settings.frontend_dist
if (_dist / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str, request: Request):  # type: ignore[no-untyped-def]
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate = _dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_dist / "index.html")
