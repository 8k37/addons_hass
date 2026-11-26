#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Пример клиента для HTTP API аддона VK Bot.

Добавьте в configuration.yaml:

rest_command:
  vkbot_send:
    url: http://a0d7b954-vkbot:8099/send
    method: POST
    headers:
      Content-Type: application/json
    payload: >-
      {"text": "{{ text }}", "peer_id": {{ peer_id }},
       "photos": {{ photos | default([]) | tojson }},
       "videos": {{ videos | default([]) | tojson }}}

И в автоматизации:

action: rest_command.vkbot_send
data:
  text: "Дверь открыта"
  peer_id: 2000000003
  photos: ["/media/door.jpg"]
  videos: []
"""

import json
import sys
import urllib.request

API = "http://127.0.0.1:8099"


def send(text="", photos=None, videos=None, peer_id=0):
    payload = json.dumps({
        "text": text,
        "photos": photos or [],
        "videos": videos or [],
        "peer_id": peer_id,
    }).encode()
    req = urllib.request.Request(
        API + "/send", data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(resp.read().decode())


if __name__ == "__main__":
    send(text=sys.argv[1] if len(sys.argv) > 1 else "test from ha_client")
