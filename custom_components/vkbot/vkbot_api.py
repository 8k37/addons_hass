"""VK Bot API client (aiohttp)."""
from __future__ import annotations

import asyncio
import logging

import aiohttp

_LOGGER = logging.getLogger(__name__)


class VKBotApiError(Exception):
    """Raised when the addon API is unreachable or returns an error."""


class VKBotApiClient:
    """Small async client for the VK Bot addon HTTP API."""

    def __init__(self, hass, url: str) -> None:
        self._hass = hass
        self._url = url.rstrip("/")

    async def async_get_stats(self) -> dict:
        """Return {'counters': {...}, 'queue': {...}} from /api/stats."""
        return await self._request("/api/stats")

    async def async_send(self, peer: str, text: str, photo=None, video=None) -> dict:
        """Send a message (optionally with media URL) via /api/send."""
        payload = {"peer": str(peer), "text": text}
        if photo:
            payload["photo"] = photo
        if video:
            payload["video"] = video
        return await self._request("/api/send", method="post", json=payload)

    async def _request(self, path: str, method: str = "get", json=None) -> dict:
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with getattr(session, method)(self._url + path, json=json) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status != 200:
                        raise VKBotApiError(f"HTTP {resp.status}: {body}")
                    return body
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise VKBotApiError(f"request to {self._url}{path} failed: {err}") from err
