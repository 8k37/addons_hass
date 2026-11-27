ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12
FROM $BUILD_FROM

# Устанавливаем зависимости
RUN apk add --no-cache python3 py3-pip && python3 -m pip install --no-cache-dir --break-system-packages \
    requests==2.32.3

# Копируем корневую файловую систему (s6-rc сервисы + приложение)
COPY rootfs /

# Права на запуск
RUN chmod +x /etc/s6-overlay/s6-rc.d/vkbot/run

WORKDIR /app
