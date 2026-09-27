# BORA-Rohdaten-Optimizer

Web-GUI im Docker-Container, die Rohdaten zur Performance-Analyse **sammelt**,
**kategorisiert** und **zeitrahmengerecht als ZIP-Pakete** bereitstellt.

| Kategorie | Erkannte Quellen |
|-----------|------------------|
| `access`  | `access.log*` – Common/Combined Log Format, W3C Extended (WebLogic, OHS, Apache), auch rotiert/`.gz` |
| `server`  | `server*.log*` – WebLogic-Serverlog (`####<…>`), log4j/ISO, deutsches Datumsformat, auch rotiert/`.gz` und als HTML-Export |
| `awr`     | Oracle-Reports als **HTML oder Text**: AWR, AWR-RAC/Global, AWR-Compare (awrdiff), ASH, ADDM, Statspack |
| `unknown` | nicht erkannt – in der GUI manuell zuordenbar |
| `ignore`  | manuell ausgeschlossen |

Die Kategorie wird primär am **Inhalt** erkannt (Dateiname nur als Rückfallebene)
und lässt sich pro Datei in der GUI übersteuern.

## Paketierungsregeln

Beide Regeln laufen **automatisch nach jedem Upload bzw. Einlesen**; neu
erstellt werden nur betroffene Zeiträume/Tage, nicht mehr gültige Pakete
werden entfernt (abschaltbar mit `BORA_AUTO_PACKAGE=0`).

**Regel 1 – AWR-Aufzeichnungszeitraum (Vorrang).** Für jeden AWR-Report
(HTML oder Text, auch RAC/Global/Compare) wird der Aufzeichnungszeitraum
(Begin Snap – End Snap) erkannt. Dazu werden zusammengeführt:

* alle `access.log*`- und `server*.log*`-Zeilen dieses Zeitraums (optional
  ± Puffer: `BORA_AWR_MARGIN_MIN`),
* alle anderen Oracle-Reports (ASH, ADDM, Statspack), deren Zeitraum sich
  mit dem AWR-Zeitraum überschneidet.

→ `BORA_2026-09-27_1000-1100.zip`.

Aus den Log-Dateien werden **nur die Zeilen zwischen Begin und End Snap
Time** übernommen – nicht die ganzen Dateien (Beispiel: 24-h-Log mit
86 400 Zeilen → 3 603 Zeilen für einen 1-h-Snapshot).

**Regel 2 – Kalendertag (standardmäßig aus).** Mit `BORA_DAY_PACKAGES=1`
werden zusätzlich alle Dateien pro Tag zusammengeführt →
`BORA_2026-09-27.zip` (ganze Tage, nur für Tage mit AWR-Report). Per Button
jederzeit auch manuell möglich.

**Ohne AWR-Report kein Paket.** Liegt kein AWR-Report vor, wird kein Paket
erzeugt (auch nicht per Button); ASH-/ADDM-/Statspack-Reports ohne
überschneidenden AWR-Zeitraum und Tage ohne AWR-Report erhalten kein Paket.
Nicht mehr regelkonforme Pakete werden entfernt. Abschaltbar mit
`BORA_REQUIRE_AWR=0`.

Manuell (Button) lassen sich beide Varianten zusätzlich mit *von/bis* und
Puffer neu erstellen.

ZIP-Aufbau:

```
BORA_2026-09-27.zip
├── access/<quelle>/access.log          # nur Zeilen des Zeitfensters, byte-genau
├── server/<quelle>/server1.log
├── server/<quelle>/server1.log00001    # .gz transparent entpackt
├── awr/<quelle>/awrrpt_1_100_101.html  # Report unverändert
└── manifest.json                       # Zeitraum, Quellen, Zeilen, Snap-IDs, DB/Instanz
```

**Einheitlicher Zeitstempel:** Das ZIP und *alle* Einträge darin – Logs,
HTML-/Text-Reports, `manifest.json` – tragen denselben Zeitstempel: bei
Tages-Paketen den Tag (`2026-09-27 00:00`), bei AWR-Paketen den Beginn des
Snapshot-Intervalls (`2026-09-27 10:00`).

Grundsätze:

* **Rohdaten bleiben Rohdaten** – Log-Zeilen werden byte-genau übernommen, nicht umformatiert.
* Mehrzeilige Einträge (Stacktraces) gehören zum vorangehenden Zeitstempel.
* Zeitzonen-Offsets werden ignoriert; alle Quellen werden in der Lokalzeit
  verglichen, in der sie geschrieben wurden (Logs und AWR derselben Umgebung
  liegen so auf einer Zeitachse).
* Jede Log-Datei wird pro Paketierung genau **einmal** sequentiell gelesen
  (streamend, konstanter Speicherbedarf – auch bei mehreren GB).
* ZIPs werden atomar geschrieben (`.part` → Umbenennung) und nutzen Zip64.

## Start – ausschließlich mit Docker

Auf dem Zielsystem wird **nur Docker** benötigt (kein Python, kein Git, keine
Skripte). Build, Tests, Start-Diagnose und Betrieb laufen im Container.

### Variante A – fertiges Image aus der Registry (ohne Quellcode)

Die GitHub-Actions-Pipeline testet, baut und veröffentlicht das Image nach
`ghcr.io/icoglu/bora-rodaten-optimizer`:

```bash
docker login ghcr.io          # nur nötig, solange das Paket privat ist (GitHub-Token mit read:packages)
docker run -d --name bora-rodaten-optimizer --restart unless-stopped \
  -p 8088:8088 \
  -v bora-data:/data \
  -v /pfad/zu/logs:/sources/logs:ro -e BORA_SOURCE_DIRS=/sources/logs \
  ghcr.io/icoglu/bora-rodaten-optimizer:latest
# GUI: http://localhost:8088   (Mac, Windows, Linux)
```

### Variante B – Docker Compose

```bash
git clone -b claude/docker-gui-data-collection-cb62pl https://github.com/icoglu/BORA-Rodaten-Optimizer.git
cd BORA-Rodaten-Optimizer      # WICHTIG: im Ordner mit docker-compose.yml ausführen
docker compose up -d           # baut lokal und startet
docker compose logs -f         # Start-Diagnose und Protokoll
docker compose down            # stoppen (Daten bleiben im Volume bora-data)
# GUI: http://localhost:8088
```

Anderer Port (z. B. weil 8088 belegt ist): in `.env` `BORA_PORT=8091` setzen –
**nicht 8090**, der ist für die BORA-Anwendung reserviert.

Einstellungen (Port, Log-Verzeichnis, Passwort, Proxy …) in `.env`,
Vorlage: `.env.example`.

### Variante C – offline (ohne Registry/Internet)

In GitHub unter *Actions → Docker-Image → letzter Lauf → Artifacts* liegt
`bora-rodaten-optimizer-image` (`docker save`-Archiv):

```bash
docker load -i bora-rodaten-optimizer-image.tar.gz
docker run …                        # wie in Variante A (Image: ghcr.io/icoglu/bora-rodaten-optimizer:latest)
```

### Automatische Port-Wahl (nur Linux-Server)

Auf Linux kann der Container im Host-Netzwerk laufen und **selbst einen
freien Port suchen**: ab `BORA_PORT` (8088) aufsteigend, der erste freie wird
genommen. Die Ports **8080** und **8090 (BORA-Anwendung)** sind reserviert
und werden nie verwendet, auch wenn sie gerade frei sind (`BORA_PORT_EXCLUDE`).

```bash
docker compose -f docker-compose.yml -f docker-compose.host.yml up -d
docker compose logs | grep lauscht
```

```
Port 8088 belegt – nächster …
Port 8089 belegt – nächster …
Port 8090 reserviert – übersprungen
BORA Rohdaten-Optimizer lauscht auf Port 8091  →  http://<host>:8091
```

> **Docker Desktop (Mac/Windows) unterstützt das Host-Netzwerk nicht** – der
> Container wäre dann unter `localhost` nicht erreichbar. Dort immer die
> Standard-Variante mit Port-Mapping verwenden.

### Start-Diagnose

Beim Start prüft der Container Datenverzeichnis, Speicherplatz,
eingebundene Log-Verzeichnisse und Zugangsschutz. Das Ergebnis steht in
`docker logs bora-rodaten-optimizer`, Warnungen zeigt zusätzlich die GUI an,
und als JSON gibt es sie unter `/api/selfcheck`:

```
[OK ] Datenverzeichnis: /data beschreibbar
[OK ] Speicherplatz: 29.1 GB frei auf /data
[WARN] Quelle /sources/logs: nicht vorhanden
       -> Verzeichnis mounten, z.B. -v /pfad/zu/logs:/sources/logs:ro
```

### Tests im Container

```bash
docker compose --profile test run --rm tests
# oder: docker build --target test .
```

### Build im Firmennetz

| Symptom im Build | Lösung |
|------------------|--------|
| `CERTIFICATE_VERIFY_FAILED` / `x509` | Firmen-Root-Zertifikat als PEM nach `certs/firma.crt` (siehe `certs/README.md`) |
| `pypi.org` nicht erreichbar | in `.env`: `HTTPS_PROXY=…` oder `PIP_INDEX_URL=…` (interner Mirror) |
| Docker Hub gesperrt, `pull access denied`, `429` | in `.env`: `BASE_IMAGE=registry.firma.de/python:3.12-slim` – oder Variante A/C |
| Port belegt | wird automatisch gelöst – siehe *Port-Wahl* |

## Daten sammeln

1. **Upload in der GUI** – beliebig viele Dateien; optional mit Quelle/Host-Label
   (z. B. `wls-prod-01`), das als Unterordner in den ZIPs erscheint.
2. **Archive** (`.zip`, `.tar`, `.tar.gz`, `.tgz`) werden automatisch entpackt.
3. **Read-only Mounts** – Verzeichnisse, die per `BORA_SOURCE_DIRS` eingebunden
   sind, werden beim Start und per *„Verzeichnisse neu einlesen“* erfasst.

### Große Uploads (> 4 GB)

* Die GUI streamt jede Datei als rohen Request-Body (`PUT /api/upload`) direkt
  auf das Daten-Volume – ohne Multipart-Parsing und ohne Zwischenkopie, mit
  Fortschrittsanzeige.
* Entpacken läuft im Hintergrund-Job (keine HTTP-Timeouts); ZIP64-Archive werden
  unterstützt. Vor dem ersten geschriebenen Byte werden Pfade (Zip-Slip) und
  freier Speicherplatz geprüft. Defekte Archive werden zu `*.defekt` umbenannt.
* **EAR-, WAR-, JAR- und RAR-Dateien werden nicht entpackt**: Liegen sie in einem
  hochgeladenen Archiv (z. B. Domain-Export), werden sie gar nicht erst
  herausgeschrieben; direkt hochgeladene bleiben unverändert und werden ignoriert.
* Getestet mit einem 4,4-GB-ZIP (18 Mio. Logzeilen): Upload, Entpacken, Analyse
  und Paketierung inkl. 4,4-GB-Eintrag im Ergebnis-ZIP.
* **Platzbedarf** auf `/data` einplanen: Archiv + entpackter Inhalt während des
  Entpackens, danach nur noch der Inhalt; bei der Paketierung zusätzlich
  temporär die Log-Ausschnitte plus die Ergebnis-ZIPs.
* Liegt ein Reverse-Proxy davor, dessen Body-Limit und Timeouts anheben, z. B.
  nginx: `client_max_body_size 0; proxy_request_buffering off; proxy_read_timeout 3600s;`

Upload per Kommandozeile (z. B. direkt vom Server):

```bash
curl -T logs.zip "http://bora-host:8088/api/upload?name=logs.zip&source=wls-prod-01"
```

## Zurücksetzen

Der Button **„Alles zurücksetzen und löschen“** (Abschnitt 5 der GUI, mit
Sicherheitsabfrage) löscht alle hochgeladenen Dateien, alle ZIP-Pakete, den
Katalog inkl. manueller Kategorien und die Arbeitsdateien. Eingebundene
Log-Verzeichnisse bleiben unberührt und werden beim nächsten Einlesen wieder
erfasst. Vollständig inkl. Volume: `docker compose down -v`.

## Konfiguration

| Variable | Standard | Bedeutung |
|----------|----------|-----------|
| `BORA_PORT` | `8088` | Startport der automatischen Port-Wahl |
| `BORA_PORT_SEARCH` | `100` | Anzahl zu probierender Ports; `1` = genau `BORA_PORT`, sonst Abbruch |
| `BORA_PORT_EXCLUDE` | `8080,8090` | Ports, die **nie** verwendet werden (8090 = BORA-Anwendung), z. B. `8080,8090,9000-9010` |
| `BORA_DATA_DIR` | `/data` | Volume für Inbox, Katalog, Ausgabe |
| `BORA_SOURCE_DIRS` | – | zusätzliche Quellverzeichnisse, `:`-getrennt (read-only genügt) |
| `BORA_ZIP_PREFIX` | `BORA` | Präfix der ZIP-Namen |
| `BORA_DAY_PACKAGES` | `0` | Zusätzlich Tages-Pakete (ganze Tage) automatisch erzeugen |
| `BORA_REQUIRE_AWR` | `1` | Ohne AWR-Report kein Paket (`0` = auch Tage/Reports ohne AWR paketieren) |
| `BORA_AWR_MARGIN_MIN` | `0` | Puffer in Minuten um den AWR-Zeitraum bei automatischen AWR-Paketen |
| `BORA_AUTO_PACKAGE` | `1` | Tages-Pakete nach jedem Upload/Einlesen automatisch erstellen (`0` = aus) |
| `BORA_SKIP_EXTRACT` | `.ear,.war,.jar,.rar` | Java-Anwendungsarchive: werden beim Entpacken übersprungen, selbst nie entpackt und als `ignore` eingestuft |
| `BORA_MAX_UPLOAD_MB` | `0` | Upload-Limit je Datei, `0` = unbegrenzt |
| `BORA_USER` / `BORA_PASSWORD` | – | aktiviert HTTP-Basic-Auth (beide setzen; TLS über Reverse-Proxy) |
| `TZ` | `Europe/Berlin` | Zeitzone des Containers (Anzeige/Dateizeiten) |

Verzeichnisse im Volume: `inbox/` (Uploads), `output/` (ZIP-Pakete),
`work/` (Temporärdaten), `catalog.sqlite3` (Katalog, Analyse-Cache).

## API

| Methode | Pfad | Zweck |
|---------|------|-------|
| `PUT`  | `/api/upload?name=…&source=…` | Streaming-Upload (Body = Datei) |
| `GET`  | `/api/files` | Katalog als JSON |
| `GET`  | `/api/status` | Status des Hintergrund-Jobs |
| `GET`  | `/api/outputs` | erzeugte ZIP-Pakete |
| `GET`  | `/download/<name>.zip` | ZIP herunterladen |
| `GET`  | `/healthz` | Health-Check (ohne Auth) |
| `GET`  | `/api/docs` | OpenAPI-Dokumentation |

## Entwicklung

```bash
docker compose --profile test run --rm tests     # Tests
docker compose up -d --build                     # nach Code-Änderung neu bauen und starten
```

Aufbau: `app/detect.py` (Kategorisierung, Oracle-Report-Parser),
`app/timestamps.py` (Zeitstempelformate), `app/catalog.py` (SQLite-Katalog),
`app/archives.py` (sicheres Entpacken), `app/packager.py` (Zeitfenster & ZIP),
`app/main.py` (Web-GUI/API).
