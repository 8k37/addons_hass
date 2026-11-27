"""VK Bot integration: config entry setup."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import CONF_PEER, CONF_URL, DOMAIN
from .coordinator import VKBotCoordinator
from .vkbot_api import VKBotApiClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["notify", "sensor"]

SERVICE_SEND = "send"
ATTR_MESSAGE = "message"
ATTR_PEER = "peer"
ATTR_PHOTO = "photo"
ATTR_VIDEO = "video"

SEND_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Optional(ATTR_PEER): cv.string,
        vol.Optional(ATTR_PHOTO): cv.string,
        vol.Optional(ATTR_VIDEO): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up VK Bot from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    client = VKBotApiClient(hass, entry.data[CONF_URL])
    # verify the addon API is reachable; raise ConfigEntryNotReady on failure
    await client.async_get_stats()
    coordinator = VKBotCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
        "peer": entry.data.get(CONF_PEER),
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def async_handle_send(call: ServiceCall) -> None:
        """Handle the vkbot.send service call."""
        data = hass.data[DOMAIN][entry.entry_id]
        peer = call.data.get(ATTR_PEER) or data["peer"]
        if not peer:
            _LOGGER.error("vkbot.send: no peer configured and none provided")
            return
        try:
            await data["client"].async_send(
                peer,
                call.data[ATTR_MESSAGE],
                photo=call.data.get(ATTR_PHOTO),
                video=call.data.get(ATTR_VIDEO),
            )
        except Exception as err:  # noqa: BLE001 - surface any API failure
            _LOGGER.error("vkbot.send failed: %s", err)

    hass.services.async_register(
        DOMAIN, SERVICE_SEND, async_handle_send, schema=SEND_SERVICE_SCHEMA
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        hass.services.async_remove(DOMAIN, SERVICE_SEND)
    return unload_ok
