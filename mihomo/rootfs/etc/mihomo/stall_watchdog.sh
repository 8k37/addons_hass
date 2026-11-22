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
# URL для замера РЕАЛЬНОЙ скорости (файл, который можно скачать через узел)
SPEED_URL="${SPEED_URL:-https://speed.cloudflare.com/__down?bytes=2000000}"
# минимально приемлемая скорость узла, КБ/с: первый кандидат, чья скорость
# закачки ≥ порога, выбирается сразу, без теста остальных (0 = всегда искать максимум)
MIN_SPEED_KB="${MIN_SPEED_KB:-500}"
# локальный порт mihomo, через который гоняется тестовая закачка
PROXY_PORT="${PROXY_PORT:-7890}"

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
bashio::log.info "Сторож трафика работает (порог: ${TIMEOUT} с без данных при активных подключениях; выбор узла — первый со скоростью закачки ≥ ${MIN_SPEED_KB} КБ/с через ${SPEED_URL})."

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
    MIN_BYTES=$((MIN_SPEED_KB * 1024))
    bashio::log.warning "Подключений: ${NCOUNT}, но трафика нет ${TIMEOUT} с — меняю узел (порог скорости: ${MIN_SPEED_KB} КБ/с)."

    # Готовим кандидатов и тестируем РЕАЛЬНУЮ СКОРОСТЬ каждого: узел
    # временно становится активным в PROXY, через mixed-порт качается
    # тестовый файл, меряется скорость скачивания. Оставляем самый быстрый.
    PROXIES="$(curl -sf --max-time 3 "${auth[@]}" "${API}/proxies" 2>/dev/null || true)"
    [ -z "${PROXIES}" ] && continue

    # группы (AUTO и пр.) выбирать как узел нельзя
    GROUPS="$(jq -r '[.proxies[] | select(.all != null) | .name] | .[]' <<<"${PROXIES}" 2>/dev/null || true)"
    CANDIDATES="$(jq -r '.proxies.PROXY.all[]' <<<"${PROXIES}" 2>/dev/null \
        | grep -vxF -e DIRECT -e REJECT -f <(printf '%s\n' "${GROUPS}") || true)"
    [ -z "${CANDIDATES}" ] && bashio::log.warning "Кандидатов для переключения не найдено." && continue

    BEST=""
    BEST_SPEED=0
    while IFS= read -r cand; do
        [ -z "${cand}" ] && continue
        # мёртвые узлы (нет ни одной успешной проверки) не тестируем,
        # если есть хоть кто-то живой
        HIST="$(jq -r --arg n "${cand}" \
            '(.proxies[$n].history // []) | map(select(.delay > 0)) | length' \
            <<<"${PROXIES}" 2>/dev/null || echo 0)"
        if [ "${HIST}" -eq 0 ] 2>/dev/null && [ -n "${BEST}" ]; then
            continue
        fi
        # переключаемся на кандидата и меряем скорость закачки через него
        curl -sf --max-time 3 -X PUT "${auth[@]}" -H "Content-Type: application/json" \
            "${API}/proxies/PROXY" -d "{\"name\": \"${cand}\"}" >/dev/null 2>&1 || true
        SPEED="$(curl -so /dev/null --max-time 6 \
            -x "http://127.0.0.1:${PROXY_PORT}" \
            -w '%{speed_download}' "${SPEED_URL}" 2>/dev/null || echo 0)"
        SPEED="${SPEED%%.*}"
        [ -z "${SPEED}" ] && SPEED=0
        bashio::log.info "Тест скорости: ${cand} — ${SPEED} байт/с"
        if [ "${SPEED}" -gt "${BEST_SPEED}" ]; then
            BEST_SPEED="${SPEED}"
            BEST="${cand}"
        fi
        # достаточная скорость — берём сразу, остальных не тестируем
        if [ "${MIN_BYTES}" -gt 0 ] && [ "${SPEED}" -ge "${MIN_BYTES}" ]; then
            bashio::log.info "${cand}: ${SPEED} байт/с ≥ ${MIN_BYTES} байт/с — подходит, тест остальных не нужен."
            break
        fi
    done <<<"${CANDIDATES}"

    # ни у кого не мерили (все «мёртвые» по истории) — берём первого кандидата
    if [ -z "${BEST}" ]; then
        BEST="$(head -n1 <<<"${CANDIDATES}")"
    fi

    if curl -sf --max-time 3 -X PUT "${auth[@]}" -H "Content-Type: application/json" \
            "${API}/proxies/PROXY" -d "{\"name\": \"${BEST}\"}" >/dev/null 2>&1; then
        if [ "${BEST_SPEED}" -ge "${MIN_BYTES}" ]; then
            bashio::log.warning "PROXY переключен на ${BEST} (${BEST_SPEED} байт/с, порог ${MIN_SPEED_KB} КБ/с выполнен). Перезапускаю проверку группы AUTO."
        else
            bashio::log.warning "Никто не дотянул до ${MIN_SPEED_KB} КБ/с — PROXY переключен на самого быстрого: ${BEST} (${BEST_SPEED} байт/с). Перезапускаю проверку группы AUTO."
        fi
        # форсируем url-test, чтобы не ждать очередного интервала
        curl -sf --max-time 30 -G "${auth[@]}" "${API}/group/AUTO/delay" \
            --data-urlencode "url=${HC_URL}" --data-urlencode "timeout=5000" \
            >/dev/null 2>&1 || true
    else
        bashio::log.warning "Не удалось переключить PROXY на ${BEST}."
    fi
done
