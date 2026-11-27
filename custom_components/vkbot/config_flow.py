"""Config flow to add a VK Bot integration instance."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_PEER, CONF_URL, DEFAULT_NAME, DOMAIN
from .vkbot_api import VKBotApiClient, VKBotApiError

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL, default="http://192.168.88.254:8099"): str,
        vol.Optional(CONF_PEER, default=""): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict) -> dict:
    """Validate the user input by probing the API."""
    url = data[CONF_URL]
    client = VKBotApiClient(hass, url)
    try:
        await client.async_get_stats()
    except VKBotApiError as err:
        raise ValueError("cannot_connect") from err
    return {"title": DEFAULT_NAME, "url": url}


class VKBotConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the VK Bot config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except ValueError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception in vkbot config flow")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info["url"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=info["title"],
                    data={"url": info["url"], "peer": user_input.get(CONF_PEER, "")},
                )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )
