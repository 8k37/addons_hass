#!/usr/bin/env python3
"""Клиент Home Assistant: отправка событий бота в HA через SUPERVISOR_TOKEN."""
import json
import os

import requests

SUPERVISOR_URL = "http://supervisor/core/api"


def fire_event(event_type: str, payload: dict) -> None:
    """Отправить событие в HA (доступно, если homeassistant_api: true)."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return
    try:
        requests.post(
            f"{SUPERVISOR_URL}/events/{event_type}",
            headers={"Authorization": f"Bearer {token}"},
            data=json.dumps(payload),
            timeout=5,
        )
    except Exception:
        pass  # события не критичны для работы бота
