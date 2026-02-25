from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import threading
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
THREADS_CACHE_TTL_S = 2.5
THREADS_CACHE_LOCK = threading.Lock()
THREADS_CACHE: dict[str, Any] = {"key": None, "expires": 0.0, "data": None}
DEFAULT_NOTIFY_TEXT_MAX_CHARS = int(os.getenv("NOTIFY_TEXT_MAX_CHARS", "4000"))


class Settings(BaseModel):
    relay_url: str = "http://127.0.0.1:8765"
    relay_token: str = ""
    default_wait: bool = True
    wait_timeout: int = 120
    poll_interval: float = 1.0
    tts_enabled: bool = False
    tts_service: str = "tts.speak"
    tts_entity_id: str = ""
    tts_media_player_entity_id: str = ""
    assist_enabled: bool = False
    assist_agent_id: str = ""
    assist_language: str = ""
    notify_webhook_id: str = "velox_funis_webhook"
    notify_text_max_chars: int = DEFAULT_NOTIFY_TEXT_MAX_CHARS


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
        notify_text_max_chars=int(os.getenv("NOTIFY_TEXT_MAX_CHARS", str(DEFAULT_NOTIFY_TEXT_MAX_CHARS))),
    )


app = FastAPI(title="Codex Chat Add-on", version="0.2.8")
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


class ThreadArchiveBody(BaseModel):
    archived: bool = True


class HaTtsBody(BaseModel):
    message: str
    service: str | None = None
    entity_id: str | None = None
    media_player_entity_id: str | None = None
    language: str | None = None
    cache: bool | None = None
    options: dict[str, Any] | None = None


class HaAssistBody(BaseModel):
    text: str
    agent_id: str | None = None
    language: str | None = None
    conversation_id: str | None = None


class HaNotifyBody(BaseModel):
    message: str
    title: str | None = "Funis"
    level: str | None = "info"
    webhook_id: str | None = None
    data: dict[str, Any] | None = None


def relay_headers(settings: Settings) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.relay_token:
        headers["Authorization"] = f"Bearer {settings.relay_token}"
    return headers


def relay_base_url(settings: Settings) -> str:
    return settings.relay_url.rstrip("/")


def detail_text(detail: Any) -> str:
    if isinstance(detail, str):
        return detail
    try:
        return json.dumps(detail, ensure_ascii=True)
    except Exception:
        return str(detail)


def _truncate_text(value: str, max_chars: int) -> tuple[str, bool]:
    text = value or ""
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    suffix = "… [truncated]"
    keep = max(0, max_chars - len(suffix))
    return text[:keep] + suffix, True


def _sanitize_notify_data(data: dict[str, Any] | None, max_chars: int) -> tuple[dict[str, Any] | None, list[str]]:
    if data is None:
        return None, []
    out: dict[str, Any] = dict(data)
    truncated_fields: list[str] = []
    for key in ("human_response", "response", "message", "text"):
        val = out.get(key)
        if isinstance(val, str):
            new_val, changed = _truncate_text(val, max_chars)
            if changed:
                out[key] = new_val
                truncated_fields.append(f"data.{key}")
    return out, truncated_fields


def parse_service(service: str) -> tuple[str, str]:
    value = (service or "").strip()
    if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", value):
        raise HTTPException(status_code=400, detail="Invalid service format; expected '<domain>.<service>'")
    return tuple(value.split(".", 1))  # type: ignore[return-value]


def supervisor_headers() -> dict[str, str]:
    token = os.getenv("SUPERVISOR_TOKEN", "").strip()
    if not token:
        raise HTTPException(status_code=500, detail="SUPERVISOR_TOKEN not available in add-on runtime")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


async def ha_service_call(service: str, payload: dict[str, Any]) -> Any:
    domain, name = parse_service(service)
    url = f"http://supervisor/core/api/services/{domain}/{name}"
    settings = load_settings()
    async with httpx.AsyncClient(timeout=max(15, settings.wait_timeout)) as client:
        try:
            resp = await client.post(url, headers=supervisor_headers(), json=payload)
        except Exception as exc:
            LOG.exception("HA service call failed service=%s error=%s", service, type(exc).__name__)
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "Home Assistant service unreachable",
                    "service": service,
                    "exception": type(exc).__name__,
                    "message": str(exc),
                },
            ) from exc

    if resp.status_code >= 400:
        LOG.warning("HA service call non-2xx service=%s status=%s body=%s", service, resp.status_code, resp.text[:400])
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    try:
        return resp.json()
    except Exception:
        return {"ok": True}


async def ha_webhook_call(webhook_id: str, payload: dict[str, Any]) -> Any:
    cleaned = (webhook_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,128}", cleaned):
        raise HTTPException(status_code=400, detail="Invalid webhook_id format")
    url = f"http://supervisor/core/api/webhook/{cleaned}"
    settings = load_settings()
    async with httpx.AsyncClient(timeout=max(10, settings.wait_timeout)) as client:
        try:
            resp = await client.post(url, headers=supervisor_headers(), json=payload)
        except Exception as exc:
            LOG.exception("HA webhook call failed webhook_id=%s error=%s", cleaned, type(exc).__name__)
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "Home Assistant webhook unreachable",
                    "webhook_id": cleaned,
                    "exception": type(exc).__name__,
                    "message": str(exc),
                },
            ) from exc

    if resp.status_code >= 400:
        LOG.warning("HA webhook call non-2xx webhook_id=%s status=%s body=%s", cleaned, resp.status_code, resp.text[:400])
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    try:
        return resp.json()
    except Exception:
        return {"ok": True}


def extract_thread(obj: dict[str, Any]) -> dict[str, Any] | None:
    thread = obj.get("thread")
    return thread if isinstance(thread, dict) else None


def thread_has_agent_message(thread: dict[str, Any]) -> bool:
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return False
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                return True
    return False


async def poll_until_agent_message(thread_id: str, timeout_s: int, poll_s: float) -> dict[str, Any]:
    deadline = time.time() + max(3, timeout_s)
    last_result: dict[str, Any] | None = None
    while time.time() < deadline:
        result = await relay_get(f"/threads/{thread_id}", params={"includeTurns": "true"})
        last_result = result
        thread = extract_thread(result)
        if thread and thread_has_agent_message(thread):
            return result
        await asyncio.sleep(max(0.2, poll_s))
    return last_result or await relay_get(f"/threads/{thread_id}", params={"includeTurns": "true"})


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
        "tts_enabled": settings.tts_enabled,
        "tts_service": settings.tts_service,
        "tts_entity_id": settings.tts_entity_id,
        "tts_media_player_entity_id": settings.tts_media_player_entity_id,
        "assist_enabled": settings.assist_enabled,
        "assist_agent_id": settings.assist_agent_id,
        "assist_language": settings.assist_language,
        "notify_webhook_id": settings.notify_webhook_id,
        "relay_health": relay,
        "relay_error": relay_error,
    }


@app.get("/api/ha/tts/config")
async def api_ha_tts_config() -> dict[str, Any]:
    settings = load_settings()
    return {
        "enabled": settings.tts_enabled,
        "service": settings.tts_service,
        "entity_id": settings.tts_entity_id,
        "media_player_entity_id": settings.tts_media_player_entity_id,
    }


@app.post("/api/ha/tts")
async def api_ha_tts(body: HaTtsBody) -> dict[str, Any]:
    settings = load_settings()
    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    service = (body.service or settings.tts_service).strip() or "tts.speak"
    entity_id = (body.entity_id or settings.tts_entity_id).strip()
    media_player_entity_id = (body.media_player_entity_id or settings.tts_media_player_entity_id).strip()
    # Compatibility fallback: allow callers that provide a media_player in entity_id
    # to work with tts.speak without duplicating config fields.
    if service == "tts.speak" and not media_player_entity_id and entity_id.startswith("media_player."):
        media_player_entity_id = entity_id
    if service == "tts.speak" and not media_player_entity_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "media_player_entity_id is required when service is tts.speak. "
                "Set add-on option tts_media_player_entity_id (for example media_player.your_desktop)"
            ),
        )

    payload: dict[str, Any] = {"message": message}
    if entity_id:
        payload["entity_id"] = entity_id
    if media_player_entity_id:
        payload["media_player_entity_id"] = media_player_entity_id
    if body.language:
        payload["language"] = body.language
    if body.cache is not None:
        payload["cache"] = body.cache
    if body.options is not None:
        payload["options"] = body.options

    result = await ha_service_call(service, payload)
    return {"ok": True, "service": service, "result": result}


@app.get("/api/ha/assist/config")
async def api_ha_assist_config() -> dict[str, Any]:
    settings = load_settings()
    return {
        "enabled": settings.assist_enabled,
        "agent_id": settings.assist_agent_id,
        "language": settings.assist_language,
    }


@app.post("/api/ha/assist/process")
async def api_ha_assist_process(body: HaAssistBody) -> dict[str, Any]:
    settings = load_settings()
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    payload: dict[str, Any] = {"text": text}
    agent_id = (body.agent_id or settings.assist_agent_id).strip()
    language = (body.language or settings.assist_language).strip()
    if agent_id:
        payload["agent_id"] = agent_id
    if language:
        payload["language"] = language
    if body.conversation_id:
        payload["conversation_id"] = body.conversation_id

    result = await ha_service_call("conversation.process", payload)
    response_text = ""
    if isinstance(result, list) and result and isinstance(result[0], dict):
        resp = result[0]
        response_obj = resp.get("response")
        if isinstance(response_obj, dict):
            speech = response_obj.get("speech")
            if isinstance(speech, dict):
                plain = speech.get("plain")
                if isinstance(plain, dict):
                    response_text = str(plain.get("speech", "") or "")
    return {"ok": True, "result": result, "response_text": response_text}


@app.get("/api/ha/notify/config")
async def api_ha_notify_config() -> dict[str, Any]:
    settings = load_settings()
    return {
        "webhook_id": settings.notify_webhook_id,
    }


@app.post("/api/ha/notify")
async def api_ha_notify(body: HaNotifyBody) -> dict[str, Any]:
    settings = load_settings()
    max_chars = max(200, int(settings.notify_text_max_chars))
    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    message, message_truncated = _truncate_text(message, max_chars)
    webhook_id = (body.webhook_id or settings.notify_webhook_id).strip()
    if not webhook_id:
        raise HTTPException(status_code=400, detail="webhook_id is required")
    payload: dict[str, Any] = {
        "title": (body.title or "Funis").strip() or "Funis",
        "message": message,
        "level": (body.level or "info").strip() or "info",
    }
    truncated_fields: list[str] = ["message"] if message_truncated else []
    if body.data is not None:
        safe_data, data_truncated = _sanitize_notify_data(body.data, max_chars)
        payload["data"] = safe_data
        truncated_fields.extend(data_truncated)
    result = await ha_webhook_call(webhook_id, payload)
    return {
        "ok": True,
        "webhook_id": webhook_id,
        "result": result,
        "truncated": bool(truncated_fields),
        "truncated_fields": truncated_fields,
        "notify_text_max_chars": max_chars,
    }


@app.get("/api/threads")
async def api_threads(
    limit: int = Query(default=30, ge=1, le=200),
    cursor: str | None = None,
    sourceKinds: str | None = "vscode",
    archived: bool | None = None,
    updatedAfter: int | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    cache_key = json.dumps(
        {"limit": limit, "cursor": cursor, "sourceKinds": sourceKinds, "archived": archived},
        sort_keys=True,
    )
    now = time.time()
    if cursor:
        params["cursor"] = cursor
    if sourceKinds:
        params["sourceKinds"] = sourceKinds
    if archived is not None:
        params["archived"] = str(archived).lower()
    params["limit"] = limit

    with THREADS_CACHE_LOCK:
        cached = THREADS_CACHE["data"] if THREADS_CACHE["key"] == cache_key and THREADS_CACHE["expires"] > now else None

    if cached is None:
        data = await relay_get("/threads", params=params)
        with THREADS_CACHE_LOCK:
            THREADS_CACHE["key"] = cache_key
            THREADS_CACHE["expires"] = time.time() + THREADS_CACHE_TTL_S
            THREADS_CACHE["data"] = data
    else:
        data = cached

    if updatedAfter is None:
        return data

    rows = data.get("data", [])
    if not isinstance(rows, list):
        return data
    filtered = [row for row in rows if isinstance(row, dict) and int(row.get("updatedAt", 0)) > updatedAfter]
    return {"data": filtered, "nextCursor": data.get("nextCursor")}


@app.get("/api/threads/{thread_id}")
async def api_thread_read(thread_id: str, includeTurns: bool = True) -> dict[str, Any]:
    try:
        return await relay_get(f"/threads/{thread_id}", params={"includeTurns": str(includeTurns).lower()})
    except HTTPException as exc:
        # New threads may exist but not yet have materialized turn history.
        text = detail_text(exc.detail)
        if includeTurns and "not materialized yet" in text:
            result = await relay_get(f"/threads/{thread_id}", params={"includeTurns": "false"})
            thread = result.get("thread")
            if isinstance(thread, dict):
                thread["turns"] = []
            return result
        raise


@app.post("/api/threads/start")
async def api_thread_start(body: ThreadStartBody) -> dict[str, Any]:
    payload = body.model_dump(exclude_none=True)
    return await relay_post("/threads/start", payload)


@app.post("/api/threads/{thread_id}/resume")
async def api_thread_resume(thread_id: str, body: ThreadResumeBody) -> dict[str, Any]:
    payload = body.model_dump(exclude_none=True)
    return await relay_post(f"/threads/{thread_id}/resume", payload)


@app.post("/api/threads/{thread_id}/archive")
async def api_thread_archive(thread_id: str, body: ThreadArchiveBody) -> dict[str, Any]:
    # Use generic rpc endpoint to support archive/unarchive without relay-specific wrappers.
    method = "thread/archive" if body.archived else "thread/unarchive"
    out = await relay_post("/rpc", {"method": method, "params": {"threadId": thread_id}})
    return out.get("result", out)


@app.post("/api/threads/{thread_id}/materialize")
async def api_thread_materialize(thread_id: str) -> dict[str, Any]:
    # Ensure thread exists in current app-server context and returns a stable read shape.
    try:
        await relay_post(f"/threads/{thread_id}/resume", {})
    except HTTPException:
        pass
    return await api_thread_read(thread_id=thread_id, includeTurns=False)


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
    # Best-effort resume to materialize thread state in relay before posting a turn.
    try:
        await relay_post(f"/threads/{thread_id}/resume", {})
    except HTTPException:
        pass

    try:
        result = await relay_post(f"/threads/{thread_id}/turns", {"text": body.text}, params=params)
    except HTTPException as exc:
        # Retry once after resume when relay reports "thread not found".
        text = detail_text(exc.detail)
        if "thread not found" in text:
            await relay_post(f"/threads/{thread_id}/resume", {})
            result = await relay_post(f"/threads/{thread_id}/turns", {"text": body.text}, params=params)
        else:
            raise

    # Some relay/app-server flows mark turn completed before agent message is materialized.
    # Do a short follow-up poll so UI gets the assistant reply without requiring another user action.
    thread_read = result.get("threadRead")
    if isinstance(thread_read, dict):
        thread = extract_thread(thread_read)
        if thread and not thread_has_agent_message(thread):
            try:
                refreshed = await poll_until_agent_message(
                    thread_id=thread_id,
                    timeout_s=min(wait_timeout, 20),
                    poll_s=settings.poll_interval,
                )
                result["threadRead"] = refreshed
            except Exception:
                # Fallback to original result; frontend can still refresh.
                pass
    return result


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(
        str(STATIC_DIR / "index.html"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


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
