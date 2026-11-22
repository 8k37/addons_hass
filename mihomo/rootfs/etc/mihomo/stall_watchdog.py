#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==============================================================================
# Сторож «зависшего» трафика для mihomo (Python, замена bash-версии).
#
# Пока есть активные подключения, но входящий (download) трафик не растет
# быстрее STALL_FLOOR_BPS байт/с STALL_TIMEOUT секунд подряд (upload не
# учитывается) — тест скорости кандидатов группы через mixed-порт и
# переключение на первый узел, дотягивающий до MIN_SPEED_BPS (0 — всегда
# брать самого быстрого). Если через EARLY_SAMPLE секунд прогноз скорости
# уже >= порога — тест узла прекращается досрочно.
#
# Все служебные запросы (REST API, обновление подписок, состояние) идут
# НАПРЯМУЮ, без прокси — сторож работает даже когда VPN полностью мертв.
# Через прокси (mixed-порт) идет только замер скорости узла — это намеренно.
#
# Переменные окружения (задаются в run):
#   STALL_WATCHDOG   0/1            — выключатель сторожа
#   STALL_TIMEOUT    сек            — порог простоя (default 10)
#   STALL_FLOOR_BPS  байт/с         — нижняя планка входящей скорости (default 30720)
#   MIN_SPEED_BPS    байт/с         — порог «достаточной» скорости (default 512000, 0 = максимум)
#   TEST_TIMEOUT     сек            — таймаут замера скорости одного узла (default 6)
#   EARLY_SAMPLE     сек            — интервал прогноза скорости (default 1, 0 = без прогноза)
#   SPEED_URL        url            — файл для закачки
#   MIXED_PORT       порт           — mixed-порт mihomo
#   API_HOST / API_PORT / API_SECRET — REST API mihomo
#   HC_URL           url            — url для group delay после переключения
#   GROUP_NAME       имя            — группа-селектор, которой управляет сторож
#   CACHE_TTL        сек            — сколько хранить результаты замеров (default 600, 0 = без кэша)
#   EMERGENCY_AFTER  срабатываний   — после стольких срабатываний подряд без результата
#                                    узел меняется вслепую, без замера (default 10, 0 = выкл)
# ==============================================================================

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


# ANSI-цвета: выделяем все строки сторожа в журнале,
# чтобы наши доработки было видно с первого взгляда.
_LEVEL_COLORS = {
    "INFO": "96",      # ярко-голубой — служебные сообщения сторожа
    "WARNING": "93",   # ярко-жёлтый — срабатывания/переключения
    "ERROR": "91",     # ярко-красный — ошибки
    "OK": "92",        # ярко-зелёный — успешные действия
}


def _c(code, text):
    if os.environ.get("NO_COLOR"):
        return text
    return "\033[%sm%s\033[0m" % (code, text)


def log(level, msg):
    color = _LEVEL_COLORS.get(level, "97")
    tag = _c(color, "[stall-watchdog]")
    lvl = _c("2;" + color, "[%s]" % level)
    print("%s %s %s" % (tag, lvl, _c(color, msg)), flush=True)


def env_str(name, default=""):
    v = os.environ.get(name, "").strip().strip('"').strip("'")
    return v or default


def env_int(name, default):
    digits = "".join(ch for ch in os.environ.get(name, "") if ch.isdigit())
    return int(digits) if digits else default


STALL_ENABLED = env_str("STALL_WATCHDOG", "1").lower() in ("1", "true")
STALL_TIMEOUT = env_int("STALL_TIMEOUT", 10)
STALL_FLOOR_BPS = env_int("STALL_FLOOR_BPS", 30720)
MIN_SPEED_BPS = env_int("MIN_SPEED_BPS", 512000)
TEST_TIMEOUT = env_int("TEST_TIMEOUT", 6)
EARLY_SAMPLE = env_int("EARLY_SAMPLE", 1)
SPEED_URL = env_str("SPEED_URL", "https://speed.cloudflare.com/__down?bytes=2000000")
MIXED_PORT = env_int("MIXED_PORT", 7890)
API = "http://%s:%d" % (env_str("API_HOST", "127.0.0.1"), env_int("API_PORT", 9090))
API_SECRET = env_str("API_SECRET", "")
HC_URL = env_str("HC_URL", "https://www.gstatic.com/generate_204")
GROUP_NAME = env_str("GROUP_NAME", "PROXY")
CACHE_FILE = env_str("CACHE_FILE", "/etc/mihomo/.stall_speed_cache")
CACHE_TTL = env_int("CACHE_TTL", 600)
EMERGENCY_AFTER = env_int("EMERGENCY_AFTER", 10)
STATE_FILE = env_str("STATE_FILE", "/etc/mihomo/.stall_state")

SUB_LOCK_FILE = "/etc/mihomo/.sub_refresh_lock"
SUB_LAST_FILE = "/etc/mihomo/.sub_refresh_last"
SUB_FAIL_FILE = "/etc/mihomo/.sub_refresh_fails"

GROUP_NAMES = {
    "PROXY", "AUTO", "FASTEST", "DIRECT", "REJECT", "GLOBAL", "PROVIDER",
    "PASS", "REJECT-DROP", "COMPATIBLE", "dtg", "Name",
}


# ---------------------------------------------------------------- REST API ---
def api(method, path, data=None, timeout=5):
    """Запрос к REST API mihomo. Напрямую, без прокси. Ошибки — исключения."""
    req = urllib.request.Request(API + path, method=method)
    if data is not None:
        req.data = json.dumps(data).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    if API_SECRET:
        req.add_header("Authorization", "Bearer " + API_SECRET)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    return json.loads(body) if body else {}


def api_group_read():
    """Читает группу GROUP_NAME. None — API мертв (порт/секрет);
    dict — ответ API (группа может быть и пустой)."""
    global GROUP_NAME
    try:
        return api("GET", "/proxies/" + urllib.parse.quote(GROUP_NAME))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return None
    except Exception:
        return None
    # 404: группа переименована — автодетект первого Selector через API
    try:
        allp = api("GET", "/proxies")
    except Exception:
        return None
    for name, info in (allp.get("proxies") or {}).items():
        if name == "GLOBAL" or info.get("type") != "Selector":
            continue
        if name != GROUP_NAME:
            log("WARNING", "Группа «%s» не найдена в API — использую селектор «%s»"
                % (GROUP_NAME, name))
            GROUP_NAME = name
        try:
            return api("GET", "/proxies/" + urllib.parse.quote(GROUP_NAME))
        except Exception:
            return None
    return {}


# ------------------------------------------------------------- замер скорости ---
def measure_node(node):
    """Скорость узла в байт/с через mixed-порт (0 = не смог).
    Ранний выход: если через EARLY_SAMPLE секунд прогноз уже >= MIN_SPEED_BPS."""
    try:
        api("PUT", "/proxies/" + urllib.parse.quote(GROUP_NAME), {"name": node})
    except Exception:
        log("WARNING", "Не удалось переключиться на %s — скорость 0" % node)
        return 0

    handler = urllib.request.ProxyHandler({
        "http": "http://127.0.0.1:%d" % MIXED_PORT,
        "https": "http://127.0.0.1:%d" % MIXED_PORT,
    })
    opener = urllib.request.build_opener(handler)
    start = time.monotonic()
    total = 0
    try:
        with opener.open(SPEED_URL, timeout=TEST_TIMEOUT) as resp:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                elapsed = time.monotonic() - start
                if EARLY_SAMPLE > 0 and MIN_SPEED_BPS > 0 and elapsed >= EARLY_SAMPLE:
                    projected = int(total / elapsed)
                    if projected >= MIN_SPEED_BPS:
                        log("INFO", "Прогноз через %.0fs уже >= порога (%d байт/с) — "
                            "тест узла %s досрочно завершен" % (elapsed, projected, node))
                        return projected
    except Exception:
        pass
    elapsed = time.monotonic() - start
    if elapsed < 0.5:
        elapsed = 0.5
    return int(total / elapsed)


def switch_and_retest(node):
    """Переключить группу на узел и обновить задержки группы."""
    try:
        api("PUT", "/proxies/" + urllib.parse.quote(GROUP_NAME), {"name": node})
    except Exception:
        pass
    try:
        url = urllib.parse.quote(HC_URL, safe="")
        api("GET", "/group/%s/delay?url=%s" % (urllib.parse.quote(GROUP_NAME), url),
            timeout=30)
    except Exception:
        pass


# ------------------------------------------------------------------- кэш ---
def cache_load():
    """speed|timestamp|node -> {node: (speed, ts)}; протухшие записи выбрасываются."""
    cache = {}
    now = int(time.time())
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split("|")
                if len(parts) != 3:
                    continue
                speed, ts, node = parts
                if not speed.isdigit() or not ts.isdigit() or not node:
                    continue
                speed, ts = int(speed), int(ts)
                if speed <= 0:
                    continue
                if CACHE_TTL > 0 and (now - ts) > CACHE_TTL:
                    continue
                cache[node] = (speed, ts)
    except Exception:
        pass
    return cache


def cache_save(cache):
    if CACHE_TTL <= 0:
        return
    tmp = CACHE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for node, (speed, ts) in cache.items():
                f.write("%d|%d|%s\n" % (speed, ts, node))
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass


# ------------------------------------------------------------- состояние ---
def state_read():
    """(серия аварийных срабатываний, последние аварийные выборы через '|')"""
    counter, recent = 0, ""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            lines = [l.rstrip("\n") for l in f]
        if lines and lines[0].isdigit():
            counter = int(lines[0])
        if len(lines) > 1:
            recent = lines[1]
    except Exception:
        pass
    return counter, recent


def state_write(counter, recent):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write("%d\n%s\n" % (counter, recent))
    except Exception:
        pass


def emergency_pick(nodes, cur, recent):
    """Узел, отличный от текущего и от последних аварийных выборов, без замера."""
    recent_list = [x for x in recent.split("|") if x]
    for skip_recent in (True, False):
        for node in nodes:
            if not node or node in GROUP_NAMES or node == cur:
                continue
            if skip_recent and node in recent_list:
                continue
            return node
    return None


# --------------------------------------- восстановление подписки (напрямую) ---
def read_int_file(path, default=0):
    try:
        with open(path, "r", encoding="utf-8") as f:
            v = f.read().strip()
        return int(v) if v.isdigit() else default
    except Exception:
        return default


def sub_refresh_running():
    try:
        return (time.time() - os.stat(SUB_LOCK_FILE).st_mtime) < 300
    except FileNotFoundError:
        return False
    except Exception:
        return False


def sub_refresh_worker(names):
    """Обновляет proxy-providers через API. Скачивает само ядро mihomo —
    провайдеры настроены с proxy: DIRECT, поэтому обновление не зависит
    от рабочего прокси и не фейлится при мертвом VPN."""
    try:
        with open(SUB_LOCK_FILE, "w", encoding="utf-8") as f:
            f.write("1")
    except Exception:
        pass
    ok = 0
    for name in names:
        try:
            api("PUT", "/providers/proxies/" + urllib.parse.quote(name), timeout=30)
            log("INFO", "Провайдер %s: обновлен" % name)
            ok += 1
        except Exception:
            log("WARNING", "Провайдер %s: не удалось обновить" % name)
    log("INFO", "PROXY пуст — запущено обновление подписок (%d провайдеров)" % ok)
    try:
        os.remove(SUB_LOCK_FILE)
    except Exception:
        pass


def sub_recover():
    """Вызывается при пустом PROXY. Защиты: lock от параллельного запуска,
    не чаще раза в 10 секунд (после 5 неудач — раз в минуту)."""
    now = int(time.time())
    fails = read_int_file(SUB_FAIL_FILE)
    last = read_int_file(SUB_LAST_FILE)
    cooldown = 60 if fails >= 5 else 10
    if now - last < cooldown:
        return
    try:
        with open(SUB_LAST_FILE, "w", encoding="utf-8") as f:
            f.write(str(now))
    except Exception:
        pass

    if sub_refresh_running():
        log("INFO", "Обновление подписок уже выполняется — жду завершения")
        time.sleep(5)
        return

    try:
        providers = api("GET", "/providers/proxies").get("providers") or {}
    except Exception:
        providers = {}
    names = [n for n, p in providers.items() if p.get("vehicleType") != "Compatible"]
    if not names:
        log("WARNING", "PROXY пуст, но proxy-providers в конфиге нет — "
            "восстанавливать нечего, проверь подписку/конфиг")
        return

    threading.Thread(target=sub_refresh_worker, args=(names,), daemon=True).start()
    time.sleep(10)  # дать mihomo скачать и применить подписку

    g = api_group_read()
    nodes = ((g or {}).get("proxies") or {}).get(GROUP_NAME) or {}
    if nodes.get("all"):
        log("INFO", "Подписка восстановлена: в %s %d узлов" % (GROUP_NAME, len(nodes["all"])))
        try:
            with open(SUB_FAIL_FILE, "w", encoding="utf-8") as f:
                f.write("0")
        except Exception:
            pass
    else:
        fails += 1
        try:
            with open(SUB_FAIL_FILE, "w", encoding="utf-8") as f:
                f.write(str(fails))
        except Exception:
            pass
        log("WARNING", "Подписка пуста и после обновления (неудачных попыток: %d)" % fails)


# ------------------------------------------------------------------ цикл ---
def main():
    if not STALL_ENABLED:
        log("INFO", "Сторож простоя отключен (stall_watchdog=false)")
        return
    log("INFO", "Сторож простоя запущен: timeout=%ds, floor=%d байт/с, min_speed=%d байт/с, "
        "test_timeout=%ds, early_sample=%ds, url=%s"
        % (STALL_TIMEOUT, STALL_FLOOR_BPS, MIN_SPEED_BPS, TEST_TIMEOUT, EARLY_SAMPLE, SPEED_URL))

    stall = 0
    last_total = None
    cache = cache_load()

    while True:
        time.sleep(1)

        try:
            conn = api("GET", "/connections")
        except Exception:
            stall = 0
            last_total = None
            continue

        conns = conn.get("connections") or []
        # входящий считаем только по подключениям через прокси (последний
        # элемент chains не DIRECT): прямые соединения (например, GEOIP,RU)
        # не должны маскировать зависший прокси-узел
        proxied = [c for c in conns if ((c.get("chains") or [""])[-1] != "DIRECT")]
        nconn = len(proxied)
        if nconn == 0:
            stall = 0
            last_total = None
            continue

        now_total = sum(int(c.get("download") or 0) for c in proxied)
        if last_total is None:
            last_total = now_total
            continue
        delta = now_total - last_total
        last_total = now_total

        if STALL_FLOOR_BPS > 0:
            alive = delta >= STALL_FLOOR_BPS
        else:
            alive = delta > 0
        if alive:
            stall = 0
            continue
        stall += 1
        if stall < STALL_TIMEOUT:
            continue
        stall = 0
        last_total = None

        log("WARNING", "Входящий трафик (download) ниже %d байт/с %d секунд подряд "
            "при %d прокси-подключениях — тест скорости узлов"
            % (STALL_FLOOR_BPS, STALL_TIMEOUT, nconn))

        g = api_group_read()
        if g is None:
            log("WARNING", "API не ответил на /proxies/%s (порт/секрет?) — пропускаю цикл"
                % GROUP_NAME)
            continue
        pdata = (g.get("proxies") or {}).get(GROUP_NAME) or {}
        nodes = pdata.get("all") or []
        cur = pdata.get("now") or ""
        if not nodes:
            log("WARNING", "В группе %s нет ни одного узла — запускаю проверку/обновление подписки"
                % GROUP_NAME)
            sub_recover()
            continue

        now_ts = int(time.time())
        cache = {n: v for n, v in cache.items()
                 if v[0] > 0 and (CACHE_TTL <= 0 or (now_ts - v[1]) <= CACHE_TTL)}

        # Аварийный режим: серия срабатываний без результата — меняем узел вслепую
        counter, recent = state_read()
        if EMERGENCY_AFTER > 0 and counter >= EMERGENCY_AFTER:
            pick = emergency_pick(nodes, cur, recent)
            if pick:
                log("WARNING", "АВАРИЙНОЕ ПЕРЕКЛЮЧЕНИЕ: %d срабатываний подряд без результата — "
                    "ставлю %s без теста" % (EMERGENCY_AFTER, pick))
                switch_and_retest(pick)
                rl = [x for x in recent.split("|") if x]
                if pick not in rl:
                    rl.append(pick)
                state_write(0, "|".join(rl[-5:]))
                continue

        # Быстрый путь: в кэше есть свежий подходящий узел — быстрый замер
        # с ранним выходом стоит недорого; не прошел — полный спринт
        if CACHE_TTL > 0 and MIN_SPEED_BPS > 0:
            fresh = [(v[1], n) for n, v in cache.items()
                     if n != cur and n not in GROUP_NAMES and v[0] >= MIN_SPEED_BPS]
            if fresh:
                cached = max(fresh)[1]
                speed = measure_node(cached)
                cache[cached] = (speed, int(time.time()))
                if speed >= MIN_SPEED_BPS:
                    log("INFO", "Кэш: %s подтвержден (%d байт/с) — переключаюсь" % (cached, speed))
                    switch_and_retest(cached)
                    state_write(0, recent)
                    continue
                log("WARNING", "Кэш: %s больше не дотягивает (%d байт/с) — удаляю из кэша, полный спринт"
                    % (cached, speed))
                cache.pop(cached, None)

        # порядок теста: сначала узлы, которые по кэшу были быстрыми
        order = sorted(nodes, key=lambda n: -cache.get(n, (0, 0))[0])
        best_node, best_speed, chosen = "", 0, ""
        for node in order:
            if not node or node in GROUP_NAMES or node == cur:
                continue
            speed = measure_node(node)
            log("INFO", "Тест скорости: %s — %d байт/с" % (node, speed))
            cache[node] = (speed, int(time.time()))
            if speed > best_speed:
                best_speed, best_node = speed, node
            if MIN_SPEED_BPS > 0 and speed >= MIN_SPEED_BPS:
                chosen = node
                log("INFO", "Подходит, тест остальных не нужен: %s" % node)
                break
        cache_save(cache)

        if chosen:
            node = chosen
            state_write(0, recent)  # нормальное срабатывание: серия прервана
        elif best_node:
            node = best_node
            counter, _ = state_read()
            state_write(counter + 1, recent)
            if MIN_SPEED_BPS > 0 and best_speed < MIN_SPEED_BPS:
                log("WARNING", "Никто не дотянул до %d байт/с — переключаю на самого быстрого: "
                    "%s (%d байт/с) [серия: %d]" % (MIN_SPEED_BPS, node, best_speed, counter + 1))
        else:
            log("WARNING", "Кандидатов для теста не нашлось")
            counter, _ = state_read()
            state_write(counter + 1, recent)
            continue

        log("INFO", "Переключаю %s на %s" % (GROUP_NAME, node))
        switch_and_retest(node)
        last_total = None  # следующий цикл считает простой заново


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
