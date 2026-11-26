#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VK Bot для Home Assistant.

Очередь сообщений (текст / фото / видео) с персистентностью в /data/vkbot,
HTTP API для автоматизаций HA и выгрузка очереди в сенсоры HA.

Пример вызова из автоматизации HA (rest_command):
    vkbot_send:
      url: http://a0d7b954-vkbot:8099/send
      method: POST
      payload: '{"text": "...", "photos": [...], "videos": [...], "peer_id": 0}'
      headers:
        Content-Type: application/json

Или через shell_command:
    curl -s -X POST http://a0d7b954-vkbot:8099/send \
      -H 'Content-Type: application/json' \
      -d '{"text":"...", "peer_id":2000000003}'
"""

import json
import os
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# ----------------------------------------------------------------------------
# Конфигурация из options аддона (env-переменные от S6 with-contenv)
# ----------------------------------------------------------------------------

VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
API_VERSION = os.environ.get("API_VERSION", "5.199").strip()
DEFAULT_PEER_ID = int(os.environ.get("DEFAULT_PEER_ID", "0") or 0)
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8099") or 8099)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").lower()
RETRY_LIMIT = int(os.environ.get("RETRY_LIMIT", "5") or 5)
RETRY_BACKOFF = int(os.environ.get("RETRY_BACKOFF_SECONDS", "30") or 30)

DATA_DIR = "/data/vkbot"
QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
_THRESHOLD = _LEVELS.get(LOG_LEVEL, 20)


def log(msg: str, level: str = "info") -> None:
    if _LEVELS.get(level, 20) >= _THRESHOLD:
        print(f"[{level}] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


# ----------------------------------------------------------------------------
# Персистентная очередь
# ----------------------------------------------------------------------------

class MessageQueue:
    """Очередь сообщений с сохранением на диск при каждом изменении."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.items: list[dict[str, Any]] = []
        self.seq = 0
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.items = data.get("items", [])
            self.seq = int(data.get("seq", 0))
            log(f"queue restored: {len(self.items)} pending")
        except FileNotFoundError:
            log("queue file missing, starting empty")
        except Exception as e:  # повреждённый файл — не падаем, стартуем с пустой
            log(f"queue file corrupted ({e}); starting empty", "error")
            self.items = []

    def _save_locked(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"seq": self.seq, "items": self.items},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def push(self, text: str, photos: list[str], videos: list[str],
             peer_id: int) -> dict[str, Any]:
        with self.lock:
            self.seq += 1
            item = {
                "id": int(time.time()) * 1000 + self.seq,
                "text": text or "",
                "photos": photos or [],
                "videos": videos or [],
                "peer_id": int(peer_id or DEFAULT_PEER_ID),
                "attempts": 0,
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_error": "",
            }
            self.items.append(item)
            self._save_locked()
        return item

    def pop(self) -> dict[str, Any] | None:
        with self.lock:
            if self.items:
                return self.items.pop(0)
        return None

    def requeue_front(self, item: dict[str, Any], error: str) -> bool:
        """Вернуть сообщение в голову очереди. False — лимит ретраев исчерпан."""
        with self.lock:
            item["attempts"] += 1
            item["last_error"] = error[:300]
            if item["attempts"] >= RETRY_LIMIT:
                log(f"msg {item['id']} dropped after {item['attempts']} "
                    f"attempts: {error}", "error")
                self._save_locked()
                return False
            self.items.insert(0, item)
            self._save_locked()
        return True

    def mark_done(self, item: dict[str, Any]) -> None:
        with self.lock:
            self._save_locked()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "pending": len(self.items),
                "oldest": self.items[0]["created"] if self.items else "",
                "items": [
                    {"id": i["id"], "text": (i["text"] or "")[:80],
                     "photos": len(i["photos"]), "videos": len(i["videos"]),
                     "peer_id": i["peer_id"], "attempts": i["attempts"],
                     "last_error": i["last_error"]}
                    for i in self.items[:20]
                ],
            }


# ----------------------------------------------------------------------------
# VK API
# ----------------------------------------------------------------------------

class VkApi:
    BASE = "https://api.vk.com/method/"

    def __init__(self, token: str, version: str):
        self.token = token
        self.version = version

    def call(self, method: str, **params) -> Any:
        params["access_token"] = self.token
        params["v"] = self.version
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(self.BASE + method, data=data)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = json.loads(resp.read().decode())
                if "error" in body:
                    raise RuntimeError(f"VK error {body['error'].get('error_code')}: "
                                       f"{body['error'].get('error_msg')}")
                return body.get("response")
            except urllib.error.HTTPError as e:
                if 500 <= e.code < 600 and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise
            except (urllib.error.URLError, OSError):
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise

    @staticmethod
    def upload_file(url: str, field: str, path: str,
                    extra: dict[str, str] | None = None) -> Any:
        """multipart/form-data upload одного файла на VK upload server."""
        boundary = "----vkbot%s" % int(time.time() * 1000)
        fname = os.path.basename(path)
        with open(path, "rb") as f:
            payload = f.read()
        ctype = "video/mp4" if fname.lower().endswith(
            (".mp4", ".mov", ".mkv", ".avi", ".webm")) else "application/octet-stream"
        parts = []
        for k, v in (extra or {}).items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"'
                f"\r\n\r\n{v}\r\n".encode())
        parts.append(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
             f'filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n').encode()
            + payload + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode())

    # -- high level ----------------------------------------------------------

    def photos_attach(self, peer_id: int, paths: list[str]) -> list[str]:
        """Загрузка фото в личку/группу, возвращает attachment-строки."""
        if not paths:
            return []
        target = "2000000003" if peer_id >= 2000000000 else str(peer_id)
        up = self.call("photos.getMessagesUploadServer", peer_id=peer_id)
        uploads = up if isinstance(up, list) else [up]
        url = uploads[0]["upload_url"]
        attachments = []
        for p in paths:
            res = self.upload_file(url, "file1", p)
            body = res if isinstance(res, list) else [res]
            ph = body[0]
            saved = self.call(
                "photos.saveMessagesPhoto",
                photo=ph["photo"], server=ph["server"], hash=ph["hash"])
            saved = saved if isinstance(saved, list) else [saved]
            ph0 = saved[0]
            owner = ph0.get("owner_id")
            if target.startswith("-") and owner and not str(owner).startswith("-"):
                owner = -int(str(owner).lstrip("-"))  # в группе — owner_id группы
            attachments.append(f"photo{owner}_{ph0['id']}")
        return attachments

    def videos_attach(self, peer_id: int, paths: list[str]) -> list[str]:
        """Загрузка видео: video.save -> upload_url -> multipart PUT."""
        attachments = []
        for p in paths:
            name = os.path.splitext(os.path.basename(p))[0][:100]
            saved = self.call("video.save", name=name, group_id=None)
            saved = saved[0] if isinstance(saved, list) else saved
            self.upload_file(saved["upload_url"], "video_file", p)
            owner = saved.get("owner_id")
            attachments.append(f"video{owner}_{saved['video_id']}")
        return attachments

    def send(self, peer_id: int, text: str, attachments: list[str]) -> Any:
        return self.call("messages.send",
                         peer_id=peer_id,
                         message=text or "",
                         attachment=",".join(attachments),
                         random_id=int(time.time() * 1000) % 2**31)


# ----------------------------------------------------------------------------
# Воркер: берёт из очереди, грузит медиа, отправляет
# ----------------------------------------------------------------------------

class Worker(threading.Thread):
    def __init__(self, q: MessageQueue, vk: VkApi, state: dict):
        super().__init__(daemon=True, name="vkbot-worker")
        self.q = q
        self.vk = vk
        self.state = state

    def run(self) -> None:
        while True:
            item = self.q.pop()
            if item is None:
                time.sleep(2)
                continue
            log(f"processing msg {item['id']} "
                f"(text={bool(item['text'])}, photos={len(item['photos'])}, "
                f"videos={len(item['videos'])}, peer={item['peer_id']})")
            try:
                attachments: list[str] = []
                attachments += self.vk.photos_attach(item["peer_id"], item["photos"])
                attachments += self.vk.videos_attach(item["peer_id"], item["videos"])
                self.vk.send(item["peer_id"], item["text"], attachments)
                self.q.mark_done(item)
                self.state["sent"] = self.state.get("sent", 0) + 1
                self.state["last_sent"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self._save_state()
                log(f"msg {item['id']} sent")
            except Exception as e:
                log(f"msg {item['id']} failed: {e}", "error")
                self.q.requeue_front(item, str(e))
                time.sleep(RETRY_BACKOFF)

    def _save_state(self) -> None:
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            log(f"state save failed: {e}", "warning")


# ----------------------------------------------------------------------------
# HTTP API
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    queue: MessageQueue = None  # type: ignore[assignment]
    state: dict = {}

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/health":
            self._json(200, {"status": "ok",
                             "pending": self.queue.snapshot()["pending"]})
        elif path == "/queue":
            self._json(200, self.queue.snapshot())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path.split("?")[0] != "/send":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            payload = json.loads(self.rfile.read(length).decode())
        except Exception as e:
            self._json(400, {"error": f"bad json: {e}"})
            return
        text = str(payload.get("text", "") or "")
        photos = [str(p) for p in payload.get("photos", []) if p]
        videos = [str(v) for v in payload.get("videos", []) if v]
        peer_id = payload.get("peer_id") or DEFAULT_PEER_ID
        if not text and not photos and not videos:
            self._json(400, {"error": "empty message: need text, photos or videos"})
            return
        if not peer_id:
            self._json(400, {"error": "no peer_id given and default_peer_id is 0"})
            return
        for p in photos + videos:
            if not os.path.isfile(p):
                self._json(400, {"error": f"file not found: {p}"})
                return
        item = self.queue.push(text, photos, videos, int(peer_id))
        log(f"queued msg {item['id']} for peer {peer_id}")
        self._json(202, {"queued": True, "id": item["id"],
                         "pending": self.queue.snapshot()["pending"]})

    def log_message(self, fmt, *args):  # тишина в логах s6
        pass


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def main() -> None:
    if not VK_TOKEN:
        log("VK_TOKEN is empty — set vk_token in add-on options; exiting", "error")
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    state: dict = {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        pass

    queue = MessageQueue(QUEUE_FILE)
    vk = VkApi(VK_TOKEN, API_VERSION)

    Handler.queue = queue
    Handler.state = state
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)

    Worker(queue, vk, state).start()
    log(f"vkbot up: http 0.0.0.0:{HTTP_PORT}, queue={QUEUE_FILE}, "
        f"pending={queue.snapshot()['pending']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
