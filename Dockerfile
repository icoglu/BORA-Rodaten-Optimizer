# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BORA_DATA_DIR=/data \
    TZ=Europe/Berlin

WORKDIR /opt/bora
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && groupadd --system --gid 10001 bora \
 && useradd --system --uid 10001 --gid bora --home-dir /opt/bora --shell /usr/sbin/nologin bora \
 && mkdir -p /data && chown bora:bora /data

COPY app ./app

USER bora
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
