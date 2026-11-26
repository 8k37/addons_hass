ARG BUILD_FROM="ghcr.io/home-assistant/amd64-base:3.19"
FROM ${BUILD_FROM}

# Python 3 + requests — единственная внешняя зависимость бота
RUN apk add --no-cache python3 py3-requests tzdata

COPY rootfs /
