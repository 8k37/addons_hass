#!/command/with-contenv bashio
# ==============================================================================
# Сторож «зависшего» трафика для mihomo.
# Пока есть активные подключения, но входящий (download) трафик
# не растёт stall_timeout секунд подряд (upload не учитывается) —
# тест скорости кандидатов группы PROXY
# через mixed-порт и переключение на первый узел, дотягивающий
# до stall_min_speed КБ/с (0 — всегда брать самого быстрого).
#
# Ускорение: если через EARLY_SAMPLE секунд темп закачки уже даёт
# прогноз >= порога — замер прекращается досрочно, узел берётся сразу.
#
# Переменные окружения (задаются в run):
#   STALL_WATCHDOG   0/1            — выключатель сторожа
#   STALL_TIMEOUT    сек            — порог простоя (default 10)
#   STALL_FLOOR_KB   КБ/с           — нижняя планка входящей скорости (default 30)
#   MIN_SPEED_KB     КБ/с           — порог «достаточной» скорости (default 500, 0 = максимум)
#   TEST_TIMEOUT     сек            — таймаут замера скорости одного узла (default 6)
#   EARLY_SAMPLE     сек            — интервал прогноза скорости (default 1, 0 = без прогноза)
#   SPEED_URL        url            — файл для закачки
#   MIXED_PORT       порт           — mixed-порт mihomo
#   API_HOST / API_PORT             — REST API mihomo
#   CACHE_TTL       сек            — сколько хранить результаты замеров (default 600, 0 = без кэша)
#   EMERGENCY_AFTER срабатываний   — после стольких срабатываний подряд без результата
#                                    узел меняется вслепую, без замера (default 10, 0 = выкл)
# ==============================================================================

STALL_ENABLED="${STALL_WATCHDOG:-1}"
STALL_ENABLED="${STALL_ENABLED//\"/}"
STALL_TIMEOUT="${STALL_TIMEOUT:-10}"
STALL_TIMEOUT="${STALL_TIMEOUT//\"/}"
STALL_TIMEOUT="${STALL_TIMEOUT//[^0-9]/}"
[ -n "$STALL_TIMEOUT" ] || STALL_TIMEOUT=10

MIN_SPEED_KB="${MIN_SPEED_KB:-500}"

# Нижняя планка входящей скорости (КБ/с): если при активных подключениях
# download медленнее этого — считается простоем (0 = только полный ноль трафика)
STALL_FLOOR_KB="${STALL_FLOOR_KB:-30}"
STALL_FLOOR_KB="${STALL_FLOOR_KB//\"/}"
STALL_FLOOR_KB="${STALL_FLOOR_KB//[^0-9]/}"
[ -n "$STALL_FLOOR_KB" ] || STALL_FLOOR_KB=30

TEST_TIMEOUT="${TEST_TIMEOUT:-6}"
MIN_SPEED_KB="${MIN_SPEED_KB//\"/}"
MIN_SPEED_KB="${MIN_SPEED_KB//[^0-9]/}"
[ -n "$MIN_SPEED_KB" ] || MIN_SPEED_KB=500

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

# секрет REST API (если задан в конфиге) — без него все запросы вернут 401 молча
API_SECRET="$(yq -r '.secret // ""' "${CONFIG_FILE}" 2>/dev/null || true)"
if [ -n "$API_SECRET" ]; then
    CURL_AUTH=(-H "Authorization: Bearer ${API_SECRET}")
else
    CURL_AUTH=()
fi

# Кэш результатов замеров скорости: файл «speed|timestamp|node».
# Позволяет при повторном срабатывании не тратить время на спринт по узлам:
# кандидат из кэша сначала быстро перепроверяется, и только если он до сих
# пор дотягивает до порога — используется без полного спринта.
CACHE_FILE="${CACHE_FILE:-/etc/mihomo/.stall_speed_cache}"
CACHE_TTL="${CACHE_TTL:-600}"
CACHE_TTL="${CACHE_TTL//\"/}"
CACHE_TTL="${CACHE_TTL//[^0-9]/}"
[ -n "$CACHE_TTL" ] || CACHE_TTL=600

# Аварийное переключение: если EMERGENCY_AFTER срабатываний подряд трафик
# так и не ожил (замеры врут, а реальные соединения не идут) — хватит мерить:
# ставим другой узел сразу, без замера. Серия хранится в файле-состоянии.
EMERGENCY_AFTER="${EMERGENCY_AFTER:-10}"
EMERGENCY_AFTER="${EMERGENCY_AFTER//\"/}"
EMERGENCY_AFTER="${EMERGENCY_AFTER//[^0-9]/}"
[ -n "$EMERGENCY_AFTER" ] || EMERGENCY_AFTER=10

STATE_FILE="${STATE_FILE:-/etc/mihomo/.stall_state}"

state_get() {
    head -n1 "$STATE_FILE" 2>/dev/null | tr -dc '0-9'
}

state_get_recent() {
    tail -n1 "$STATE_FILE" 2>/dev/null
}

state_set() {
    printf '%s\n%s\n' "$1" "$(state_get_recent)" > "$STATE_FILE" 2>/dev/null || true
}

state_set_recent() {
    printf '%s\n%s\n' "$(state_get)" "$1" > "$STATE_FILE" 2>/dev/null || true
}

if [ "$STALL_ENABLED" != "1" ] && [ "$STALL_ENABLED" != "true" ]; then
    bashio::log.info "Сторож простоя отключен (stall_watchdog=false)"
    exit 0
fi

bashio::log.info "Сторож простоя запущен: timeout=${STALL_TIMEOUT}s, floor=${STALL_FLOOR_KB}КБ/с, min_speed=${MIN_SPEED_KB}КБ/с, test_timeout=${TEST_TIMEOUT}s, early_sample=${EARLY_SAMPLE}s, url=${SPEED_URL}"

STALL_COUNT=0
LAST_TOTAL=""

api_get() { curl -fsS --max-time 5 "${CURL_AUTH[@]}" "$API$1" 2>/dev/null; }

# Скорость узла в байт/с через mixed-порт (0 = не смог).
# Если MIN_SPEED_KB > 0 и EARLY_SAMPLE > 0: через EARLY_SAMPLE секунд
# считается прогноз (байты/прошедшее время); если прогноз уже >= порога —
# закачка прерывается досрочно, возвращается прогноз.
measure_node() {
    local node="$1" out pid size elapsed projected
    # переключаем PROXY на узел и качаем тестовый файл
    if ! curl -fsS --max-time 5 "${CURL_AUTH[@]}" -X PUT "$API/proxies/PROXY" \
        -d "{\"name\":\"$node\"}" >/dev/null 2>&1; then
        bashio::log.warning "Не удалось переключиться на $node — скорость 0"
        echo 0; return
    fi
    out=$(mktemp)
    local start=$(date +%s)
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
    # скорость по фактическому времени закачки: деление на полный TEST_TIMEOUT
    # занижало скорость узлов, скачавших файл раньше таймаута
    local elapsed=$(( $(date +%s) - start ))
    [ "$elapsed" -lt 1 ] && elapsed=1
    speed=$((size / elapsed))
    echo "$speed"
}

# Самый свежий узел из кэша, чья скорость >= порога и запись не старше TTL.
# Печатает имя узла; 0 и пусто — узел не найден.
cache_lookup() {
    [ "$CACHE_TTL" -gt 0 ] || return 1
    [ -f "$CACHE_FILE" ] || return 1
    local now speed ts node best_ts=0 best=""
    now=$(date +%s)
    while IFS='|' read -r speed ts node; do
        [ -n "$speed" ] && [ -n "$ts" ] && [ -n "$node" ] || continue
        [ "$speed" -gt 0 ] 2>/dev/null || continue
        [ $((now - ts)) -le "$CACHE_TTL" ] || continue
        [ "$node" = "$cur" ] && continue
        # при пороге 0 (режим «всегда максимум») кэшем не пользуемся —
        # там нужен полный замер каждого узла
        [ "$MIN_SPEED_KB" -gt 0 ] || return 1
        [ "$speed" -ge $((MIN_SPEED_KB * 1024)) ] || continue
        if [ "$ts" -gt "$best_ts" ]; then best_ts=$ts; best=$node; fi
    done < "$CACHE_FILE"
    [ -n "$best" ] && { printf '%s' "$best"; return 0; }
    return 1
}

# Дописать результат замера в кэш (speed байт/с, node).
# Предыдущие записи узла удаляются — одна актуальная запись на узел.
cache_save() {
    [ "$CACHE_TTL" -gt 0 ] || return 0
    local node="$2"
    if [ -f "$CACHE_FILE" ]; then
        awk -F'|' -v n="$node" 'NF==3 && $3 != n' "$CACHE_FILE" > "${CACHE_FILE}.tmp" 2>/dev/null \
            && mv "${CACHE_FILE}.tmp" "$CACHE_FILE"
    fi
    printf '%s|%s|%s\n' "$1" "$(date +%s)" "$node" >> "$CACHE_FILE" 2>/dev/null || true
}

# Удалить узел из кэша (например, если он перестал дотягивать до порога)
cache_remove() {
    [ -f "$CACHE_FILE" ] || return 0
    awk -F'|' -v n="$1" 'NF==3 && $3 != n' "$CACHE_FILE" > "${CACHE_FILE}.tmp" 2>/dev/null \
        && mv "${CACHE_FILE}.tmp" "$CACHE_FILE"
}

# Последняя сохранённая скорость узла (0 — записи нет)
cache_get_speed() {
    [ -f "$CACHE_FILE" ] || { echo 0; return; }
    awk -F'|' -v n="$1" '$3 == n {v=$1} END {print v + 0}' "$CACHE_FILE" 2>/dev/null || echo 0
}

# Выбросить протухшие записи кэша
cache_prune() {
    [ "$CACHE_TTL" -gt 0 ] || return 0
    [ -f "$CACHE_FILE" ] || return 0
    local now=$(date +%s)
    awk -F'|' -v now="$now" -v ttl="$CACHE_TTL" 'NF==3 && $1 ~ /^[0-9]+$/ && (now-$2)<=ttl' \
        "$CACHE_FILE" > "${CACHE_FILE}.tmp" 2>/dev/null && mv "${CACHE_FILE}.tmp" "$CACHE_FILE"
}

switch_and_retest() {
    local node="$1"
    curl -fsS --max-time 5 "${CURL_AUTH[@]}" -X PUT "$API/proxies/PROXY" -d "{\"name\":\"$node\"}" >/dev/null 2>&1 || true
    curl -fsS --max-time 30 "${CURL_AUTH[@]}" "$API/group/AUTO/delay?url=$(printf '%s' 'https://www.gstatic.com/generate_204' | sed 's/:/%3A/; s@/@%2Fg')" >/dev/null 2>&1 || true
}

# Аварийный выбор: узел, отличный от текущего и от последних аварийных,
# без какого-либо замера
emergency_pick() {
    local recent="$1" node pass
    for pass in 1 2; do
        while IFS= read -r node; do
            [ -n "$node" ] || continue
            is_group_name "$node" && continue
            [ "$node" = "$cur" ] && continue
            if [ "$pass" -eq 1 ]; then
                case "|$recent|" in *"|$node|"*) continue ;; esac
            fi
            printf '%s' "$node"; return 0
        done <<< "$nodes"
    done
    return 1
}

# --- Восстановление подписки при пустом PROXY --------------------------------
SUB_LOCK_FILE="/etc/mihomo/.sub_refresh_lock"
SUB_LAST_FILE="/etc/mihomo/.sub_refresh_last"
SUB_FAIL_FILE="/etc/mihomo/.sub_refresh_fails"

sub_refresh_running() {
    [ -f "$SUB_LOCK_FILE" ] || return 1
    local pid; pid=$(cat "$SUB_LOCK_FILE" 2>/dev/null)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

# Фоновое обновление всех proxy-providers (встроенный Compatible не трогаем)
sub_refresh_do() {
    (
        echo $$ > "$SUB_LOCK_FILE"
        provs=$(api_get "/providers/proxies" 2>/dev/null | jq -r '.providers | to_entries[] | select(.value.vehicleType != "Compatible") | .key' 2>/dev/null)
        if [ -z "$provs" ]; then
            bashio::log.warning "PROXY пуст, но proxy-providers в конфиге нет — восстанавливать нечего, проверь подписку/конфиг"
        else
            n=0
            while IFS= read -r p; do
                [ -n "$p" ] || continue
                n=$((n+1))
                if curl -fsS --max-time 30 "${CURL_AUTH[@]}" -X PUT "$API/providers/proxies/$p" >/dev/null 2>&1; then
                    bashio::log.info "Провайдер ${p}: обновлен"
                else
                    bashio::log.warning "Провайдер ${p}: не удалось обновить"
                fi
            done <<< "$provs"
            bashio::log.info "PROXY пуст — запущено обновление подписок (${n} провайдеров)"
        fi
        rm -f "$SUB_LOCK_FILE"
    ) &
}

# Вызывается при пустом PROXY. Защиты: lock от параллельного запуска,
# не чаще раза в минуту (после 5 неудач — раз в 5 минут).
sub_recover() {
    local now=$(date +%s)
    local fails=$(cat "$SUB_FAIL_FILE" 2>/dev/null); case "$fails" in ''|*[!0-9]*) fails=0;; esac
    local last=$(cat "$SUB_LAST_FILE" 2>/dev/null); case "$last" in ''|*[!0-9]*) last=0;; esac
    local cooldown=60
    [ "$fails" -ge 5 ] && cooldown=300
    [ $((now - last)) -lt "$cooldown" ] && return 0
    echo "$now" > "$SUB_LAST_FILE"

    if sub_refresh_running; then
        bashio::log.info "Обновление подписок уже выполняется — жду завершения"
        sleep 5
        return 0
    fi
    sub_refresh_do
    sleep 10   # дать mihomo скачать и применить подписку

    local g cnt
    g=$(api_get "/proxies/PROXY" 2>/dev/null || true)
    cnt=$(printf '%s' "$g" | jq -r '.proxies.PROXY.all // [] | length' 2>/dev/null)
    case "$cnt" in ''|*[!0-9]*) cnt=0;; esac
    if [ "$cnt" -gt 0 ]; then
        bashio::log.info "Подписка восстановлена: в PROXY ${cnt} узлов"
        echo 0 > "$SUB_FAIL_FILE"
        LAST_TOTAL=""
    else
        fails=$((fails+1))
        echo "$fails" > "$SUB_FAIL_FILE"
        bashio::log.warning "Подписка пуста и после обновления (неудачных попыток: ${fails})"
    fi
    return 0
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

    # считаем входящий только по подключениям, идущим через прокси
    # (последний элемент chains не DIRECT): прямые соединения — например,
    # правило GEOIP,RU — не должны маскировать зависший прокси-узел
    now_total=$(printf '%s' "$conn_json" | jq -r '[.connections[]? | select((.chains | last) != "DIRECT") | .download] | add // 0' 2>/dev/null)
    case "$now_total" in ''|*[!0-9]*) { STALL_COUNT=0; continue; } ;; esac

    nconn=$(printf '%s' "$conn_json" | jq -r '[.connections[]? | select((.chains | last) != "DIRECT")] | length' 2>/dev/null)
    case "$nconn" in ''|*[!0-9]*) { STALL_COUNT=0; continue; } ;; esac
    [ "$nconn" -gt 0 ] 2>/dev/null || { STALL_COUNT=0; continue; }

    if [ -z "$LAST_TOTAL" ]; then
        LAST_TOTAL="$now_total"
        STALL_COUNT=0
        continue
    fi

    delta=$((now_total - LAST_TOTAL))
    LAST_TOTAL="$now_total"

    # нижняя планка: входящий трафик должен идти быстрее STALL_FLOOR_KB КБ/с,
    # иначе даже «ползущий» счётчик считается простоем
    if [ "$delta" -lt 0 ] || [ "$delta" -ge $((STALL_FLOOR_KB * 1024)) ]; then
        STALL_COUNT=0
        # трафик ожил — серия подозрительных срабатываний прервана
        [ "$(state_get)" = "0" ] 2>/dev/null || state_set 0
        continue
    fi

    STALL_COUNT=$((STALL_COUNT + 1))
    if [ "$STALL_COUNT" -lt "$STALL_TIMEOUT" ]; then
        continue
    fi
    STALL_COUNT=0

    bashio::log.warning "Входящий трафик (download) стоит ${STALL_TIMEOUT}s при $nconn прокси-подключениях — тест скорости узлов"

    # PROXY пуст (подписка умерла/отдала пустой список) — пробуем перекачать
    sub_nodes=$(printf '%s' "$nodes" 2>/dev/null)
    group_json=$(api_get "/proxies/PROXY" 2>/dev/null || true)
    nodes=$(printf '%s' "$group_json" | jq -r '.proxies.PROXY.all // [] | .[]' 2>/dev/null)
    cur=$(printf '%s' "$group_json" | jq -r '.proxies.PROXY.now // ""' 2>/dev/null)
    [ -n "$nodes" ] || {
        bashio::log.warning "В группе PROXY нет ни одного узла — запускаю проверку/обновление подписки"
        sub_recover
        continue
    }

    # Аварийный режим: серия срабатываний без результата — меняем узел вслепую
    if [ "$EMERGENCY_AFTER" -gt 0 ] 2>/dev/null && [ "$(state_get)" -ge "$EMERGENCY_AFTER" ] 2>/dev/null; then
        recent=$(state_get_recent)
        if emerg_node=$(emergency_pick "$recent"); then
            bashio::log.warning "АВАРИЙНОЕ ПЕРЕКЛЮЧЕНИЕ: ${EMERGENCY_AFTER} срабатываний подряд без результата — ставлю ${emerg_node} без теста"
            switch_and_retest "$emerg_node"
            # помним последние 5 аварийных выборов — не прыгаем по кругу между двумя узлами
            _IFS=$IFS; IFS='|'; read -ra _rl <<< "$recent"; IFS=$_IFS
            case "|$recent|" in *"|$emerg_node|"*) : ;; *) _rl+=("$emerg_node") ;; esac
            [ "${#_rl[@]}" -gt 5 ] && _rl=("${_rl[@]: -5}")
            _IFS=$IFS; IFS='|'; recent="${_rl[*]}"; IFS=$_IFS
            state_set_recent "$recent"
            state_set 0
            LAST_TOTAL=""
            continue
        fi
    fi

    # кандидаты: узлы группы PROXY
    group_json=$(api_get "/proxies/PROXY" 2>/dev/null || true)
    nodes=$(printf '%s' "$group_json" | jq -r '.proxies.PROXY.all // [] | .[]' 2>/dev/null)
    cur=$(printf '%s' "$group_json" | jq -r '.proxies.PROXY.now // ""' 2>/dev/null)
    [ -n "$nodes" ] || { bashio::log.warning "Список кандидатов пуст (не удалось прочитать группу PROXY)"; continue; }

    # Быстрый путь: в кэше есть свежий подходящий узел — сначала быстрый замер
    # (ранний выход делает его дешёвым). Переключение «вслепую» убрано: без
    # перепроверки ватчер мог весь TTL переключаться на один и тот же узел,
    # который успел деградировать.
    cache_prune
    if cached=$(cache_lookup); then
        speed=$(measure_node "$cached")
        cache_save "$speed" "$cached"
        if [ "$speed" -ge $((MIN_SPEED_KB * 1024)) ]; then
            bashio::log.info "Кэш: ${cached} подтвержден (${speed} байт/с) — переключаюсь"
            switch_and_retest "$cached"
            state_set 0
            LAST_TOTAL=""
            continue
        fi
        bashio::log.warning "Кэш: ${cached} больше не дотягивает (${speed} байт/с) — удаляю из кэша, полный спринт"
        cache_remove "$cached"
    fi

    best_node=""
    best_speed=0
    chosen=""

    # порядок теста: сначала узлы, которые по кэшу были быстрыми (даже старая
    # запись лучше случайного порядка) — спринт заканчивается раньше
    nodes_sorted=$(while IFS= read -r n; do
        [ -n "$n" ] || continue
        printf '%s\t%s\n' "$(cache_get_speed "$n")" "$n"
    done <<< "$nodes" | sort -t$'\t' -rn -k1,1 | cut -f2-)
    [ -n "$nodes_sorted" ] || nodes_sorted="$nodes"

    while IFS= read -r node; do
        [ -n "$node" ] || continue
        is_group_name "$node" && continue
        [ "$node" = "$cur" ] && continue

        speed=$(measure_node "$node")
        bashio::log.info "Тест скорости: $node — ${speed} байт/с"
        cache_save "$speed" "$node"

        if [ "$speed" -gt "$best_speed" ]; then
            best_speed=$speed
            best_node=$node
        fi

        if [ "$MIN_SPEED_KB" -gt 0 ] && [ "$speed" -ge $((MIN_SPEED_KB * 1024)) ]; then
            chosen=$node
            bashio::log.info "Подходит, тест остальных не нужен: $node"
            break
        fi
    done <<< "$nodes_sorted"

    if [ -n "$chosen" ]; then
        node=$chosen
        state_set 0   # нормальное срабатывание: узел дотянул — серия прервана
    elif [ -n "$best_node" ]; then
        node=$best_node
        # подозрительное срабатывание: ни один узел не дотянул — копим серию
        state_set $(( $(state_get) + 1 ))
        if [ "$MIN_SPEED_KB" -gt 0 ] && [ "$best_speed" -lt $((MIN_SPEED_KB * 1024)) ]; then
            bashio::log.warning "Никто не дотянул до ${MIN_SPEED_KB} КБ/с — переключаю на самого быстрого: $node ($best_speed байт/с) [серия: $(state_get)]"
        fi
    else
        bashio::log.warning "Кандидатов для теста не нашлось"
        state_set $(( $(state_get) + 1 ))
        continue
    fi

    bashio::log.info "Переключаю PROXY на $node"
    switch_and_retest "$node"
    LAST_TOTAL=""   # чтобы следующий цикл считал простой заново
done
