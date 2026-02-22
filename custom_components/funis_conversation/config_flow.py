"""Config flow for Funis conversation integration."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from .const import (
    CONF_APPROVAL_POLICY,
    CONF_CWD,
    CONF_MODEL,
    CONF_NAME,
    CONF_RELAY_TOKEN,
    CONF_RELAY_URL,
    CONF_SANDBOX_MODE,
    CONF_WAIT_POLL,
    CONF_WAIT_TIMEOUT,
    DEFAULT_APPROVAL_POLICY,
    DEFAULT_NAME,
    DEFAULT_RELAY_URL,
    DEFAULT_SANDBOX_MODE,
    DEFAULT_WAIT_POLL,
    DEFAULT_WAIT_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def _validate_relay(
    relay_url: str,
    relay_token: str,
    timeout_s: int,
) -> None:
    url = f"{relay_url.rstrip('/')}/health"
    headers: dict[str, str] = {}
    if relay_token:
        headers["Authorization"] = f"Bearer {relay_token}"
    async with httpx.AsyncClient(timeout=max(5, timeout_s)) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code >= 400:
        raise ValueError(f"Relay health check failed: HTTP {resp.status_code}")


class FunisConversationConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Funis conversation."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=_schema())

        errors: dict[str, str] = {}
        try:
            await _validate_relay(
                relay_url=user_input[CONF_RELAY_URL],
                relay_token=user_input.get(CONF_RELAY_TOKEN, ""),
                timeout_s=user_input[CONF_WAIT_TIMEOUT],
            )
        except httpx.RequestError as err:
            _LOGGER.warning("Relay connection failed: %s", err)
            errors["base"] = "cannot_connect"
        except ValueError as err:
            _LOGGER.warning("Relay validation error: %s", err)
            errors["base"] = "invalid_relay"
        except Exception:  # pragma: no cover - safety
            _LOGGER.exception("Unexpected exception during config flow")
            errors["base"] = "unknown"

        if errors:
            return self.async_show_form(step_id="user", data_schema=_schema(user_input), errors=errors)

        await self.async_set_unique_id("funis_conversation_default")
        self._abort_if_unique_id_configured()

        title = user_input.get(CONF_NAME, DEFAULT_NAME)
        return self.async_create_entry(title=title, data=user_input)

def _schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    data = defaults or {}
    return vol.Schema(
        {
            vol.Optional(CONF_NAME, default=data.get(CONF_NAME, DEFAULT_NAME)): str,
            vol.Optional(CONF_RELAY_URL, default=data.get(CONF_RELAY_URL, DEFAULT_RELAY_URL)): str,
            vol.Optional(CONF_RELAY_TOKEN, default=data.get(CONF_RELAY_TOKEN, "")): str,
            vol.Optional(CONF_WAIT_TIMEOUT, default=data.get(CONF_WAIT_TIMEOUT, DEFAULT_WAIT_TIMEOUT)): vol.All(
                vol.Coerce(int), vol.Range(min=10, max=900)
            ),
            vol.Optional(CONF_WAIT_POLL, default=data.get(CONF_WAIT_POLL, DEFAULT_WAIT_POLL)): vol.All(
                vol.Coerce(float), vol.Range(min=0.2, max=5.0)
            ),
            vol.Optional(CONF_CWD, default=data.get(CONF_CWD, "")): str,
            vol.Optional(CONF_MODEL, default=data.get(CONF_MODEL, "")): str,
            vol.Optional(
                CONF_APPROVAL_POLICY,
                default=data.get(CONF_APPROVAL_POLICY, DEFAULT_APPROVAL_POLICY),
            ): vol.In(["untrusted", "on-failure", "on-request", "never"]),
            vol.Optional(
                CONF_SANDBOX_MODE,
                default=data.get(CONF_SANDBOX_MODE, DEFAULT_SANDBOX_MODE),
            ): vol.In(["read-only", "workspace-write", "danger-full-access"]),
        }
    )
