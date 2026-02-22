"""Conversation agent for Funis via Codex relay."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from homeassistant.components import conversation
from homeassistant.components.conversation import ConversationEntity, ConversationInput, ConversationResult
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.storage import Store

from .const import (
    CONF_APPROVAL_POLICY,
    CONF_CWD,
    CONF_MODEL,
    CONF_RELAY_TOKEN,
    CONF_RELAY_URL,
    CONF_SANDBOX_MODE,
    CONF_WAIT_POLL,
    CONF_WAIT_TIMEOUT,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class _RelayConfig:
    relay_url: str
    relay_token: str
    wait_timeout: int
    wait_poll: float
    cwd: str
    model: str
    approval_policy: str
    sandbox_mode: str


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Funis conversation entity."""
    async_add_entities([FunisConversationAgent(hass, config_entry)])


class FunisConversationAgent(ConversationEntity, conversation.AbstractConversationAgent):
    """Funis-backed conversation agent."""

    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_name = entry.title
        self._store: Store[dict[str, str]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._map: dict[str, str] = {}

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Support all languages."""
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        """Register as active conversation agent and load state."""
        await super().async_added_to_hass()
        data = await self._store.async_load()
        self._map = data or {}
        conversation.async_set_agent(self.hass, self.entry, self)

    async def async_will_remove_from_hass(self) -> None:
        """Clean up registration."""
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    async def async_process(self, user_input: ConversationInput) -> ConversationResult:
        """Process a user request via relay thread turn."""
        cfg = self._cfg()
        conv_id = user_input.conversation_id or str(uuid.uuid4())
        response = intent.IntentResponse(language=user_input.language)

        try:
            # First route through HA's built-in conversation agent so exposed-entity
            # control/intents work natively. Fall back to Funis relay only when HA
            # reports no intent match.
            ha_result = await self._ha_builtin_process(user_input, conv_id)
            if ha_result is not None:
                return ha_result

            thread_id = self._map.get(conv_id)
            if not thread_id:
                start_payload: dict[str, Any] = {
                    "approvalPolicy": cfg.approval_policy,
                    "sandbox": cfg.sandbox_mode,
                }
                if cfg.cwd:
                    start_payload["cwd"] = cfg.cwd
                if cfg.model:
                    start_payload["model"] = cfg.model
                start_out = await self._relay_post(cfg, "/threads/start", start_payload)
                thread = start_out.get("thread", {})
                thread_id = thread.get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    raise RuntimeError("thread/start did not return thread id")
                self._map[conv_id] = thread_id
                await self._store.async_save(self._map)
            else:
                resume_payload: dict[str, Any] = {
                    "approvalPolicy": cfg.approval_policy,
                    "sandbox": cfg.sandbox_mode,
                }
                if cfg.cwd:
                    resume_payload["cwd"] = cfg.cwd
                if cfg.model:
                    resume_payload["model"] = cfg.model
                await self._relay_post(cfg, f"/threads/{thread_id}/resume", resume_payload)

            turn_payload = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": user_input.text}],
                "approvalPolicy": cfg.approval_policy,
                "sandboxPolicy": _sandbox_mode_to_turn_policy(cfg.sandbox_mode),
            }
            out = await self._relay_post(
                cfg,
                f"/threads/{thread_id}/turns",
                turn_payload,
                params={
                    "wait": "true",
                    "waitTimeout": str(cfg.wait_timeout),
                    "waitPoll": str(cfg.wait_poll),
                },
            )
            text = _extract_last_agent_message(out)
            if not text:
                # Some relay/app-server paths complete before agent text is fully materialized.
                text = await self._poll_for_agent_message(cfg, thread_id, timeout_s=min(cfg.wait_timeout, 20))
            if not text:
                text = "I completed the request, but no assistant message was returned."
            response.async_set_speech(text)
        except Exception as err:
            _LOGGER.exception("Funis conversation failed")
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"Funis agent error: {err}",
            )

        return ConversationResult(
            response=response,
            conversation_id=conv_id,
            continue_conversation=True,
        )

    async def _ha_builtin_process(self, user_input: ConversationInput, conv_id: str) -> ConversationResult | None:
        """Try HA native conversation first; return None when relay fallback is needed."""
        data: dict[str, Any] = {
            "text": user_input.text,
            "language": user_input.language,
            "agent_id": "home_assistant",
            "conversation_id": conv_id,
        }
        result = await self.hass.services.async_call(
            "conversation",
            "process",
            data,
            blocking=True,
            return_response=True,
            context=user_input.context,
        )
        if not isinstance(result, dict):
            return None

        resp_obj = result.get("response")
        if not isinstance(resp_obj, dict):
            return None
        response_type = str(resp_obj.get("response_type", ""))
        speech = _extract_ha_speech(resp_obj)
        out_conv_id = str(result.get("conversation_id") or conv_id)
        continue_conversation = bool(result.get("continue_conversation", False))

        # Fall back to Funis relay only when HA explicitly reports no intent match.
        if response_type == "error":
            data_obj = resp_obj.get("data")
            code = data_obj.get("code") if isinstance(data_obj, dict) else ""
            if code == "no_intent_match":
                return None

        out = intent.IntentResponse(language=user_input.language)
        if speech:
            out.async_set_speech(speech)
        else:
            out.async_set_speech("Done.")
        return ConversationResult(
            response=out,
            conversation_id=out_conv_id,
            continue_conversation=continue_conversation,
        )

    def _cfg(self) -> _RelayConfig:
        data = self.entry.data
        return _RelayConfig(
            relay_url=data[CONF_RELAY_URL].rstrip("/"),
            relay_token=data.get(CONF_RELAY_TOKEN, ""),
            wait_timeout=int(data.get(CONF_WAIT_TIMEOUT, 120)),
            wait_poll=float(data.get(CONF_WAIT_POLL, 1.0)),
            cwd=str(data.get(CONF_CWD, "")),
            model=str(data.get(CONF_MODEL, "")),
            approval_policy=str(data.get(CONF_APPROVAL_POLICY, "never")),
            sandbox_mode=str(data.get(CONF_SANDBOX_MODE, "danger-full-access")),
        )

    async def _relay_post(
        self,
        cfg: _RelayConfig,
        path: str,
        body: dict[str, Any],
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if cfg.relay_token:
            headers["Authorization"] = f"Bearer {cfg.relay_token}"
        url = f"{cfg.relay_url}{path}"
        async with httpx.AsyncClient(timeout=max(20, cfg.wait_timeout + 20)) as client:
            resp = await client.post(url, headers=headers, json=body, params=params)
        if resp.status_code >= 400:
            raise RuntimeError(f"Relay {path} failed HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except Exception as err:
            raise RuntimeError(f"Relay {path} returned invalid JSON") from err

    async def _relay_get(
        self,
        cfg: _RelayConfig,
        path: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if cfg.relay_token:
            headers["Authorization"] = f"Bearer {cfg.relay_token}"
        url = f"{cfg.relay_url}{path}"
        async with httpx.AsyncClient(timeout=max(15, cfg.wait_timeout + 15)) as client:
            resp = await client.get(url, headers=headers, params=params)
        if resp.status_code >= 400:
            raise RuntimeError(f"Relay {path} failed HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except Exception as err:
            raise RuntimeError(f"Relay {path} returned invalid JSON") from err

    async def _poll_for_agent_message(self, cfg: _RelayConfig, thread_id: str, timeout_s: int) -> str:
        deadline = self.hass.loop.time() + max(3, timeout_s)
        while self.hass.loop.time() < deadline:
            read = await self._relay_get(
                cfg,
                f"/threads/{thread_id}",
                params={"includeTurns": "true"},
            )
            text = _extract_last_agent_message(read)
            if text:
                return text
            await asyncio.sleep(max(0.2, cfg.wait_poll))
        return ""


def _extract_last_agent_message(payload: dict[str, Any]) -> str:
    thread_read = payload.get("threadRead") if isinstance(payload.get("threadRead"), dict) else payload
    thread = thread_read.get("thread") if isinstance(thread_read, dict) else None
    turns = thread.get("turns") if isinstance(thread, dict) else None
    if not isinstance(turns, list):
        return ""
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        for item in reversed(items):
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
                # Fallback for shapes where text is provided as content chunks.
                content = item.get("content")
                if isinstance(content, list):
                    parts: list[str] = []
                    for c in content:
                        if isinstance(c, dict):
                            t = c.get("text")
                            if isinstance(t, str) and t:
                                parts.append(t)
                    joined = "\n".join(parts).strip()
                    if joined:
                        return joined
    return ""


def _extract_ha_speech(response_obj: dict[str, Any]) -> str:
    speech = response_obj.get("speech")
    if not isinstance(speech, dict):
        return ""
    plain = speech.get("plain")
    if isinstance(plain, dict):
        text = plain.get("speech")
        if isinstance(text, str) and text.strip():
            return text.strip()
    ssml = speech.get("ssml")
    if isinstance(ssml, dict):
        text = ssml.get("speech")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def _sandbox_mode_to_turn_policy(mode: str) -> dict[str, Any]:
    if mode == "danger-full-access":
        return {"type": "dangerFullAccess"}
    if mode == "read-only":
        return {"type": "readOnly"}
    return {"type": "workspaceWrite"}
