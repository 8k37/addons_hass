#!/usr/bin/env python3
"""VK-бот для Home Assistant с персистентной очередью сообщений.

Возможности:
- Отправка текста, фото и видео в VK
- Персистентная очередь на SQLite: сообщения переживают перезапуск аддона
- Ретраи с экспоненциальной задержкой
- HTTP API: POST /send, GET /queue, POST /flush
"""
import json
import mimetypes
import os
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

# --- Конфигурация из env (задаётся через options аддона) ---
VK_TOKEN = os.environ.get("VK_TOKEN", "")
API_PORT = int(os.environ.get("API_PORT", "8099"))
WORKERS = int(os.environ.get("WORKERS", "2"))
RETRY_LIMIT = int(os.environ.get("RETRY_LIMIT", "5"))
RETRY_BASE_DELAY = int(os.environ.get("RETRY_BASE_DELAY", "10"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info")

DATA_DIR = "/data"
DB_PATH = os.path.join(DATA_DIR, "queue.db")
API_BASE = "https://api.vk.com/method"
API_VERSION = "5.199"

_LOG_LOCK = threading.Lock()


def log(level: str, msg: str) -> None:
    order = {"trace": 0, "debug": 1, "info": 2, "warning": 3, "error": 4, "fatal": 5}
    if order.get(level, 2) >= order.get(LOG_LEVEL, 2):
        with _LOG_LOCK:
            print(f"[{level.upper()}] {msg}", flush=True)


# --------------------------------------------------------------------------
# База данных: персистентная очередь
# --------------------------------------------------------------------------

class QueueDB:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS queue (
                id TEXT PRIMARY KEY,
                peer TEXT NOT NULL,
                text TEXT,
                photo TEXT,
                video TEXT,
                random_id INTEGER,
                attempts INTEGER DEFAULT 0,
                next_attempt_at REAL DEFAULT 0,
                status TEXT DEFAULT 'pending',
                error TEXT,
                created_at REAL
            )
        """)
        self._conn.commit()

    def add(self, peer: str, text, photo, video) -> str:
        task_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO queue (id, peer, text, photo, video, random_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, peer, text, photo, video, int(time.time() * 1000) % 10**9, time.time()),
            )
            self._conn.commit()
        return task_id

    def due_tasks(self, limit: int = 10) -> list:
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, peer, text, photo, video, random_id, attempts"
                " FROM queue WHERE status = 'pending' AND next_attempt_at <= ?"
                " ORDER BY created_at LIMIT ?",
                (now, limit),
            ).fetchall()
        return [
            {"id": r[0], "peer": r[1], "text": r[2], "photo": r[3],
             "video": r[4], "random_id": r[5], "attempts": r[6]}
            for r in rows
        ]

    def mark_sent(self, task_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM queue WHERE id = ?", (task_id,))
            self._conn.commit()

    def mark_retry(self, task_id: str, attempts: int, error: str) -> None:
        if attempts >= RETRY_LIMIT:
            with self._lock:
                self._conn.execute(
                    "UPDATE queue SET status = 'failed', error = ?, attempts = ? WHERE id = ?",
                    (error[:500], attempts, task_id),
                )
                self._conn.commit()
            log("error", f"Задача {task_id} провалилась после {attempts} попыток: {error}")
        else:
            delay = RETRY_BASE_DELAY * (2 ** attempts)
            with self._lock:
                self._conn.execute(
                    "UPDATE queue SET attempts = ?, next_attempt_at = ?, error = ? WHERE id = ?",
                    (attempts, time.time() + delay, error[:500], task_id),
                )
                self._conn.commit()

    def stats(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM queue GROUP BY status"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def flush(self) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM queue")
            self._conn.commit()
        return cur.rowcount

    def failed(self) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, peer, text, error FROM queue WHERE status = 'failed'"
            ).fetchall()
        return [{"id": r[0], "peer": r[1], "text": r[2], "error": r[3]} for r in rows]


DB = QueueDB(DB_PATH)

# --------------------------------------------------------------------------
# VK API
# --------------------------------------------------------------------------

class VkApi:
    def __init__(self, token: str):
        self.token = token
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._min_interval = 0.35  # ~3 запроса/сек на токен

    def call(self, method: str, **params) -> dict:
        # Троттлинг: не чаще 1 запроса в 0.35с
        with self._lock:
            wait = self._min_interval - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()
        params["access_token"] = self.token
        params["v"] = API_VERSION
        resp = requests.post(f"{API_BASE}/{method}", data=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise VkError(data["error"])
        return data["response"]


class VkError(Exception):
    def __init__(self, error: dict):
        self.code = error.get("error_code")
        super().__init__(f"{self.code}: {error.get('error_msg')}")


def vk_upload_photo(api: VkApi, peer_id: str, source: str) -> str:
    """Загрузка фото: возвращает attachment вида photo<owner>_<id>."""
    peer_num = peer_id.split("_")[-1] if "_" in peer_id else peer_id
    up = api.call("photos.getMessagesUploadServer", peer_id=int(peer_num))
    url = up["upload_url"]

    if source.startswith("http"):
        raw = requests.get(source, timeout=60).content
        fname = os.path.basename(source.split("?")[0]) or "photo.jpg"
    else:
        with open(source, "rb") as f:
            raw = f.read()
        fname = os.path.basename(source)

    ctype = mimetypes.guess_type(fname)[0] or "image/jpeg"
    r = requests.post(url, files={"photo": (fname, raw, ctype)}, timeout=120)
    r.raise_for_status()
    d = r.json()
    if not (d.get("photo") and d.get("server") and d.get("hash")):
        raise VkError({"error_code": -1, "error_msg": f"Плохой ответ upload-сервера: {d}"})
    saved = api.call("photos.saveMessagesPhoto", photo=d["photo"], server=d["server"], hash=d["hash"])
    item = saved[0]
    return f"photo{item['owner_id']}_{item['id']}"


def vk_upload_video(api: VkApi, source: str) -> str:
    """Загрузка видео: возвращает attachment вида video<owner>_<id>."""
    save = api.call("video.save")
    url = save["upload_url"]

    if source.startswith("http"):
        raw = requests.get(source, timeout=300).content
        fname = os.path.basename(source.split("?")[0]) or "video.mp4"
    else:
        with open(source, "rb") as f:
            raw = f.read()
        fname = os.path.basename(source)

    ctype = mimetypes.guess_type(fname)[0] or "video/mp4"
    r = requests.post(url, files={"video_file": (fname, raw, ctype)}, timeout=600)
    r.raise_for_status()
    d = r.json()
    return f"video{d['owner_id']}_{d['video_id']}"


# --------------------------------------------------------------------------
# Воркеры
# --------------------------------------------------------------------------

STOP = threading.Event()


def worker(api: VkApi) -> None:
    while not STOP.is_set():
        tasks = DB.due_tasks(limit=1)
        if not tasks:
            STOP.wait(1.0)
            continue
        task = tasks[0]
        try:
            attachments = []
            if task["photo"]:
                attachments.append(vk_upload_photo(api, task["peer"], task["photo"]))
            if task["video"]:
                attachments.append(vk_upload_video(api, task["video"]))
            params = {"peer_id": task["peer"], "random_id": task["random_id"]}
            if task["text"]:
                params["message"] = task["text"]
            if attachments:
                params["attachment"] = ",".join(attachments)
            api.call("messages.send", **params)
            DB.mark_sent(task["id"])
            log("info", f"Отправлено в {task['peer']} (задача {task['id'][:8]})")
        except VkError as e:
            if e.code == 9:  # flood control
                log("warning", "Flood control, ждём 3с")
                time.sleep(3)
            DB.mark_retry(task["id"], task["attempts"] + 1, str(e))
        except Exception as e:
            log("error", f"Ошибка отправки: {e}")
            DB.mark_retry(task["id"], task["attempts"] + 1, str(e))


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/queue":
            self._json(200, {"stats": DB.stats(), "failed": DB.failed()})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path == "/send":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception as e:
                self._json(400, {"ok": False, "error": f"bad json: {e}"})
                return
            peer = str(data.get("peer", "")).strip()
            if not peer:
                self._json(400, {"ok": False, "error": "peer is required"})
                return
            text = data.get("text")
            photo = data.get("photo")
            video = data.get("video")
            if not text and not photo and not video:
                self._json(400, {"ok": False, "error": "text, photo or video required"})
                return
            task_id = DB.add(peer, text, photo, video)
            self._json(200, {"ok": True, "task_id": task_id})
        elif self.path == "/flush":
            n = DB.flush()
            self._json(200, {"ok": True, "deleted": n})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def log_message(self, fmt, *args):
        log("debug", f"HTTP {self.address_string()} {fmt % args}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    if not VK_TOKEN:
        log("fatal", "VK_TOKEN не задан — укажите vk_token в настройках аддона")
        return 1

    api = VkApi(VK_TOKEN)
    try:
        me = api.call("users.get")[0]
        log("info", f"VK-токен действителен, бот: {me.get('first_name')} {me.get('last_name')} (id {me.get('id')})")
    except Exception as e:
        log("error", f"Проверка токена не удалась: {e}")

    for i in range(WORKERS):
        threading.Thread(target=worker, args=(api,), daemon=True, name=f"worker-{i}").start()
    log("info", f"Запущено воркеров: {WORKERS}, порт API: {API_PORT}")

    server = ThreadingHTTPServer(("0.0.0.0", API_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        STOP.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
