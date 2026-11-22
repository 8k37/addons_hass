ARG BUILD_FROM
FROM $BUILD_FROM

# BUILD_ARCH задаётся сборщиком Home Assistant (для Pi 5 это aarch64)
ARG BUILD_ARCH
ARG MIHOMO_VERSION
ARG METACUBEXD_VERSION

RUN set -eux; \
    apk add --no-cache ca-certificates curl tzdata tar gzip iptables python3; \
    case "${BUILD_ARCH}" in \
      aarch64) MIHOMO_ARCH="arm64" ;; \
      armv7)   MIHOMO_ARCH="armv7" ;; \
      armhf)   MIHOMO_ARCH="armv5" ;; \
      i386)    MIHOMO_ARCH="386" ;; \
      amd64)   MIHOMO_ARCH="amd64" ;; \
      *) echo "Unsupported architecture: ${BUILD_ARCH}"; exit 1 ;; \
    esac; \
    curl -fsSL \
      "https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/mihomo-linux-${MIHOMO_ARCH}-${MIHOMO_VERSION}.gz" \
      -o /tmp/mihomo.gz; \
    gunzip /tmp/mihomo.gz; \
    install -m 0755 /tmp/mihomo /usr/local/bin/mihomo; \
    rm -f /tmp/mihomo

# yq для слияния конфигов (proxy-providers)
ARG YQ_VERSION=v4.44.3
RUN set -eux; \
    case "${BUILD_ARCH}" in \
      aarch64) YQ_ARCH="arm64" ;; \
      armv7)   YQ_ARCH="arm" ;; \
      armhf)   YQ_ARCH="arm" ;; \
      i386)    YQ_ARCH="386" ;; \
      amd64)   YQ_ARCH="amd64" ;; \
      *) echo "Unsupported architecture: ${BUILD_ARCH}"; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${YQ_ARCH}" \
      -o /usr/local/bin/yq; \
    chmod +x /usr/local/bin/yq

# Веб-панель metacubexd (официальный дашборд mihomo)
RUN set -eux; \
    mkdir -p /usr/share/mihomo/ui; \
    curl -fsSL \
      "https://github.com/MetaCubeX/metacubexd/releases/download/${METACUBEXD_VERSION}/compressed-dist.tgz" \
      -o /tmp/ui.tgz; \
    tar -xzf /tmp/ui.tgz -C /usr/share/mihomo/ui; \
    rm -f /tmp/ui.tgz

COPY rootfs /

RUN chmod +x /etc/s6-overlay/s6-rc.d/mihomo/run /etc/s6-overlay/s6-rc.d/mihomo/finish
