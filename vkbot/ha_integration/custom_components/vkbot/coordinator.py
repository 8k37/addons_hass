"""Data update coordinator for VK Bot stats."""
from __future__ import annotations

import datetime
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import UPDATE_INTERVAL_SECONDS
from .vkbot_api import VKBotApiClient, VKBotApiError

_LOGGER = logging.getLogger(__name__)


class VKBotCoordinator(DataUpdateCoordinator):
    """Poll the addon API for counters and queue stats."""

    def __init__(self, hass: HomeAssistant, client: VKBotApiClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="vkbot",
            update_interval=datetime.timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self._client = client

    async def _async_update_data(self) -> dict:
        try:
            data = await self._client.async_get_stats()
        except VKBotApiError as err:
            raise UpdateFailed(str(err)) from err
        return {
            "counters": data.get("counters", {}),
            "queue": data.get("queue", {}),
        }
