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
_HOME_ASSISTANT_ENTITY_ID = "conversation.home_assistant"


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
        self._last_reply_by_conv: dict[str, str] = {}

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
            try:
                ha_result = await self._ha_builtin_process(user_input, conv_id)
            except Exception as err:
                _LOGGER.debug("HA built-in routing unavailable, continuing with Funis relay: %s", err)
                ha_result = None
            if ha_result is not None:
                speech = _extract_ha_speech_from_result(ha_result)
                if speech:
                    self._last_reply_by_conv[conv_id] = speech
                return ha_result

            previous_reply = self._last_reply_by_conv.get(conv_id, "")
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
            text = _extract_new_agent_message(out, previous_reply)
            if not text:
                # Some relay/app-server paths complete before agent text is fully materialized.
                text = await self._poll_for_agent_message(
                    cfg,
                    thread_id,
                    previous_reply=previous_reply,
                    timeout_s=min(cfg.wait_timeout, 20),
                )
            if not text:
                text = "I completed the request, but no assistant message was returned."
            else:
                self._last_reply_by_conv[conv_id] = text
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
        result: dict[str, Any] | None = None
        for agent_id in self._builtin_agent_id_candidates():
            data: dict[str, Any] = {
                "text": user_input.text,
                "language": user_input.language,
                "agent_id": agent_id,
                "conversation_id": conv_id,
            }
            try:
                call_result = await self.hass.services.async_call(
                    "conversation",
                    "process",
                    data,
                    blocking=True,
                    return_response=True,
                    context=user_input.context,
                )
            except Exception as err:
                msg = str(err).lower()
                if "invalid agent" in msg or "agent_id" in msg:
                    _LOGGER.debug("Skipping invalid built-in agent id '%s': %s", agent_id, err)
                    continue
                raise

            if isinstance(call_result, dict):
                result = call_result
                break

        if result is None:
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

    def _builtin_agent_id_candidates(self) -> list[str]:
        """Return likely-valid IDs for the built-in Home Assistant conversation agent."""
        candidates: list[str] = []
        home_agent_const = getattr(getattr(conversation, "const", object()), "HOME_ASSISTANT_AGENT", None)
        if isinstance(home_agent_const, str) and home_agent_const:
            candidates.append(home_agent_const)

        try:
            manager = conversation.get_agent_manager(self.hass)
            for info in manager.async_get_agent_info():
                agent_id = getattr(info, "id", None)
                if isinstance(agent_id, str) and agent_id:
                    candidates.append(agent_id)
                try:
                    agent = manager.async_get_agent(agent_id)
                except Exception:
                    continue
                registry_entry = getattr(agent, "registry_entry", None)
                entity_id = getattr(registry_entry, "entity_id", None)
                if isinstance(entity_id, str) and entity_id:
                    candidates.append(entity_id)
                name = str(getattr(info, "name", "")).lower()
                if name == "home assistant":
                    if isinstance(agent_id, str) and agent_id:
                        candidates.append(agent_id)
                    if isinstance(entity_id, str) and entity_id:
                        candidates.append(entity_id)
        except Exception as err:
            _LOGGER.debug("Unable to enumerate conversation agents for built-in fallback: %s", err)

        candidates.extend([_HOME_ASSISTANT_ENTITY_ID, "home_assistant"])
        # Preserve order, remove duplicates/empty values.
        ordered_unique: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            cid = candidate.strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            ordered_unique.append(cid)
        return ordered_unique

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

    async def _poll_for_agent_message(
        self,
        cfg: _RelayConfig,
        thread_id: str,
        previous_reply: str,
        timeout_s: int,
    ) -> str:
        deadline = self.hass.loop.time() + max(3, timeout_s)
        while self.hass.loop.time() < deadline:
            read = await self._relay_get(
                cfg,
                f"/threads/{thread_id}",
                params={"includeTurns": "true"},
            )
            text = _extract_new_agent_message(read, previous_reply)
            if text:
                return text
            await asyncio.sleep(max(0.2, cfg.wait_poll))
        return ""


def _extract_last_agent_message(payload: dict[str, Any], latest_turn_only: bool = False) -> str:
    thread_read = payload.get("threadRead") if isinstance(payload.get("threadRead"), dict) else payload
    thread = thread_read.get("thread") if isinstance(thread_read, dict) else None
    turns = thread.get("turns") if isinstance(thread, dict) else None
    if not isinstance(turns, list):
        return ""
    turns_iter = [turns[-1]] if latest_turn_only and turns else reversed(turns)
    for turn in turns_iter:
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


def _extract_new_agent_message(payload: dict[str, Any], previous_reply: str) -> str:
    # Restrict to the newest turn so we don't replay finalized text from an older turn.
    text = _extract_last_agent_message(payload, latest_turn_only=True)
    if not text:
        return ""
    if previous_reply and text.strip() == previous_reply.strip():
        return ""
    return text


def _extract_ha_speech_from_result(result: ConversationResult) -> str:
    try:
        speech = getattr(result.response, "speech", None)
        if isinstance(speech, dict):
            plain = speech.get("plain")
            if isinstance(plain, dict):
                text = plain.get("speech")
                if isinstance(text, str):
                    return text.strip()
        data = getattr(result.response, "as_dict", None)
        if callable(data):
            out = data()
            if isinstance(out, dict):
                return _extract_ha_speech(out)
    except Exception:
        return ""
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
