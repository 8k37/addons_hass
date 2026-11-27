"""Notify entity for VK Bot (new-style NotifyEntity)."""
from __future__ import annotations

import logging

from homeassistant.components.notify import NotifyEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .vkbot_api import VKBotApiError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up the VK Bot notify entity from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([VKBotNotifyEntity(entry, data["client"], data["peer"])])


class VKBotNotifyEntity(NotifyEntity):
    """Send notifications via the VK Bot addon."""

    _attr_has_entity_name = True
    _attr_name = "Уведомления"

    def __init__(self, entry: ConfigEntry, client, default_peer: str | None) -> None:
        self._attr_unique_id = f"{entry.entry_id}_notify"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "VK Bot",
            "manufacturer": "vkbot addon",
        }
        self._client = client
        self._default_peer = default_peer

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Send a message to the default peer (or all peers passed as title fallback)."""
        if not self._default_peer:
            _LOGGER.error("vkbot: no peer configured in the integration settings")
            return
        text = f"{title}\n{message}" if title else message
        try:
            await self._client.async_send(self._default_peer, text)
        except VKBotApiError as err:
            _LOGGER.error("vkbot: failed to send: %s", err)
