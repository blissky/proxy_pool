FROM python:3.11-slim-bookworm

ARG SING_BOX_VERSION=1.13.2

LABEL maintainer="ProxyPool contributors"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    CONFIG_FILE=/data/config.json \
    DB_CONN=redis://@127.0.0.1:6379/0 \
    SING_BOX_BINARY=sing-box \
    SING_BOX_RUNTIME_DIR=/data/sing-box

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends redis-server tini ca-certificates curl gnupg systemd-standalone-sysusers \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://sing-box.app/gpg.key | gpg --dearmor -o /etc/apt/keyrings/sagernet.gpg \
    && printf '%s\n' \
       'Types: deb' \
       'URIs: https://deb.sagernet.org/' \
       'Suites: *' \
       'Components: *' \
       'Enabled: yes' \
       'Signed-By: /etc/apt/keyrings/sagernet.gpg' \
       > /etc/apt/sources.list.d/sagernet.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends "sing-box=${SING_BOX_VERSION}*" \
    && sing-box version \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x proxy_pool.sh && mkdir -p /data

VOLUME ["/data"]

EXPOSE 8082 8083

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8083/config', timeout=3)"

ENTRYPOINT ["tini", "--"]
CMD ["bash", "proxy_pool.sh", "start", "--fg"]
