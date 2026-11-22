#!/command/with-contenv bashio
# ==============================================================================
# Сторож «зависшего» трафика для mihomo.
# Пока есть активные подключения, но суммарный трафик не растёт
# stall_timeout секунд подряд — тест скорости кандидатов группы PROXY
# через mixed-порт и переключение на первый узел, дотягивающий
# до stall_min_speed КБ/с (0 — всегда брать самого быстрого).
#
# Ускорение: если через EARLY_SAMPLE секунд темп закачки уже даёт
# прогноз >= порога — замер прекращается досрочно, узел берётся сразу.
#
# Переменные окружения (задаются в run):
#   STALL_WATCHDOG   0/1            — выключатель сторожа
#   STALL_TIMEOUT    сек            — порог простоя (default 10)
#   MIN_SPEED_KB     КБ/с           — порог «достаточной» скорости (default 500, 0 = максимум)
#   TEST_TIMEOUT     сек            — таймаут замера скорости одного узла (default 6)
#   EARLY_SAMPLE     сек            — интервал прогноза скорости (default 1, 0 = без прогноза)
#   SPEED_URL        url            — файл для закачки
#   MIXED_PORT       порт           — mixed-порт mihomo
#   API_HOST / API_PORT             — REST API mihomo
# ==============================================================================

STALL_ENABLED="${STALL_WATCHDOG:-1}"
STALL_ENABLED="${STALL_ENABLED//\"/}"
STALL_TIMEOUT="${STALL_TIMEOUT:-10}"
STALL_TIMEOUT="${STALL_TIMEOUT//\"/}"
STALL_TIMEOUT="${STALL_TIMEOUT//[^0-9]/}"
[ -n "$STALL_TIMEOUT" ] || STALL_TIMEOUT=10

MIN_SPEED_KB="${MIN_SPEED_KB:-500}"
MIN_SPEED_KB="${MIN_SPEED_KB//\"/}"
MIN_SPEED_KB="${MIN_SPEED_KB//[^0-9]/}"

TEST_TIMEOUT="${TEST_TIMEOUT:-6}"
TEST_TIMEOUT="${TEST_TIMEOUT//\"/}"
TEST_TIMEOUT="${TEST_TIMEOUT//[^0-9]/}"
[ -n "$TEST_TIMEOUT" ] || TEST_TIMEOUT=6

# Прогноз скорости по первым N секундам закачки (0 = качать до конца)
EARLY_SAMPLE="${EARLY_SAMPLE:-1}"
EARLY_SAMPLE="${EARLY_SAMPLE//\"/}"
EARLY_SAMPLE="${EARLY_SAMPLE//[^0-9]/}"
[ -n "$EARLY_SAMPLE" ] || EARLY_SAMPLE=1

SPEED_URL="${SPEED_URL:-https://speed.cloudflare.com/__down?bytes=2000000}"
SPEED_URL="${SPEED_URL//\"/}"
MIXED_PORT="${MIXED_PORT:-7890}"
MIXED_PORT="${MIXED_PORT//[^0-9]/}"
[ -n "$MIXED_PORT" ] || MIXED_PORT=7890
API_HOST="${API_HOST:-127.0.0.1}"
API_HOST="${API_HOST//\"/}"
API_PORT="${API_PORT:-9090}"
API_PORT="${API_PORT//[^0-9]/}"
[ -n "$API_PORT" ] || API_PORT=9090
API="http://${API_HOST}:${API_PORT}"

if [ "$STALL_ENABLED" != "1" ] && [ "$STALL_ENABLED" != "true" ]; then
    bashio::log.info "Сторож простоя отключен (stall_watchdog=false)"
    exit 0
fi

bashio::log.info "Сторож простоя запущен: timeout=${STALL_TIMEOUT}s, min_speed=${MIN_SPEED_KB}КБ/с, test_timeout=${TEST_TIMEOUT}s, early_sample=${EARLY_SAMPLE}s, url=${SPEED_URL}"

STALL_COUNT=0
LAST_TOTAL=""

api_get() { curl -fsS --max-time 5 "$API$1" 2>/dev/null; }

# Скорость узла в байт/с через mixed-порт (0 = не смог).
# Если MIN_SPEED_KB > 0 и EARLY_SAMPLE > 0: через EARLY_SAMPLE секунд
# считается прогноз (байты/прошедшее время); если прогноз уже >= порога —
# закачка прерывается досрочно, возвращается прогноз.
measure_node() {
    local node="$1" out pid size elapsed projected
    # переключаем PROXY на узел и качаем тестовый файл
    if ! curl -fsS --max-time 5 -X PUT "$API/proxies/PROXY" \
        -d "{\"name\":\"$node\"}" >/dev/null 2>&1; then
        bashio::log.warning "Не удалось переключиться на $node — скорость 0"
        echo 0; return
    fi
    out=$(mktemp)
    curl -fsS --max-time "$TEST_TIMEOUT" -x "http://127.0.0.1:${MIXED_PORT}" "$SPEED_URL" -o "$out" 2>/dev/null &
    pid=$!

    if [ "$EARLY_SAMPLE" -gt 0 ] && [ "$MIN_SPEED_KB" -gt 0 ]; then
        # ранний прогноз: проверяем темп каждые EARLY_SAMPLE секунд
        while kill -0 "$pid" 2>/dev/null; do
            sleep "$EARLY_SAMPLE"
            kill -0 "$pid" 2>/dev/null || break
            size=$(stat -c%s "$out" 2>/dev/null || echo 0)
            projected=$((size / EARLY_SAMPLE))
            if [ "$projected" -ge $((MIN_SPEED_KB * 1024)) ]; then
                kill "$pid" 2>/dev/null
                wait "$pid" 2>/dev/null
                rm -f "$out"
                bashio::log.info "Прогноз через ${EARLY_SAMPLE}s уже >= порога (${projected} байт/с) — тест узла $node досрочно завершен"
                echo "$projected"
                return
            fi
        done
        # узел не дотянул по прогнозу — докачиваем до конца таймаута для точного замера
    fi

    wait "$pid" 2>/dev/null
    size=$(stat -c%s "$out" 2>/dev/null || echo 0)
    rm -f "$out"
    speed=$((size / TEST_TIMEOUT))
    echo "$speed"
}

switch_and_retest() {
    local node="$1"
    curl -fsS --max-time 5 -X PUT "$API/proxies/PROXY" -d "{\"name\":\"$node\"}" >/dev/null 2>&1 || true
    curl -fsS --max-time 30 "$API/group/AUTO/delay?url=$(printf '%s' 'https://www.gstatic.com/generate_204' | sed 's/:/%3A/; s@/@%2Fg')" >/dev/null 2>&1 || true
}

is_group_name() {
    case "$1" in
        PROXY|AUTO|FASTEST|DIRECT|REJECT|GLOBAL|PROVIDER|PASS|REJECT-DROP|COMPATIBLE|dtg|Name) return 0 ;;
        *) return 1 ;;
    esac
}

while true; do
    sleep 1

    conn_json=$(api_get "/connections") || { STALL_COUNT=0; continue; }

    now_total=$(printf '%s' "$conn_json" | sed -n 's/.*"downloadTotal":\([0-9]*\),"uploadTotal":\([0-9]*\).*/\1\2/p')
    [ -n "$now_total" ] || { STALL_COUNT=0; continue; }

    nconn=$(printf '%s' "$conn_json" | grep -o '"id"' | wc -l)
    [ "$nconn" -gt 0 ] 2>/dev/null || { STALL_COUNT=0; continue; }

    if [ -z "$LAST_TOTAL" ] || [ "$now_total" != "$LAST_TOTAL" ]; then
        LAST_TOTAL="$now_total"
        STALL_COUNT=0
        continue
    fi

    STALL_COUNT=$((STALL_COUNT + 1))
    if [ "$STALL_COUNT" -lt "$STALL_TIMEOUT" ]; then
        continue
    fi
    STALL_COUNT=0

    bashio::log.warning "Трафик стоит ${STALL_TIMEOUT}s при $nconn подключениях — тест скорости узлов"

    # кандидаты: узлы группы PROXY
    nodes=$(api_get "/proxies/PROXY" 2>/dev/null | sed -n 's/.*"all":\[\(.*\)\],"now".*/\1/p' | tr -d '"' | tr ',' '\n') || nodes=""
    [ -n "$nodes" ] || { bashio::log.warning "Список кандидатов пуст"; continue; }

    cur=$(api_get "/proxies/PROXY" 2>/dev/null | sed -n 's/.*"now":"\([^"]*\)".*/\1/p')

    best_node=""
    best_speed=0
    chosen=""

    while IFS= read -r node; do
        [ -n "$node" ] || continue
        is_group_name "$node" && continue
        [ "$node" = "$cur" ] && continue

        speed=$(measure_node "$node")
        bashio::log.info "Тест скорости: $node — ${speed} байт/с"

        if [ "$speed" -gt "$best_speed" ]; then
            best_speed=$speed
            best_node=$node
        fi

        if [ "$MIN_SPEED_KB" -gt 0 ] && [ "$speed" -ge $((MIN_SPEED_KB * 1024)) ]; then
            chosen=$node
            bashio::log.info "Подходит, тест остальных не нужен: $node"
            break
        fi
    done <<< "$nodes"

    if [ -n "$chosen" ]; then
        node=$chosen
    elif [ -n "$best_node" ]; then
        node=$best_node
        if [ "$MIN_SPEED_KB" -gt 0 ] && [ "$best_speed" -lt $((MIN_SPEED_KB * 1024)) ]; then
            bashio::log.warning "Никто не дотянул до ${MIN_SPEED_KB} КБ/с — переключаю на самого быстрого: $node ($best_speed байт/с)"
        fi
    else
        bashio::log.warning "Кандидатов для теста не нашлось"
        continue
    fi

    bashio::log.info "Переключаю PROXY на $node"
    switch_and_retest "$node"
    LAST_TOTAL=""   # чтобы следующий цикл считал простой заново
done
