from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

OPTIONS_PATH = Path("/data/options.json")
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
LOG = logging.getLogger("codex-chat-addon")


class Settings(BaseModel):
    relay_url: str = "http://127.0.0.1:8765"
    relay_token: str = ""
    default_wait: bool = True
    wait_timeout: int = 120
    poll_interval: float = 1.0


def load_settings() -> Settings:
    # Home Assistant add-on options are available in /data/options.json.
    if OPTIONS_PATH.exists():
        try:
            data = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
            return Settings(**data)
        except Exception as exc:
            raise RuntimeError(f"Invalid add-on options file {OPTIONS_PATH}: {exc}")

    # Local development fallback.
    return Settings(
        relay_url=os.getenv("RELAY_URL", "http://127.0.0.1:8765"),
        relay_token=os.getenv("RELAY_TOKEN", ""),
        default_wait=os.getenv("DEFAULT_WAIT", "true").lower() == "true",
        wait_timeout=int(os.getenv("WAIT_TIMEOUT", "120")),
        poll_interval=float(os.getenv("POLL_INTERVAL", "1.0")),
    )


app = FastAPI(title="Codex Chat Add-on", version="0.1.2")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class ThreadStartBody(BaseModel):
    cwd: str | None = None
    approvalPolicy: str | None = "never"
    model: str | None = None


class ThreadResumeBody(BaseModel):
    cwd: str | None = None
    approvalPolicy: str | None = None
    model: str | None = None


class TurnBody(BaseModel):
    text: str
    wait: bool | None = None
    waitTimeout: int | None = None


def relay_headers(settings: Settings) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.relay_token:
        headers["Authorization"] = f"Bearer {settings.relay_token}"
    return headers


def relay_base_url(settings: Settings) -> str:
    return settings.relay_url.rstrip("/")


async def relay_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = load_settings()
    url = f"{relay_base_url(settings)}{path}"
    async with httpx.AsyncClient(timeout=settings.wait_timeout + 15) as client:
        try:
            resp = await client.get(
                url,
                headers=relay_headers(settings),
                params=params,
            )
        except Exception as exc:
            LOG.exception("Relay GET failed url=%s params=%s error=%s", url, params, type(exc).__name__)
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "Relay unreachable",
                    "url": url,
                    "exception": type(exc).__name__,
                    "message": str(exc),
                },
            ) from exc

    if resp.status_code >= 400:
        LOG.warning("Relay GET non-2xx url=%s status=%s body=%s", url, resp.status_code, resp.text[:400])
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


async def relay_post(path: str, body: dict[str, Any], params: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = load_settings()
    url = f"{relay_base_url(settings)}{path}"
    async with httpx.AsyncClient(timeout=settings.wait_timeout + 30) as client:
        try:
            resp = await client.post(
                url,
                headers=relay_headers(settings),
                params=params,
                json=body,
            )
        except Exception as exc:
            LOG.exception("Relay POST failed url=%s params=%s error=%s", url, params, type(exc).__name__)
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "Relay unreachable",
                    "url": url,
                    "exception": type(exc).__name__,
                    "message": str(exc),
                },
            ) from exc

    if resp.status_code >= 400:
        LOG.warning("Relay POST non-2xx url=%s status=%s body=%s", url, resp.status_code, resp.text[:400])
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    settings = load_settings()
    relay_health = await relay_get("/health")
    return {
        "ok": True,
        "addon": "codex_chat",
        "relay_url": settings.relay_url,
        "relay": relay_health,
    }


@app.get("/api/diagnostics")
async def api_diagnostics() -> dict[str, Any]:
    settings = load_settings()
    relay_url = relay_base_url(settings)
    token_present = bool(settings.relay_token)
    try:
        relay = await relay_get("/health")
        relay_ok = True
        relay_error = None
    except HTTPException as exc:
        relay_ok = False
        relay = None
        relay_error = exc.detail

    return {
        "ok": relay_ok,
        "relay_url": relay_url,
        "relay_token_present": token_present,
        "wait_timeout": settings.wait_timeout,
        "poll_interval": settings.poll_interval,
        "relay_health": relay,
        "relay_error": relay_error,
    }


@app.get("/api/threads")
async def api_threads(
    limit: int = Query(default=30, ge=1, le=200),
    cursor: str | None = None,
    sourceKinds: str | None = "vscode",
    archived: bool | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    if sourceKinds:
        params["sourceKinds"] = sourceKinds
    if archived is not None:
        params["archived"] = str(archived).lower()
    return await relay_get("/threads", params=params)


@app.get("/api/threads/{thread_id}")
async def api_thread_read(thread_id: str, includeTurns: bool = True) -> dict[str, Any]:
    return await relay_get(f"/threads/{thread_id}", params={"includeTurns": str(includeTurns).lower()})


@app.post("/api/threads/start")
async def api_thread_start(body: ThreadStartBody) -> dict[str, Any]:
    payload = body.model_dump(exclude_none=True)
    return await relay_post("/threads/start", payload)


@app.post("/api/threads/{thread_id}/resume")
async def api_thread_resume(thread_id: str, body: ThreadResumeBody) -> dict[str, Any]:
    payload = body.model_dump(exclude_none=True)
    return await relay_post(f"/threads/{thread_id}/resume", payload)


@app.post("/api/threads/{thread_id}/turns")
async def api_turn_start(thread_id: str, body: TurnBody) -> dict[str, Any]:
    settings = load_settings()
    wait = settings.default_wait if body.wait is None else body.wait
    wait_timeout = settings.wait_timeout if body.waitTimeout is None else body.waitTimeout

    params = {
        "wait": str(wait).lower(),
        "waitTimeout": str(wait_timeout),
        "waitPoll": str(settings.poll_interval),
    }
    return await relay_post(f"/threads/{thread_id}/turns", {"text": body.text}, params=params)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.on_event("startup")
async def startup_log() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    LOG.info(
        "Codex Chat add-on started relay_url=%s relay_token_present=%s wait_timeout=%s poll_interval=%s",
        relay_base_url(settings),
        bool(settings.relay_token),
        settings.wait_timeout,
        settings.poll_interval,
    )
