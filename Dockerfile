# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BORA_DATA_DIR=/data \
    TZ=Europe/Berlin \
    BORA_PORT=8088

WORKDIR /opt/bora
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && groupadd --system --gid 10001 bora \
 && useradd --system --uid 10001 --gid bora --home-dir /opt/bora --shell /usr/sbin/nologin bora \
 && mkdir -p /data && chown bora:bora /data

COPY app ./app

USER bora
VOLUME ["/data"]
EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"BORA_PORT\"]}/healthz',timeout=4).status==200 else 1)"

# Port per BORA_PORT änderbar (z.B. wenn 8088 belegt ist oder bei --network host)
CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port \"$BORA_PORT\" --proxy-headers"]
