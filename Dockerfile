# BORA Rohdaten-Optimizer - alles im Container: Build, Tests, Betrieb.
#
#   docker build -t bora-rodaten-optimizer .              # Laufzeit-Image
#   docker build --target test .                          # Tests im Container
#
# Basis-Image überschreibbar (interne Registry):
#   --build-arg BASE_IMAGE=registry.firma.de/python:3.12-slim
ARG BASE_IMAGE=python:3.12-slim

# ---------------------------------------------------------------- base
FROM ${BASE_IMAGE} AS base
ARG PIP_INDEX_URL=
ARG PIP_TRUSTED_HOST=
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
WORKDIR /opt/bora

# Firmen-/Proxy-Zertifikate (*.crt in certs/) vertrauen - nötig bei TLS-Inspection
COPY certs/ /tmp/certs/
RUN set -e; \
    if ls /tmp/certs/*.crt >/dev/null 2>&1; then \
      cat /tmp/certs/*.crt >> /etc/ssl/certs/ca-certificates.crt; \
      echo "Firmenzertifikate übernommen: $(ls /tmp/certs/*.crt | wc -l)"; \
    fi; \
    rm -rf /tmp/certs

COPY requirements.txt .
RUN pip install ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} \
                ${PIP_TRUSTED_HOST:+--trusted-host "$PIP_TRUSTED_HOST"} \
                -r requirements.txt
COPY app ./app

# ---------------------------------------------------------------- test
FROM base AS test
COPY requirements-dev.txt .
RUN pip install ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} \
                ${PIP_TRUSTED_HOST:+--trusted-host "$PIP_TRUSTED_HOST"} \
                -r requirements-dev.txt
COPY tests ./tests
RUN python -m pytest -q -p no:cacheprovider

# ---------------------------------------------------------------- runtime (Standard)
FROM base AS runtime
LABEL org.opencontainers.image.title="BORA Rohdaten-Optimizer" \
      org.opencontainers.image.description="Sammeln, Kategorisieren und zeitrahmengerechtes Paketieren von access.log, server*.log* und Oracle-Reports" \
      org.opencontainers.image.source="https://github.com/icoglu/BORA-Rodaten-Optimizer"
ENV BORA_DATA_DIR=/data \
    BORA_PORT=8088 \
    BORA_PORT_SEARCH=100 \
    BORA_PORT_EXCLUDE=8080,8090 \
    TZ=Europe/Berlin
RUN groupadd --system --gid 10001 bora \
 && useradd --system --uid 10001 --gid bora --home-dir /opt/bora --shell /usr/sbin/nologin bora \
 && mkdir -p /data /sources/logs && chown bora:bora /data

USER bora
VOLUME ["/data"]
EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; p=open('/tmp/bora-port').read().strip(); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz',timeout=4).status==200 else 1)"

# Sucht ab BORA_PORT den ersten freien Port (BORA_PORT_SEARCH Versuche, 1 = fester Port).
# Mit --network host werden so die Ports des Hosts geprüft.
CMD ["python", "-m", "app.serve"]
