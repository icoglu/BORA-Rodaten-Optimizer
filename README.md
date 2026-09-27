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

## Paketierungsregel

> Alle Reports werden passend zum Zeitrahmen zusammengeführt und in einzelne,
> mit Datum gekennzeichnete ZIP-Pakete geschrieben.

| Modus | ZIP-Name | Inhalt |
|-------|----------|--------|
| **Pro Kalendertag** (Standard) | `BORA_2026-09-27.zip` | alle Oracle-Reports, deren Intervall den Tag berührt, und genau die Access-/Server-Log-Zeilen dieses Tages |
| **Pro AWR-Intervall** | `BORA_2026-09-27_1000-1100.zip` | der/die Reports des Snapshot-Intervalls (RAC-Instanzen zusammengeführt) und die Log-Zeilen darin, optional ± Puffer in Minuten |

Optional lässt sich der Zeitraum per *von/bis* einschränken.

ZIP-Aufbau:

```
BORA_2026-09-27.zip
├── access/<quelle>/access.log          # nur Zeilen des Zeitfensters, byte-genau
├── server/<quelle>/server1.log
├── server/<quelle>/server1.log00001    # .gz transparent entpackt
├── awr/<quelle>/awrrpt_1_100_101.html  # Report unverändert
└── manifest.json                       # Zeitraum, Quellen, Zeilen, Snap-IDs, DB/Instanz
```

Grundsätze:

* **Rohdaten bleiben Rohdaten** – Log-Zeilen werden byte-genau übernommen, nicht umformatiert.
* Mehrzeilige Einträge (Stacktraces) gehören zum vorangehenden Zeitstempel.
* Zeitzonen-Offsets werden ignoriert; alle Quellen werden in der Lokalzeit
  verglichen, in der sie geschrieben wurden (Logs und AWR derselben Umgebung
  liegen so auf einer Zeitachse).
* Jede Log-Datei wird pro Paketierung genau **einmal** sequentiell gelesen
  (streamend, konstanter Speicherbedarf – auch bei mehreren GB).
* ZIPs werden atomar geschrieben (`.part` → Umbenennung) und nutzen Zip64.

## Start

```bash
docker compose up -d --build
# GUI: http://localhost:8080
```

Oder ohne Compose:

```bash
docker build -t bora-rodaten-optimizer .
docker run -d -p 8080:8080 -v bora-data:/data \
  -v /pfad/zu/logs:/sources/logs:ro -e BORA_SOURCE_DIRS=/sources/logs \
  bora-rodaten-optimizer
```

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
* Getestet mit einem 4,4-GB-ZIP (18 Mio. Logzeilen): Upload, Entpacken, Analyse
  und Paketierung inkl. 4,4-GB-Eintrag im Ergebnis-ZIP.
* **Platzbedarf** auf `/data` einplanen: Archiv + entpackter Inhalt während des
  Entpackens, danach nur noch der Inhalt; bei der Paketierung zusätzlich
  temporär die Log-Ausschnitte plus die Ergebnis-ZIPs.
* Liegt ein Reverse-Proxy davor, dessen Body-Limit und Timeouts anheben, z. B.
  nginx: `client_max_body_size 0; proxy_request_buffering off; proxy_read_timeout 3600s;`

Upload per Kommandozeile (z. B. direkt vom Server):

```bash
curl -T logs.zip "http://bora-host:8080/api/upload?name=logs.zip&source=wls-prod-01"
```

## Konfiguration

| Variable | Standard | Bedeutung |
|----------|----------|-----------|
| `BORA_DATA_DIR` | `/data` | Volume für Inbox, Katalog, Ausgabe |
| `BORA_SOURCE_DIRS` | – | zusätzliche Quellverzeichnisse, `:`-getrennt (read-only genügt) |
| `BORA_ZIP_PREFIX` | `BORA` | Präfix der ZIP-Namen |
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
pip install -r requirements-dev.txt
pytest -q
BORA_DATA_DIR=./data uvicorn app.main:create_app --factory --reload --port 8080
```

Aufbau: `app/detect.py` (Kategorisierung, Oracle-Report-Parser),
`app/timestamps.py` (Zeitstempelformate), `app/catalog.py` (SQLite-Katalog),
`app/archives.py` (sicheres Entpacken), `app/packager.py` (Zeitfenster & ZIP),
`app/main.py` (Web-GUI/API).
