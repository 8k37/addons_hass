"""VK Bot integration: config entry setup."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PEER, CONF_URL, DOMAIN
from .coordinator import VKBotCoordinator
from .vkbot_api import VKBotApiClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["notify", "sensor"]


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
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
