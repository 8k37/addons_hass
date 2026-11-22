#!/usr/bin/with-contenv bashio
# ==============================================================================
# Сторож «зависшего» трафика: если активные подключения есть, но суммарный
# трафик (upload+download) не растёт STALL_TIMEOUT секунд подряд — считаем,
# что текущий узел «отдает, но не возит», принудительно переключаем группу
# PROXY на следующий живой узел и перезапускаем health-check группы AUTO.
# ==============================================================================

API="http://127.0.0.1:9090"
TIMEOUT="${STALL_TIMEOUT:-10}"
HC_URL="${HC_URL:-https://www.gstatic.com/generate_204}"

# секрет внешнего контроллера, если задан в конфиге
SECRET="$(yq '.secret // ""' "${CONFIG_FILE}" 2>/dev/null || echo "")"
if [ -n "${SECRET}" ]; then
    auth=(-H "Authorization: Bearer ${SECRET}")
else
    auth=()
fi

# ждём поднятия API (максимум ~2 минуты)
i=0
until curl -sf --max-time 3 "${auth[@]}" "${API}/version" >/dev/null 2>&1; do
    i=$((i + 1))
    [ "${i}" -gt 120 ] && exit 0
    sleep 1
done
bashio::log.info "Сторож трафика работает (порог: ${TIMEOUT} с без данных при активных подключениях)."

LAST_TOTAL="-1"
STALL=0
while true; do
    sleep 1

    CONN="$(curl -sf --max-time 3 "${auth[@]}" "${API}/connections" 2>/dev/null || true)"
    # нет API или нет активных подключений — счётчики сбрасываем
    if [ -z "${CONN}" ]; then
        STALL=0
        LAST_TOTAL="-1"
        continue
    fi
    NCOUNT="$(jq '.connections | length' <<<"${CONN}" 2>/dev/null || echo 0)"
    if [ "${NCOUNT}" -eq 0 ] 2>/dev/null; then
        STALL=0
        LAST_TOTAL="-1"
        continue
    fi

    TOTAL="$(jq '[.downloadTotal, .uploadTotal] | add' <<<"${CONN}" 2>/dev/null || echo 0)"
    if [ "${TOTAL}" = "${LAST_TOTAL}" ]; then
        STALL=$((STALL + 1))
    else
        STALL=0
        LAST_TOTAL="${TOTAL}"
        continue
    fi
    [ "${STALL}" -lt "${TIMEOUT}" ] && continue

    # сработал порог — меняем узел
    STALL=0
    LAST_TOTAL="-1"
    bashio::log.warning "Подключений: ${NCOUNT}, но трафика нет ${TIMEOUT} с — меняю узел."

    PROXIES="$(curl -sf --max-time 3 "${auth[@]}" "${API}/proxies" 2>/dev/null || true)"
    [ -z "${PROXIES}" ] && continue
    CUR="$(jq -r '.proxies.PROXY.now // empty' <<<"${PROXIES}")"
    [ -z "${CUR}" ] && continue

    # группы (AUTO и пр.) выбирать как узел нельзя
    GROUPS="$(jq -r '[.proxies[] | select(.all != null) | .name] | .[]' <<<"${PROXIES}" 2>/dev/null || true)"
    CANDIDATES="$(jq -r '.proxies.PROXY.all[]' <<<"${PROXIES}" 2>/dev/null \
        | grep -vxF -e DIRECT -e REJECT -e "${CUR}" -f <(printf '%s\n' "${GROUPS}") || true)"

    # предпочитаем узел, у которого есть хоть одна успешная проверка (delay > 0);
    # если таких нет — берём первого кандидата по списку
    NEXT=""
    while IFS= read -r cand; do
        [ -z "${cand}" ] && continue
        HIST="$(jq -r --arg n "${cand}" \
            '(.proxies[$n].history // []) | map(select(.delay > 0)) | length' \
            <<<"${PROXIES}" 2>/dev/null || echo 0)"
        if [ "${HIST}" -gt 0 ] 2>/dev/null; then
            NEXT="${cand}"
            break
        fi
        [ -z "${NEXT}" ] && NEXT="${cand}"
    done <<<"${CANDIDATES}"
    [ -z "${NEXT}" ] && bashio::log.warning "Кандидатов для переключения не найдено." && continue

    if curl -sf --max-time 3 -X PUT "${auth[@]}" -H "Content-Type: application/json" \
            "${API}/proxies/PROXY" -d "{\"name\": \"${NEXT}\"}" >/dev/null 2>&1; then
        bashio::log.info "PROXY переключен: ${CUR} -> ${NEXT}. Перезапускаю проверку группы AUTO."
        # форсируем url-test, чтобы не ждать очередного интервала
        curl -sf --max-time 30 -G "${auth[@]}" "${API}/group/AUTO/delay" \
            --data-urlencode "url=${HC_URL}" --data-urlencode "timeout=5000" \
            >/dev/null 2>&1 || true
    else
        bashio::log.warning "Не удалось переключить PROXY на ${NEXT}."
    fi
done
