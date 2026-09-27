# Basis-Image überschreibbar, z.B. für eine interne Registry:
#   docker compose build --build-arg BASE_IMAGE=registry.firma.de/python:3.12-slim
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

# Optional für Firmennetze (werden über docker-compose.yml aus .env übergeben)
ARG PIP_INDEX_URL=
ARG PIP_TRUSTED_HOST=

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BORA_DATA_DIR=/data \
    TZ=Europe/Berlin \
    BORA_PORT=8088

WORKDIR /opt/bora

# Firmen-/Proxy-Zertifikate (*.crt in certs/) vertrauen - nötig bei TLS-Inspection
COPY certs/ /tmp/certs/
RUN set -e; \
    if ls /tmp/certs/*.crt >/dev/null 2>&1; then \
      cat /tmp/certs/*.crt >> /etc/ssl/certs/ca-certificates.crt; \
      echo "Firmenzertifikate übernommen: $(ls /tmp/certs/*.crt | wc -l)"; \
    fi; \
    rm -rf /tmp/certs
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

COPY requirements.txt .
RUN pip install ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} \
                ${PIP_TRUSTED_HOST:+--trusted-host "$PIP_TRUSTED_HOST"} \
                -r requirements.txt \
 && groupadd --system --gid 10001 bora \
 && useradd --system --uid 10001 --gid bora --home-dir /opt/bora --shell /usr/sbin/nologin bora \
 && mkdir -p /data && chown bora:bora /data

COPY app ./app

USER bora
VOLUME ["/data"]
EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"BORA_PORT\"]}/healthz',timeout=4).status==200 else 1)"

# Port per BORA_PORT änderbar (z.B. bei --network host)
CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port \"$BORA_PORT\" --proxy-headers"]
