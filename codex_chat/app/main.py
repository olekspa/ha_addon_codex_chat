from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import threading
import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

OPTIONS_PATH = Path("/data/options.json")
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
LOG = logging.getLogger("codex-chat-addon")
THREADS_CACHE_TTL_S = 2.5
THREADS_CACHE_LOCK = threading.Lock()
THREADS_CACHE: dict[str, Any] = {"key": None, "expires": 0.0, "data": None}
DEFAULT_NOTIFY_TEXT_MAX_CHARS = int(os.getenv("NOTIFY_TEXT_MAX_CHARS", "4000"))
APP_VERSION = "0.4.10"
ROUTE_LENTUS = "lentus"
ROUTE_MULSUS = "mulsus"
VALID_ROUTES = {ROUTE_LENTUS, ROUTE_MULSUS}
PERSON_USER_CACHE_TTL_S = 60.0
PERSON_USER_CACHE_LOCK = threading.Lock()
PERSON_USER_CACHE: dict[str, Any] = {"key": None, "expires": 0.0, "data": None}
FORBIDDEN_BUTTON_LABELS = (
    "Speak Last",
    "Assist Input",
    "Assist Last",
    "Pin",
    "Archive",
    "Unarchive",
    "Materialize",
)
FORBIDDEN_BUTTON_IDS = (
    "speakLastBtn",
    "assistInputBtn",
    "assistLastBtn",
    "pinBtn",
    "archiveBtn",
    "unarchiveBtn",
    "materializeBtn",
)
FORBIDDEN_BUTTON_BY_LABEL_RE = re.compile(
    r"<button\b[^>]*>\s*(?:"
    + "|".join(re.escape(label) for label in FORBIDDEN_BUTTON_LABELS)
    + r")\s*</button>",
    re.IGNORECASE,
)
FORBIDDEN_BUTTON_BY_ID_RE = re.compile(
    r"<button\b[^>]*\bid=['\"](?:"
    + "|".join(re.escape(button_id) for button_id in FORBIDDEN_BUTTON_IDS)
    + r")['\"][^>]*>.*?</button>",
    re.IGNORECASE | re.DOTALL,
)
TURN_WAIT_TIMEOUT_RE = re.compile(r"Timed out waiting for turn completion:\s*([A-Za-z0-9._:-]+)")


def invalidate_threads_cache() -> None:
    with THREADS_CACHE_LOCK:
        THREADS_CACHE["key"] = None
        THREADS_CACHE["expires"] = 0.0
        THREADS_CACHE["data"] = None


class Settings(BaseModel):
    relay_url: str = "http://127.0.0.1:8765"
    relay_token: str = ""
    mulsus_relay_url: str = ""
    mulsus_relay_token: str = ""
    admin_person_entity_id: str = "person.alex"
    mulsus_person_entity_id: str = "person.tetyana"
    lentus_agent_label: str = "Lentus"
    mulsus_agent_label: str = "Mulsus"
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
    notify_webhook_id: str = "lentus_agent_webhook"
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
        mulsus_relay_url=os.getenv("MULSUS_RELAY_URL", ""),
        mulsus_relay_token=os.getenv("MULSUS_RELAY_TOKEN", ""),
        admin_person_entity_id=os.getenv("ADMIN_PERSON_ENTITY_ID", "person.alex"),
        mulsus_person_entity_id=os.getenv("MULSUS_PERSON_ENTITY_ID", "person.tetyana"),
        lentus_agent_label=os.getenv("LENTUS_AGENT_LABEL", "Lentus"),
        mulsus_agent_label=os.getenv("MULSUS_AGENT_LABEL", "Mulsus"),
        default_wait=os.getenv("DEFAULT_WAIT", "true").lower() == "true",
        wait_timeout=int(os.getenv("WAIT_TIMEOUT", "120")),
        poll_interval=float(os.getenv("POLL_INTERVAL", "1.0")),
        notify_text_max_chars=int(os.getenv("NOTIFY_TEXT_MAX_CHARS", str(DEFAULT_NOTIFY_TEXT_MAX_CHARS))),
    )


app = FastAPI(title="Codex Chat Add-on", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    title: str | None = "Lentus"
    level: str | None = "info"
    webhook_id: str | None = None
    data: dict[str, Any] | None = None


def relay_headers(relay_token: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if relay_token:
        headers["Authorization"] = f"Bearer {relay_token}"
    return headers


def relay_base_url(relay_url: str) -> str:
    return relay_url.rstrip("/")


class RouteContext(BaseModel):
    key: str
    label: str
    relay_url: str
    relay_token: str


class SessionContext(BaseModel):
    ha_user_id: str
    ha_user_name: str
    ha_user_display_name: str
    allowed_routes: list[str]
    default_route: str


def _first_header(request: Request, *names: str) -> str:
    for name in names:
        value = (request.headers.get(name) or "").strip()
        if value:
            return value
    return ""


def _normalize_route_key(route: str | None) -> str | None:
    raw = (route or "").strip().lower()
    if not raw:
        return None
    if raw not in VALID_ROUTES:
        raise HTTPException(status_code=400, detail=f"Invalid route '{route}'. Expected one of: lentus, mulsus")
    return raw


def _route_catalog(settings: Settings) -> dict[str, RouteContext]:
    return {
        ROUTE_LENTUS: RouteContext(
            key=ROUTE_LENTUS,
            label=(settings.lentus_agent_label or "Lentus").strip() or "Lentus",
            relay_url=(settings.relay_url or "").strip(),
            relay_token=(settings.relay_token or "").strip(),
        ),
        ROUTE_MULSUS: RouteContext(
            key=ROUTE_MULSUS,
            label=(settings.mulsus_agent_label or "Mulsus").strip() or "Mulsus",
            relay_url=(settings.mulsus_relay_url or "").strip(),
            relay_token=(settings.mulsus_relay_token or "").strip(),
        ),
    }


def _configured_routes(settings: Settings) -> set[str]:
    configured: set[str] = set()
    for key, route in _route_catalog(settings).items():
        if route.relay_url:
            configured.add(key)
    return configured


def _parse_person_entity_id(entity_id: str, *, label: str) -> str:
    cleaned = (entity_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+\.[A-Za-z0-9_]+", cleaned):
        raise HTTPException(status_code=500, detail=f"Invalid {label} entity id: '{entity_id}'")
    return cleaned


async def ha_get_state(entity_id: str, timeout_s: int) -> dict[str, Any]:
    url = f"http://supervisor/core/api/states/{quote(entity_id, safe='')}"
    async with httpx.AsyncClient(timeout=max(10, timeout_s)) as client:
        try:
            resp = await client.get(url, headers=supervisor_headers())
        except Exception as exc:
            LOG.exception("HA state fetch failed entity_id=%s error=%s", entity_id, type(exc).__name__)
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "Home Assistant state unreachable",
                    "entity_id": entity_id,
                    "exception": type(exc).__name__,
                    "message": str(exc),
                },
            ) from exc
    if resp.status_code >= 400:
        if resp.status_code == 401:
            LOG.error(
                "HA state fetch unauthorized entity_id=%s status=%s body=%s",
                entity_id,
                resp.status_code,
                resp.text[:400],
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "Home Assistant API unauthorized while resolving per-user routing",
                    "entity_id": entity_id,
                    "hint": "Set add-on config `homeassistant_api: true`, then restart the add-on.",
                },
            )
        LOG.warning("HA state fetch non-2xx entity_id=%s status=%s body=%s", entity_id, resp.status_code, resp.text[:400])
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    payload = resp.json()
    if isinstance(payload, dict) and isinstance(payload.get("attributes"), dict):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
    raise HTTPException(status_code=502, detail=f"Unexpected HA state payload for {entity_id}")


def _extract_person_user_id(state: dict[str, Any], *, entity_id: str) -> str:
    attributes = state.get("attributes")
    if not isinstance(attributes, dict):
        raise HTTPException(status_code=500, detail=f"Entity {entity_id} has no attributes map")
    user_id = str(attributes.get("user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=500, detail=f"Entity {entity_id} has no attributes.user_id")
    return user_id


async def _resolve_person_user_mapping(settings: Settings) -> dict[str, str]:
    admin_entity_id = _parse_person_entity_id(settings.admin_person_entity_id, label="admin_person_entity_id")
    mulsus_entity_id = _parse_person_entity_id(settings.mulsus_person_entity_id, label="mulsus_person_entity_id")
    cache_key = (admin_entity_id.lower(), mulsus_entity_id.lower())
    now = time.time()
    with PERSON_USER_CACHE_LOCK:
        cached_key = PERSON_USER_CACHE.get("key")
        cached_exp = float(PERSON_USER_CACHE.get("expires") or 0.0)
        cached_data = PERSON_USER_CACHE.get("data")
        if cached_key == cache_key and cached_exp > now and isinstance(cached_data, dict):
            return dict(cached_data)

    admin_state, mulsus_state = await asyncio.gather(
        ha_get_state(admin_entity_id, timeout_s=settings.wait_timeout),
        ha_get_state(mulsus_entity_id, timeout_s=settings.wait_timeout),
    )
    mapping = {
        "admin_user_id": _extract_person_user_id(admin_state, entity_id=admin_entity_id),
        "mulsus_user_id": _extract_person_user_id(mulsus_state, entity_id=mulsus_entity_id),
    }
    with PERSON_USER_CACHE_LOCK:
        PERSON_USER_CACHE["key"] = cache_key
        PERSON_USER_CACHE["expires"] = time.time() + PERSON_USER_CACHE_TTL_S
        PERSON_USER_CACHE["data"] = dict(mapping)
    return mapping


async def resolve_user_session(request: Request, settings: Settings) -> SessionContext:
    ingress_user_id = _first_header(request, "X-Remote-User-Id")
    if not ingress_user_id:
        raise HTTPException(status_code=403, detail="Missing X-Remote-User-Id ingress header")

    ingress_user_name = _first_header(request, "X-Remote-User-Name", "X-Remote-User")
    ingress_user_display_name = _first_header(request, "X-Remote-User-Display-Name", "X-Remote-User-Name")
    user_mapping = await _resolve_person_user_mapping(settings)
    configured_routes = _configured_routes(settings)

    if ingress_user_id == user_mapping["admin_user_id"]:
        allowed_routes = [route for route in (ROUTE_LENTUS, ROUTE_MULSUS) if route in configured_routes]
        default_route = ROUTE_LENTUS if ROUTE_LENTUS in allowed_routes else (allowed_routes[0] if allowed_routes else ROUTE_LENTUS)
    elif ingress_user_id == user_mapping["mulsus_user_id"]:
        allowed_routes = [ROUTE_MULSUS] if ROUTE_MULSUS in configured_routes else []
        default_route = ROUTE_MULSUS
    else:
        raise HTTPException(status_code=403, detail="User is not mapped to an allowed agent route")

    if not allowed_routes:
        raise HTTPException(status_code=503, detail="No configured relay routes available for this user")

    return SessionContext(
        ha_user_id=ingress_user_id,
        ha_user_name=ingress_user_name,
        ha_user_display_name=ingress_user_display_name or ingress_user_name or ingress_user_id,
        allowed_routes=allowed_routes,
        default_route=default_route,
    )


def resolve_route_context(settings: Settings, session: SessionContext, route: str | None) -> RouteContext:
    chosen_route = _normalize_route_key(route) or session.default_route
    if chosen_route not in session.allowed_routes:
        raise HTTPException(status_code=403, detail=f"Route '{chosen_route}' is not allowed for this user")
    route_context = _route_catalog(settings).get(chosen_route)
    if route_context is None:
        raise HTTPException(status_code=500, detail=f"Unknown route '{chosen_route}'")
    if not route_context.relay_url:
        raise HTTPException(status_code=503, detail=f"Route '{chosen_route}' is not configured")
    return route_context


def build_session_payload(session: SessionContext, settings: Settings, effective_route: str) -> dict[str, Any]:
    route_labels = {key: route.label for key, route in _route_catalog(settings).items()}
    return {
        "ok": True,
        "ha_user_id": session.ha_user_id,
        "ha_user_name": session.ha_user_name,
        "ha_user_display_name": session.ha_user_display_name,
        "allowed_routes": session.allowed_routes,
        "default_route": session.default_route,
        "effective_route": effective_route,
        "route_labels": route_labels,
        "agent_label": route_labels.get(effective_route, effective_route),
    }


def detail_text(detail: Any) -> str:
    if isinstance(detail, str):
        return detail
    try:
        return json.dumps(detail, ensure_ascii=True)
    except Exception:
        return str(detail)


def detail_error_text(detail: Any) -> str:
    text = detail_text(detail)
    try:
        parsed = json.loads(text)
    except Exception:
        return text
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()
    return text


def extract_timeout_turn_id(detail: Any) -> str:
    match = TURN_WAIT_TIMEOUT_RE.search(detail_error_text(detail))
    if not match:
        return ""
    return match.group(1).strip()


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


def render_index_html() -> str:
    raw = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    raw = raw.replace("__APP_VERSION__", APP_VERSION)
    # Hard safety net: strip deprecated action buttons from delivered markup.
    sanitized = FORBIDDEN_BUTTON_BY_ID_RE.sub("", raw)
    sanitized = FORBIDDEN_BUTTON_BY_LABEL_RE.sub("", sanitized)
    return sanitized


def html_no_cache_headers(html_text: str) -> dict[str, str]:
    digest = hashlib.sha256(html_text.encode("utf-8")).hexdigest()[:12]
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
        "X-Codex-Chat-Version": APP_VERSION,
        "X-Codex-Chat-UI-SHA": digest,
    }


def supervisor_headers() -> dict[str, str]:
    token = os.getenv("SUPERVISOR_TOKEN", "").strip()
    if not token:
        raise HTTPException(status_code=500, detail="SUPERVISOR_TOKEN not available in add-on runtime")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


async def ha_service_call(service: str, payload: dict[str, Any], timeout_s: float | None = None) -> Any:
    domain, name = parse_service(service)
    url = f"http://supervisor/core/api/services/{domain}/{name}"
    settings = load_settings()
    if timeout_s is None:
        # Assist flows can cascade into a full Codex turn and exceed the normal service budget.
        if service == "conversation.process":
            timeout_s = max(30.0, float(settings.wait_timeout) + 60.0)
        else:
            timeout_s = float(max(15, settings.wait_timeout))
    async with httpx.AsyncClient(timeout=timeout_s) as client:
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


def thread_find_turn_by_id(thread: dict[str, Any], turn_id: str) -> dict[str, Any] | None:
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return None
    for turn in turns:
        if isinstance(turn, dict) and str(turn.get("id") or "") == turn_id:
            return turn
    return None


def turn_has_agent_message(turn: dict[str, Any]) -> bool:
    items = turn.get("items")
    if not isinstance(items, list):
        return False
    for item in items:
        if isinstance(item, dict) and item.get("type") == "agentMessage":
            return True
    return False


def turn_is_terminal(turn: dict[str, Any]) -> bool:
    status = str(turn.get("status") or "").strip().lower()
    return status in {"completed", "failed", "error", "cancelled", "canceled"}


async def poll_until_agent_message(
    route_context: RouteContext,
    thread_id: str,
    timeout_s: int,
    poll_s: float,
) -> dict[str, Any]:
    deadline = time.time() + max(3, timeout_s)
    last_result: dict[str, Any] | None = None
    while time.time() < deadline:
        result = await relay_get(route_context, f"/threads/{thread_id}", params={"includeTurns": "true"})
        last_result = result
        thread = extract_thread(result)
        if thread and thread_has_agent_message(thread):
            return result
        await asyncio.sleep(max(0.2, poll_s))
    return last_result or await relay_get(route_context, f"/threads/{thread_id}", params={"includeTurns": "true"})


async def poll_until_turn_ready(
    route_context: RouteContext,
    thread_id: str,
    turn_id: str,
    timeout_s: int,
    poll_s: float,
) -> dict[str, Any]:
    deadline = time.time() + max(3, timeout_s)
    last_result: dict[str, Any] | None = None
    while time.time() < deadline:
        result = await relay_get(route_context, f"/threads/{thread_id}", params={"includeTurns": "true"})
        last_result = result
        thread = extract_thread(result)
        if thread:
            turn = thread_find_turn_by_id(thread, turn_id)
            if turn and (turn_has_agent_message(turn) or turn_is_terminal(turn)):
                return result
        await asyncio.sleep(max(0.2, poll_s))
    return last_result or await relay_get(route_context, f"/threads/{thread_id}", params={"includeTurns": "true"})


async def relay_get(route_context: RouteContext, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = load_settings()
    url = f"{relay_base_url(route_context.relay_url)}{path}"
    async with httpx.AsyncClient(timeout=settings.wait_timeout + 15) as client:
        try:
            resp = await client.get(
                url,
                headers=relay_headers(route_context.relay_token),
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


async def relay_post(
    route_context: RouteContext,
    path: str,
    body: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = load_settings()
    url = f"{relay_base_url(route_context.relay_url)}{path}"
    async with httpx.AsyncClient(timeout=settings.wait_timeout + 30) as client:
        try:
            resp = await client.post(
                url,
                headers=relay_headers(route_context.relay_token),
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


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _collect_dict_nodes(root: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    stack: list[Any] = [root]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            nodes.append(cur)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return nodes


def _entry_remaining_pct(entry: dict[str, Any]) -> float | None:
    for key in (
        "remaining_pct",
        "remaining_percent",
        "percent_remaining",
        "pct_remaining",
        "remainingPct",
        "remainingPercent",
    ):
        value = _safe_float(entry.get(key))
        if value is not None:
            return max(0.0, min(100.0, value))
    used_pct = _safe_float(entry.get("used_pct"))
    if used_pct is None:
        used_pct = _safe_float(entry.get("used_percent"))
    if used_pct is None:
        used_pct = _safe_float(entry.get("usedPct"))
    if used_pct is None:
        used_pct = _safe_float(entry.get("usedPercent"))
    if used_pct is not None:
        return max(0.0, min(100.0, 100.0 - used_pct))
    remaining = _safe_float(entry.get("remaining"))
    if remaining is None:
        remaining = _safe_float(entry.get("remainingAmount"))
    if remaining is None:
        remaining = _safe_float(entry.get("remaining_count"))
    limit = _safe_float(entry.get("limit"))
    if limit is None:
        limit = _safe_float(entry.get("max"))
    if limit is None:
        limit = _safe_float(entry.get("quota"))
    if remaining is not None and limit is not None and limit > 0:
        return max(0.0, min(100.0, (remaining / limit) * 100.0))
    used = _safe_float(entry.get("used"))
    if used is None:
        used = _safe_float(entry.get("usedAmount"))
    if used is None:
        used = _safe_float(entry.get("consumed"))
    if used is not None and limit is not None and limit > 0:
        return max(0.0, min(100.0, ((limit - used) / limit) * 100.0))
    return None


def _entry_window_name(entry: dict[str, Any]) -> str:
    for key in ("window", "period", "bucket", "name", "label", "title", "windowName", "window_name", "interval"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def _entry_window_seconds(entry: dict[str, Any]) -> float | None:
    for key in ("window_minutes", "windowMinutes", "period_minutes", "periodMinutes", "windowDurationMins"):
        value = _safe_float(entry.get(key))
        if value is not None and value > 0:
            return value * 60.0
    for key in ("window_seconds", "windowSeconds", "period_seconds", "periodSeconds", "duration_seconds", "durationSeconds"):
        value = _safe_float(entry.get(key))
        if value is not None and value > 0:
            return value
    return None


def _normalize_usage_limits(payload: dict[str, Any]) -> dict[str, Any]:
    nodes = _collect_dict_nodes(payload)
    five_hour_pct: float | None = None
    weekly_pct: float | None = None
    updated_at = ""

    for node in nodes:
        if not updated_at:
            candidate = node.get("updated_at") or node.get("updatedAt") or node.get("last_updated_at")
            if isinstance(candidate, str) and candidate.strip():
                updated_at = candidate.strip()
        remaining_pct = _entry_remaining_pct(node)
        if remaining_pct is None:
            continue
        window = _entry_window_name(node)
        if five_hour_pct is None and (
            "5h" in window
            or "5-hour" in window
            or "5 hour" in window
            or ("five" in window and "hour" in window)
        ):
            five_hour_pct = remaining_pct
        window_seconds = _entry_window_seconds(node)
        if five_hour_pct is None and window_seconds is not None and abs(window_seconds - (5 * 3600)) <= 60:
            five_hour_pct = remaining_pct
        if weekly_pct is None and ("week" in window or "weekly" in window):
            weekly_pct = remaining_pct
        if weekly_pct is None and window_seconds is not None and abs(window_seconds - (7 * 24 * 3600)) <= 3600:
            weekly_pct = remaining_pct

    return {
        "five_hour_remaining_pct": five_hour_pct,
        "weekly_remaining_pct": weekly_pct,
        "updated_at": updated_at,
        "raw": payload,
    }


def _sse_event_line(event: str, payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=True)
    lines = [f"event: {event}"]
    for line in serialized.splitlines() or ["{}"]:
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


@app.get("/api/health")
async def api_health(request: Request, route: str | None = Query(default=None)) -> dict[str, Any]:
    settings = load_settings()
    session = await resolve_user_session(request, settings)
    route_context = resolve_route_context(settings, session, route)
    relay_health = await relay_get(route_context, "/health")
    return {
        "ok": True,
        "addon": "codex_chat",
        "version": APP_VERSION,
        "route": route_context.key,
        "agent_label": route_context.label,
        "relay_url": route_context.relay_url,
        "relay": relay_health,
    }


@app.get("/api/version")
async def api_version() -> dict[str, Any]:
    html = render_index_html()
    return {
        "ok": True,
        "version": APP_VERSION,
        "ui_sha": hashlib.sha256(html.encode("utf-8")).hexdigest(),
    }


@app.get("/api/session")
async def api_session(request: Request, route: str | None = Query(default=None)) -> dict[str, Any]:
    settings = load_settings()
    session = await resolve_user_session(request, settings)
    route_context = resolve_route_context(settings, session, route)
    return build_session_payload(session, settings, route_context.key)


@app.get("/api/diagnostics")
async def api_diagnostics(request: Request, route: str | None = Query(default=None)) -> dict[str, Any]:
    settings = load_settings()
    session = await resolve_user_session(request, settings)
    route_context = resolve_route_context(settings, session, route)
    token_present = bool(route_context.relay_token)
    try:
        relay = await relay_get(route_context, "/health")
        relay_ok = True
        relay_error = None
    except HTTPException as exc:
        relay_ok = False
        relay = None
        relay_error = exc.detail

    return {
        "ok": relay_ok,
        "route": route_context.key,
        "agent_label": route_context.label,
        "relay_url": relay_base_url(route_context.relay_url),
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
        "session": build_session_payload(session, settings, route_context.key),
        "relay_health": relay,
        "relay_error": relay_error,
    }


@app.get("/api/usage/limits")
async def api_usage_limits(request: Request, route: str | None = Query(default=None)) -> dict[str, Any]:
    settings = load_settings()
    session = await resolve_user_session(request, settings)
    route_context = resolve_route_context(settings, session, route)
    try:
        relay_payload = await relay_get(route_context, "/usage/limits")
    except HTTPException as exc:
        return {
            "ok": False,
            "route": route_context.key,
            "agent_label": route_context.label,
            "error": detail_text(exc.detail),
            "five_hour_remaining_pct": None,
            "weekly_remaining_pct": None,
            "updated_at": "",
        }

    normalized = _normalize_usage_limits(relay_payload if isinstance(relay_payload, dict) else {"value": relay_payload})
    return {
        "ok": True,
        "route": route_context.key,
        "agent_label": route_context.label,
        **normalized,
    }


@app.get("/api/ha/tts/config")
async def api_ha_tts_config(request: Request) -> dict[str, Any]:
    settings = load_settings()
    await resolve_user_session(request, settings)
    return {
        "enabled": settings.tts_enabled,
        "service": settings.tts_service,
        "entity_id": settings.tts_entity_id,
        "media_player_entity_id": settings.tts_media_player_entity_id,
    }


@app.post("/api/ha/tts")
async def api_ha_tts(request: Request, body: HaTtsBody) -> dict[str, Any]:
    settings = load_settings()
    await resolve_user_session(request, settings)
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
async def api_ha_assist_config(request: Request) -> dict[str, Any]:
    settings = load_settings()
    await resolve_user_session(request, settings)
    return {
        "enabled": settings.assist_enabled,
        "agent_id": settings.assist_agent_id,
        "language": settings.assist_language,
    }


@app.post("/api/ha/assist/process")
async def api_ha_assist_process(request: Request, body: HaAssistBody) -> dict[str, Any]:
    settings = load_settings()
    await resolve_user_session(request, settings)
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
async def api_ha_notify_config(request: Request) -> dict[str, Any]:
    settings = load_settings()
    await resolve_user_session(request, settings)
    return {
        "webhook_id": settings.notify_webhook_id,
    }


@app.post("/api/ha/notify")
async def api_ha_notify(request: Request, body: HaNotifyBody) -> dict[str, Any]:
    settings = load_settings()
    await resolve_user_session(request, settings)
    max_chars = max(200, int(settings.notify_text_max_chars))
    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    message, message_truncated = _truncate_text(message, max_chars)
    webhook_id = (body.webhook_id or settings.notify_webhook_id).strip()
    if not webhook_id:
        raise HTTPException(status_code=400, detail="webhook_id is required")
    payload: dict[str, Any] = {
        "title": (body.title or "Lentus").strip() or "Lentus",
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


async def thread_read_with_route(
    route_context: RouteContext,
    thread_id: str,
    include_turns: bool,
) -> dict[str, Any]:
    try:
        return await relay_get(route_context, f"/threads/{thread_id}", params={"includeTurns": str(include_turns).lower()})
    except HTTPException as exc:
        # New threads may exist but not yet have materialized turn history.
        text = detail_text(exc.detail)
        if include_turns and "not materialized yet" in text:
            result = await relay_get(route_context, f"/threads/{thread_id}", params={"includeTurns": "false"})
            thread = result.get("thread")
            if isinstance(thread, dict):
                thread["turns"] = []
            return result
        raise


@app.get("/api/threads")
async def api_threads(
    request: Request,
    route: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=200),
    cursor: str | None = None,
    sourceKinds: str | None = "vscode",
    archived: bool | None = None,
    updatedAfter: int | None = None,
) -> dict[str, Any]:
    settings = load_settings()
    session = await resolve_user_session(request, settings)
    route_context = resolve_route_context(settings, session, route)
    params: dict[str, Any] = {}
    cache_key = json.dumps(
        {
            "userId": session.ha_user_id,
            "route": route_context.key,
            "limit": limit,
            "cursor": cursor,
            "sourceKinds": sourceKinds,
            "archived": archived,
        },
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
        data = await relay_get(route_context, "/threads", params=params)
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
async def api_thread_read(
    request: Request,
    thread_id: str,
    includeTurns: bool = True,
    route: str | None = Query(default=None),
) -> dict[str, Any]:
    settings = load_settings()
    session = await resolve_user_session(request, settings)
    route_context = resolve_route_context(settings, session, route)
    return await thread_read_with_route(route_context, thread_id=thread_id, include_turns=includeTurns)


@app.get("/api/threads/{thread_id}/events")
async def api_thread_events(
    request: Request,
    thread_id: str,
    route: str | None = Query(default=None),
    timeout: int = Query(default=300, ge=5, le=3600),
    heartbeat: int = Query(default=15, ge=2, le=60),
    turnId: str | None = None,
) -> StreamingResponse:
    settings = load_settings()
    session = await resolve_user_session(request, settings)
    route_context = resolve_route_context(settings, session, route)
    url = f"{relay_base_url(route_context.relay_url)}/threads/{thread_id}/events"
    params: dict[str, Any] = {
        "timeout": str(timeout),
        "heartbeat": str(heartbeat),
    }
    if turnId:
        params["turnId"] = turnId

    base_headers = relay_headers(route_context.relay_token)
    base_headers.pop("Content-Type", None)
    base_headers["Accept"] = "text/event-stream"

    async def stream() -> Any:
        async with httpx.AsyncClient(timeout=None) as client:
            try:
                async with client.stream("GET", url, headers=base_headers, params=params) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        detail = body.decode("utf-8", errors="replace")[:400]
                        yield _sse_event_line(
                            "relay_error",
                            {
                                "error": "relay_sse_non_2xx",
                                "status": resp.status_code,
                                "detail": detail,
                            },
                        )
                        return
                    async for line in resp.aiter_lines():
                        if line is None:
                            continue
                        yield f"{line}\n"
            except Exception as exc:
                yield _sse_event_line(
                    "relay_error",
                    {
                        "error": "relay_sse_proxy_failed",
                        "exception": type(exc).__name__,
                        "message": str(exc),
                    },
                )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/threads/start")
async def api_thread_start(
    request: Request,
    body: ThreadStartBody,
    route: str | None = Query(default=None),
) -> dict[str, Any]:
    settings = load_settings()
    session = await resolve_user_session(request, settings)
    route_context = resolve_route_context(settings, session, route)
    payload = body.model_dump(exclude_none=True)
    out = await relay_post(route_context, "/threads/start", payload)
    invalidate_threads_cache()
    return out


@app.post("/api/threads/{thread_id}/resume")
async def api_thread_resume(
    request: Request,
    thread_id: str,
    body: ThreadResumeBody,
    route: str | None = Query(default=None),
) -> dict[str, Any]:
    settings = load_settings()
    session = await resolve_user_session(request, settings)
    route_context = resolve_route_context(settings, session, route)
    payload = body.model_dump(exclude_none=True)
    out = await relay_post(route_context, f"/threads/{thread_id}/resume", payload)
    invalidate_threads_cache()
    return out


@app.post("/api/threads/{thread_id}/archive")
async def api_thread_archive(
    request: Request,
    thread_id: str,
    body: ThreadArchiveBody,
    route: str | None = Query(default=None),
) -> dict[str, Any]:
    settings = load_settings()
    session = await resolve_user_session(request, settings)
    route_context = resolve_route_context(settings, session, route)
    # Use generic rpc endpoint to support archive/unarchive without relay-specific wrappers.
    method = "thread/archive" if body.archived else "thread/unarchive"
    out = await relay_post(route_context, "/rpc", {"method": method, "params": {"threadId": thread_id}})
    invalidate_threads_cache()
    return out.get("result", out)


@app.post("/api/threads/{thread_id}/materialize")
async def api_thread_materialize(
    request: Request,
    thread_id: str,
    route: str | None = Query(default=None),
) -> dict[str, Any]:
    settings = load_settings()
    session = await resolve_user_session(request, settings)
    route_context = resolve_route_context(settings, session, route)
    # Ensure thread exists in current app-server context and returns a stable read shape.
    try:
        await relay_post(route_context, f"/threads/{thread_id}/resume", {})
    except HTTPException:
        pass
    invalidate_threads_cache()
    return await thread_read_with_route(route_context, thread_id=thread_id, include_turns=False)


@app.post("/api/threads/{thread_id}/turns")
async def api_turn_start(
    request: Request,
    thread_id: str,
    body: TurnBody,
    route: str | None = Query(default=None),
) -> dict[str, Any]:
    settings = load_settings()
    session = await resolve_user_session(request, settings)
    route_context = resolve_route_context(settings, session, route)
    wait = settings.default_wait if body.wait is None else body.wait
    wait_timeout = settings.wait_timeout if body.waitTimeout is None else body.waitTimeout

    params = {
        "wait": str(wait).lower(),
        "waitTimeout": str(wait_timeout),
        "waitPoll": str(settings.poll_interval),
    }
    # Best-effort resume to materialize thread state in relay before posting a turn.
    try:
        await relay_post(route_context, f"/threads/{thread_id}/resume", {})
    except HTTPException:
        pass

    try:
        result = await relay_post(route_context, f"/threads/{thread_id}/turns", {"text": body.text}, params=params)
    except HTTPException as exc:
        # Retry once after resume when relay reports "thread not found".
        text = detail_text(exc.detail)
        if "thread not found" in text:
            await relay_post(route_context, f"/threads/{thread_id}/resume", {})
            result = await relay_post(route_context, f"/threads/{thread_id}/turns", {"text": body.text}, params=params)
        elif wait and exc.status_code == 504:
            timeout_turn_id = extract_timeout_turn_id(exc.detail)
            if not timeout_turn_id:
                raise
            grace_s = min(45, max(10, int(wait_timeout)))
            LOG.warning(
                "Turn wait timeout for thread_id=%s turn_id=%s; polling grace window %ss",
                thread_id,
                timeout_turn_id,
                grace_s,
            )
            try:
                recovered_thread_read = await poll_until_turn_ready(
                    route_context=route_context,
                    thread_id=thread_id,
                    turn_id=timeout_turn_id,
                    timeout_s=grace_s,
                    poll_s=settings.poll_interval,
                )
            except Exception:
                LOG.exception(
                    "Failed timeout recovery poll for thread_id=%s turn_id=%s",
                    thread_id,
                    timeout_turn_id,
                )
                raise
            thread = extract_thread(recovered_thread_read)
            turn = thread_find_turn_by_id(thread, timeout_turn_id) if thread else None
            if not turn or not (turn_has_agent_message(turn) or turn_is_terminal(turn)):
                raise
            result = {
                "turnStart": {"turn": {"id": timeout_turn_id}},
                "threadRead": recovered_thread_read,
                "timeoutRecovered": True,
            }
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
                    route_context=route_context,
                    thread_id=thread_id,
                    timeout_s=min(wait_timeout, 20),
                    poll_s=settings.poll_interval,
                )
                result["threadRead"] = refreshed
            except Exception:
                # Fallback to original result; frontend can still refresh.
                pass
    invalidate_threads_cache()
    return result


@app.get("/")
async def index() -> HTMLResponse:
    html = render_index_html()
    return HTMLResponse(content=html, headers=html_no_cache_headers(html))


@app.get("/static/index.html")
async def static_index() -> HTMLResponse:
    html = render_index_html()
    return HTMLResponse(content=html, headers=html_no_cache_headers(html))


@app.on_event("startup")
async def startup_log() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    LOG.info(
        "Codex Chat add-on started lentus_relay_url=%s lentus_token=%s mulsus_relay_url=%s mulsus_token=%s wait_timeout=%s poll_interval=%s",
        relay_base_url(settings.relay_url),
        bool(settings.relay_token),
        relay_base_url(settings.mulsus_relay_url) if settings.mulsus_relay_url else "",
        bool(settings.mulsus_relay_token),
        settings.wait_timeout,
        settings.poll_interval,
    )
