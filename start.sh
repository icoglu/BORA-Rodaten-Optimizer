#!/usr/bin/env bash
# BORA Rohdaten-Optimizer - Start mit Selbstdiagnose (Linux/macOS)
#
#   ./start.sh            Image bauen (bzw. vorhandenes laden) und starten
#   ./start.sh --rebuild  Image neu bauen erzwingen
#   ./start.sh --stop     Container stoppen
#
# Alle Ausgaben landen zusätzlich in diagnose.log.
set -uo pipefail
cd "$(dirname "$0")"

LOG=diagnose.log
IMAGE=bora-rodaten-optimizer:latest
IMAGE_TAR=dist/bora-rodaten-optimizer-image.tar.gz
exec > >(tee "$LOG") 2>&1

ok()   { printf '  [OK]     %s\n' "$*"; }
warn() { printf '  [HINWEIS] %s\n' "$*"; }
fail() { printf '\n  [FEHLER] %s\n' "$1"; shift; for l in "$@"; do printf '           %s\n' "$l"; done
         printf '\n  Bitte die Datei %s an den Support schicken.\n' "$(pwd)/$LOG"; exit 1; }

echo "== BORA Rohdaten-Optimizer - Start ($(date '+%F %T'), $(uname -sm))"

# ------------------------------------------------------------------ Docker
command -v docker >/dev/null 2>&1 || fail "Docker ist nicht installiert." \
  "Linux: https://docs.docker.com/engine/install/  |  Mac/Windows: Docker Desktop"
if ! docker info >/dev/null 2>&1; then
  if docker info 2>&1 | grep -qi "permission denied"; then
    fail "Keine Berechtigung für Docker." \
      "sudo usermod -aG docker \$USER   (danach ab- und wieder anmelden)" \
      "oder das Skript mit sudo starten: sudo ./start.sh"
  fi
  fail "Docker läuft nicht." "Linux: sudo systemctl start docker  |  Mac/Windows: Docker Desktop starten"
fi
ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null)"

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  fail "Docker Compose fehlt." "Paket 'docker-compose-plugin' installieren (Linux) bzw. Docker Desktop aktualisieren."
fi
ok "Compose: $("${COMPOSE[@]}" version --short 2>/dev/null || "${COMPOSE[@]}" version | head -1)"

if [[ "${1:-}" == "--stop" ]]; then "${COMPOSE[@]}" down; exit 0; fi

# ------------------------------------------------------------ Konfiguration
[[ -f .env ]] || { cp .env.example .env; ok ".env aus .env.example angelegt"; }
PORT=$(grep -E '^BORA_PORT=' .env | tail -1 | cut -d= -f2 | tr -d '[:space:]"')
PORT=${PORT:-8088}
mkdir -p sources

port_busy() {
  if command -v ss >/dev/null 2>&1; then ss -ltn "( sport = :$1 )" 2>/dev/null | grep -q LISTEN
  elif command -v lsof >/dev/null 2>&1; then lsof -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
  else (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; fi
}
OWN=$(docker ps -q --filter "name=^bora-rodaten-optimizer$" --filter "publish=$PORT")
if [[ -z "$OWN" ]] && port_busy "$PORT"; then
  fail "Port $PORT ist bereits belegt." \
    "In .env einen freien Port setzen, z.B.  BORA_PORT=9088  und erneut starten."
fi
ok "Port $PORT frei"

# ------------------------------------------------------------------- Image
BUILD_ARGS=(--no-build)
if [[ "${1:-}" == "--rebuild" ]]; then
  BUILD_ARGS=(--build)
elif docker image inspect "$IMAGE" >/dev/null 2>&1; then
  ok "Image vorhanden: $IMAGE"
elif [[ -f "$IMAGE_TAR" ]] || compgen -G "$IMAGE_TAR.part*" >/dev/null; then
  if [[ ! -f "$IMAGE_TAR" ]]; then  # geteilt ausgelieferte Datei zusammensetzen
    cat "$IMAGE_TAR".part* > "$IMAGE_TAR" && ok "Teildateien zusammengesetzt"
  fi
  echo "== Lade fertiges Image aus $IMAGE_TAR (kein Build/Internet nötig) ..."
  docker load -i "$IMAGE_TAR" || fail "Image konnte nicht geladen werden ($IMAGE_TAR defekt?)."
  ok "Image geladen"
else
  BUILD_ARGS=(--build)
fi

if [[ "${BUILD_ARGS[0]}" == "--build" ]]; then
  echo "== Baue Image (dauert beim ersten Mal 1-3 Minuten) ..."
  if ! "${COMPOSE[@]}" build; then
    if grep -qiE "CERTIFICATE_VERIFY_FAILED|certificate verify failed|x509" "$LOG"; then
      fail "Zertifikatsfehler - Firmen-Proxy mit TLS-Inspection." \
        "Firmen-Root-Zertifikat als PEM nach certs/firma.crt legen (siehe certs/README.md)," \
        "danach: ./start.sh --rebuild   - ODER fertiges Image nutzen: $IMAGE_TAR"
    elif grep -qiE "pull access denied|failed to resolve source metadata|registry-1.docker.io|TLS handshake timeout|i/o timeout" "$LOG"; then
      fail "Docker Hub nicht erreichbar (Basis-Image python:3.12-slim)." \
        "In .env eine interne Registry setzen: BASE_IMAGE=registry.firma.de/python:3.12-slim" \
        "oder Docker-Proxy konfigurieren - ODER fertiges Image nutzen: $IMAGE_TAR"
    elif grep -qiE "Could not find a version|No matching distribution|ProxyError|Connection to pypi" "$LOG"; then
      fail "Python-Pakete (pypi.org) nicht erreichbar." \
        "In .env HTTPS_PROXY bzw. PIP_INDEX_URL (interner Mirror) setzen" \
        "- ODER fertiges Image nutzen: $IMAGE_TAR"
    fi
    fail "Build fehlgeschlagen - Details siehe oben."
  fi
  ok "Image gebaut"
fi

# ------------------------------------------------------------------- Start
echo "== Starte Container ..."
"${COMPOSE[@]}" up -d --no-build --force-recreate || fail "Container konnte nicht gestartet werden - Details siehe oben."

for _ in $(seq 1 45); do
  STATUS=$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' bora-rodaten-optimizer 2>/dev/null)
  [[ "$STATUS" == "running healthy" ]] && break
  [[ "$STATUS" == exited* || "$STATUS" == restarting* ]] && break
  sleep 2
done
if [[ "$STATUS" != "running healthy" ]]; then
  echo "== Container-Log:"; docker logs --tail 60 bora-rodaten-optimizer
  fail "Container ist nicht betriebsbereit (Status: ${STATUS:-unbekannt})."
fi
ok "Container läuft (healthy)"

HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "  ============================================================"
echo "   BORA Rohdaten-Optimizer ist bereit:"
echo "     http://localhost:$PORT"
[[ -n "$HOST_IP" ]] && echo "     http://$HOST_IP:$PORT   (von anderen Rechnern)"
echo "   Log-Verzeichnisse für automatisches Einlesen: $(pwd)/sources"
echo "   Stoppen: ./start.sh --stop"
echo "  ============================================================"
