"""Sensor platform: VK Bot statistics from the addon API."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

COUNTER_LABELS = {
    "sent_total": ("Отправлено всего", "mdi:send"),
    "sent_text": ("Отправлено текстов", "mdi:message-text"),
    "sent_photo": ("Отправлено фото", "mdi:image"),
    "sent_video": ("Отправлено видео", "mdi:video"),
}

QUEUE_LABELS = {
    "pending": ("В очереди", "mdi:clock-outline"),
    "failed": ("Ошибок", "mdi:alert-circle"),
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up VK Bot sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    entities = []
    for key, (label, icon) in COUNTER_LABELS.items():
        entities.append(VKBotCounterSensor(entry, coordinator, key, label, icon))
    for status, (label, icon) in QUEUE_LABELS.items():
        entities.append(VKBotQueueSensor(entry, coordinator, status, label, icon))
    async_add_entities(entities)


class _VKBotSensorBase(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, entry: ConfigEntry, coordinator) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "VK Bot",
            "manufacturer": "vkbot addon",
        }


class VKBotCounterSensor(_VKBotSensorBase):
    """A cumulative counter (sent_total / sent_text / ...)."""

    def __init__(self, entry, coordinator, key: str, label: str, icon: str) -> None:
        super().__init__(entry, coordinator)
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_counter_{key}"
        self._attr_name = label
        self._attr_icon = icon

    @property
    def native_value(self):
        return self.coordinator.data["counters"].get(self._key, 0)


class VKBotQueueSensor(_VKBotSensorBase):
    """Current queue size by status (pending / failed)."""

    def __init__(self, entry, coordinator, status: str, label: str, icon: str) -> None:
        super().__init__(entry, coordinator)
        self._status = status
        self._attr_unique_id = f"{entry.entry_id}_queue_{status}"
        self._attr_name = label
        self._attr_icon = icon

    @property
    def native_value(self):
        return self.coordinator.data["queue"].get(self._status, 0)
