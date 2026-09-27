# BORA Rohdaten-Optimizer - Start mit Selbstdiagnose (Windows / Docker Desktop)
#
#   .\start.ps1            Image bauen (bzw. vorhandenes laden) und starten
#   .\start.ps1 -Rebuild   Image neu bauen erzwingen
#   .\start.ps1 -Stop      Container stoppen
#
# Falls Skripte blockiert sind:  powershell -ExecutionPolicy Bypass -File .\start.ps1
param([switch]$Rebuild, [switch]$Stop)
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$Log = Join-Path $PSScriptRoot "diagnose.log"
$Image = "bora-rodaten-optimizer:latest"
$ImageTar = "dist\bora-rodaten-optimizer-image.tar.gz"
Start-Transcript -Path $Log -Force | Out-Null

function Ok($m)   { Write-Host "  [OK]     $m" -ForegroundColor Green }
function Fail($m, [string[]]$hints) {
  Write-Host "`n  [FEHLER] $m" -ForegroundColor Red
  foreach ($h in $hints) { Write-Host "           $h" }
  Write-Host "`n  Bitte die Datei $Log an den Support schicken."
  Stop-Transcript | Out-Null; exit 1
}

Write-Host "== BORA Rohdaten-Optimizer - Start ($(Get-Date -Format s))"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Fail "Docker ist nicht installiert." @("Docker Desktop installieren: https://www.docker.com/products/docker-desktop/")
}
docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail "Docker läuft nicht." @("Docker Desktop starten und warten, bis 'Engine running' angezeigt wird.") }
Ok "Docker $(docker version --format '{{.Server.Version}}')"

docker compose version *> $null
if ($LASTEXITCODE -eq 0) { $Compose = @("docker", "compose") }
elseif (Get-Command docker-compose -ErrorAction SilentlyContinue) { $Compose = @("docker-compose") }
else { Fail "Docker Compose fehlt." @("Docker Desktop aktualisieren.") }
function Compose { if ($Compose.Count -gt 1) { & docker compose @args } else { & docker-compose @args } }
Ok "Compose vorhanden"

if ($Stop) { Compose down; Stop-Transcript | Out-Null; exit 0 }

if (-not (Test-Path .env)) { Copy-Item .env.example .env; Ok ".env aus .env.example angelegt" }
$Port = ((Select-String -Path .env -Pattern '^BORA_PORT=(\d+)' | Select-Object -Last 1).Matches.Groups[1].Value)
if (-not $Port) { $Port = "8088" }
New-Item -ItemType Directory -Force -Path sources | Out-Null

$own = docker ps -q --filter "name=^bora-rodaten-optimizer$"
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy -and -not $own) {
  $proc = (Get-Process -Id $busy[0].OwningProcess -ErrorAction SilentlyContinue).ProcessName
  Fail "Port $Port ist bereits belegt (Prozess: $proc)." @("In .env einen freien Port setzen, z.B. BORA_PORT=9088, und erneut starten.")
}
Ok "Port $Port frei"

$build = $false
if ($Rebuild) { $build = $true }
else {
  docker image inspect $Image *> $null
  if ($LASTEXITCODE -eq 0) { Ok "Image vorhanden: $Image" }
  elseif ((Test-Path $ImageTar) -or (Test-Path "$ImageTar.part*")) {
    if (-not (Test-Path $ImageTar)) {  # geteilt ausgelieferte Datei zusammensetzen
      $out = [IO.File]::Create((Join-Path $PSScriptRoot $ImageTar))
      Get-ChildItem "$ImageTar.part*" | Sort-Object Name | ForEach-Object {
        $in = [IO.File]::OpenRead($_.FullName); $in.CopyTo($out); $in.Close() }
      $out.Close(); Ok "Teildateien zusammengesetzt"
    }
    Write-Host "== Lade fertiges Image aus $ImageTar (kein Build/Internet nötig) ..."
    docker load -i $ImageTar
    if ($LASTEXITCODE -ne 0) { Fail "Image konnte nicht geladen werden." @() }
    Ok "Image geladen"
  } else { $build = $true }
}

if ($build) {
  Write-Host "== Baue Image (dauert beim ersten Mal 1-3 Minuten) ..."
  Compose build 2>&1 | Tee-Object -Variable buildOut
  if ($LASTEXITCODE -ne 0) {
    $t = ($buildOut | Out-String)
    if ($t -match "CERTIFICATE_VERIFY_FAILED|certificate verify failed|x509") {
      Fail "Zertifikatsfehler - Firmen-Proxy mit TLS-Inspection." @("Firmen-Root-Zertifikat als PEM nach certs\firma.crt legen (siehe certs\README.md), dann .\start.ps1 -Rebuild", "ODER fertiges Image nutzen: $ImageTar")
    } elseif ($t -match "pull access denied|failed to resolve source metadata|registry-1.docker.io|TLS handshake timeout|i/o timeout") {
      Fail "Docker Hub nicht erreichbar." @("In .env BASE_IMAGE=<interne-registry>/python:3.12-slim setzen oder Proxy in Docker Desktop konfigurieren", "ODER fertiges Image nutzen: $ImageTar")
    } elseif ($t -match "Could not find a version|No matching distribution|ProxyError") {
      Fail "Python-Pakete (pypi.org) nicht erreichbar." @("In .env HTTPS_PROXY bzw. PIP_INDEX_URL setzen", "ODER fertiges Image nutzen: $ImageTar")
    }
    Fail "Build fehlgeschlagen - Details siehe oben." @()
  }
  Ok "Image gebaut"
}

Write-Host "== Starte Container ..."
Compose up -d --no-build --force-recreate
if ($LASTEXITCODE -ne 0) { Fail "Container konnte nicht gestartet werden - Details siehe oben." @() }

$status = ""
for ($i = 0; $i -lt 45; $i++) {
  $status = docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' bora-rodaten-optimizer
  if ($status -eq "running healthy" -or $status -like "exited*" -or $status -like "restarting*") { break }
  Start-Sleep 2
}
if ($status -ne "running healthy") {
  docker logs --tail 60 bora-rodaten-optimizer
  Fail "Container ist nicht betriebsbereit (Status: $status)." @()
}
Ok "Container läuft (healthy)"

Write-Host ""
Write-Host "  ============================================================"
Write-Host "   BORA Rohdaten-Optimizer ist bereit:  http://localhost:$Port" -ForegroundColor Green
Write-Host "   Log-Verzeichnisse für automatisches Einlesen: $PSScriptRoot\sources"
Write-Host "   Stoppen: .\start.ps1 -Stop"
Write-Host "  ============================================================"
Stop-Transcript | Out-Null
Start-Process "http://localhost:$Port"
