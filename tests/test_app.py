import io
import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

from .conftest import ACCESS, AWR_HTML


def _client(data: Path, **kw) -> TestClient:
    return TestClient(create_app(Settings(data_dir=data, source_dirs=[], **kw)))


def test_upload_scan_build_download(tmp_path: Path):
    data = tmp_path / "data"
    with _client(data) as c:
        c.app.state.jobs.wait()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("logs/access.log", ACCESS)
            zf.writestr("../../evil/awr_1.html", AWR_HTML)
        r = c.post("/upload", data={"source": "wls01"},
                   files=[("files", ("bundle.zip", buf.getvalue(), "application/zip"))], follow_redirects=False)
        assert r.status_code == 303
        c.app.state.jobs.wait()
        status = c.get("/api/status").json()
        assert "Unzulässiger Pfad" in status["result"]["archivfehler"]  # Zip-Slip abgewiesen
        assert (data / "inbox" / "wls01" / "bundle.zip.defekt").exists()
        assert not (data / "inbox" / "wls01" / "bundle").exists() and not (tmp_path / "evil").exists()

        r = c.post("/upload", data={"source": "wls01"}, files=[
            ("files", ("access.log", ACCESS.encode(), "text/plain")),
            ("files", ("awrrpt_1_100_101.html", AWR_HTML.encode(), "text/html")),
        ], follow_redirects=False)
        assert r.status_code == 303
        c.app.state.jobs.wait()
        files = c.get("/api/files").json()
        cats = {f["rel"]: f["category"] for f in files}
        assert cats["wls01/access.log"] == "access" and cats["wls01/awrrpt_1_100_101.html"] == "awr"

        assert c.get("/").status_code == 200
        c.post("/build", data={"mode": "day"})
        c.app.state.jobs.wait()
        status = c.get("/api/status").json()
        assert status["error"] is None, status
        names = [o["name"] for o in c.get("/api/outputs").json()]
        assert names == ["BORA_2026-09-27.zip", "BORA_2026-09-27_1000-1100.zip"]  # 26.09.: kein AWR -> kein Paket
        r = c.get("/download/BORA_2026-09-27.zip")
        assert r.status_code == 200 and zipfile.ZipFile(io.BytesIO(r.content)).testzip() is None
        assert c.get("/download/..%2Fcatalog.sqlite3").status_code in (400, 404)


def test_basic_auth(tmp_path: Path):
    with _client(tmp_path / "data", auth_user="admin", auth_password="geheim") as c:
        assert c.get("/").status_code == 401
        assert c.get("/healthz").status_code == 200
        assert c.get("/", auth=("admin", "geheim")).status_code == 200


def test_streaming_upload_zip64(tmp_path: Path):
    data = tmp_path / "data"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        with zf.open("host/access.log", "w", force_zip64=True) as fh:  # Zip64-Strukturen wie bei > 4 GB
            fh.write(ACCESS.encode())
        zf.writestr("host/awrrpt_1_100_101.html", AWR_HTML)
    with _client(data) as c:
        c.app.state.jobs.wait()
        r = c.put("/api/upload", params={"name": "paket.zip", "source": "gross"}, content=buf.getvalue())
        assert r.status_code == 200 and r.json()["bytes"] == len(buf.getvalue())
        c.app.state.jobs.wait()
        cats = {f["rel"]: f["category"] for f in c.get("/api/files").json()}
        assert cats == {"gross/paket/host/access.log": "access", "gross/paket/host/awrrpt_1_100_101.html": "awr"}
        assert not (data / "inbox" / "gross" / "paket.zip").exists()  # Archiv nach Entpacken entfernt


def test_upload_limit(tmp_path: Path):
    with _client(tmp_path / "data", max_upload_mb=1) as c:
        r = c.put("/api/upload", params={"name": "x.log"}, content=b"x" * (2 * 1024 * 1024))
        assert r.status_code == 413
        assert not list((tmp_path / "data" / "inbox").rglob("*.part"))


def test_selfcheck(tmp_path: Path):
    with _client(tmp_path / "data") as c:
        checks = {x["pruefung"]: x for x in c.get("/api/selfcheck").json()}
        assert checks["Datenverzeichnis"]["ok"]
    missing = tmp_path / "fehlt"
    with TestClient(create_app(Settings(data_dir=tmp_path / "d2", source_dirs=[missing]))) as c:
        checks = {x["pruefung"]: x for x in c.get("/api/selfcheck").json()}
        assert not checks[f"Quelle {missing}"]["ok"] and "mounten" in checks[f"Quelle {missing}"]["abhilfe"]


def test_auto_day_packages(tmp_path: Path):
    """Regel: alle Dateien werden nach Zeitstempel im Inhalt pro Tag automatisch zu einem ZIP zusammengeführt."""
    data = tmp_path / "data"
    with _client(data, day_packages=True) as c:
        c.app.state.jobs.wait()
        c.put("/api/upload", params={"name": "access.log", "source": "wls01"}, content=ACCESS.encode())
        c.put("/api/upload", params={"name": "awrrpt_1_100_101.html", "source": "wls01"}, content=AWR_HTML.encode())
        c.app.state.jobs.wait()
        names = [o["name"] for o in c.get("/api/outputs").json()]
        # ohne Klick: Regel 1 (AWR-Zeitraum) + Regel 2 (nur Tage mit AWR-Report)
        assert names == ["BORA_2026-09-27.zip", "BORA_2026-09-27_1000-1100.zip"]
        z = zipfile.ZipFile(io.BytesIO(c.get("/download/BORA_2026-09-27.zip").content))
        assert {"access/wls01/access.log", "awr/wls01/awrrpt_1_100_101.html"} <= set(z.namelist())

        # Datei löschen -> betroffene Tage werden aktualisiert, leere Tage entfernt
        fid = next(f["id"] for f in c.get("/api/files").json() if f["rel"] == "wls01/access.log")
        c.post(f"/files/{fid}/delete")
        c.app.state.jobs.wait()
        # einzige Log-Datei gelöscht -> AWR-Report ohne Logs -> keine Pakete mehr
        assert c.get("/api/outputs").json() == []


def test_auto_package_can_be_disabled(tmp_path: Path):
    with _client(tmp_path / "data", auto_package=False) as c:
        c.app.state.jobs.wait()
        c.put("/api/upload", params={"name": "access.log"}, content=ACCESS.encode())
        c.app.state.jobs.wait()
        assert c.get("/api/outputs").json() == []


def test_category_change_updates_day_packages(tmp_path: Path):
    with _client(tmp_path / "data", day_packages=True) as c:
        c.app.state.jobs.wait()
        c.put("/api/upload", params={"name": "access.log", "source": "wls01"}, content=ACCESS.encode())
        c.put("/api/upload", params={"name": "awrrpt_1_100_101.html", "source": "wls01"}, content=AWR_HTML.encode())
        c.app.state.jobs.wait()
        fid = next(f["id"] for f in c.get("/api/files").json() if f["rel"] == "wls01/access.log")
        c.post(f"/files/{fid}/category", data={"category": "ignore"})
        c.app.state.jobs.wait()
        assert c.get("/api/outputs").json() == []   # Log ignoriert -> AWR ohne Logs -> kein Paket
        c.post(f"/files/{fid}/category", data={"category": "auto"})
        c.app.state.jobs.wait()
        assert [o["name"] for o in c.get("/api/outputs").json()] == ["BORA_2026-09-27.zip", "BORA_2026-09-27_1000-1100.zip"]
        z = zipfile.ZipFile(io.BytesIO(c.get("/download/BORA_2026-09-27.zip").content))
        assert "access/wls01/access.log" in z.namelist()


def test_ear_files_are_not_extracted(tmp_path: Path):
    data = tmp_path / "data"
    ear = io.BytesIO()
    with zipfile.ZipFile(ear, "w") as z:           # EAR = ZIP-Format mit Anwendung
        z.writestr("META-INF/application.xml", "<application/>")
        z.writestr("app.war", b"PK")
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w") as z:
        z.writestr("domain/servers/AdminServer/logs/access.log", ACCESS)
        z.writestr("domain/awr/awrrpt_1_100_101.html", AWR_HTML)
        z.writestr("domain/apps/bora.ear", ear.getvalue())
        z.writestr("domain/lib/treiber.jar", b"PK")
    with _client(data) as c:
        c.app.state.jobs.wait()
        c.put("/api/upload", params={"name": "export.zip", "source": "wls01"}, content=bundle.getvalue())
        c.app.state.jobs.wait()
        assert c.get("/api/status").json()["result"]["nicht_entpackt"] == 2
        c.put("/api/upload", params={"name": "bora.ear", "source": "direkt"}, content=ear.getvalue())
        c.app.state.jobs.wait()
        extracted = data / "inbox" / "wls01" / "export" / "domain"
        assert (extracted / "servers/AdminServer/logs/access.log").exists()
        assert not (extracted / "apps/bora.ear").exists() and not (extracted / "lib/treiber.jar").exists()
        # direkt hochgeladene EAR bleibt unverändert liegen, wird nicht entpackt und ignoriert
        assert (data / "inbox" / "direkt" / "bora.ear").read_bytes() == ear.getvalue()
        assert not (data / "inbox" / "direkt" / "bora").exists()
        cats = {f["rel"]: f["category"] for f in c.get("/api/files").json()}
        assert cats["direkt/bora.ear"] == "ignore"
        z = zipfile.ZipFile(io.BytesIO(c.get("/download/BORA_2026-09-27_1000-1100.zip").content))
        assert not any(n.endswith((".ear", ".jar", ".war")) for n in z.namelist())


def test_rule_one_awr_period_collects_logs_and_other_reports(tmp_path: Path):
    """Regel 1: AWR-HTML -> Aufzeichnungszeitraum erkennen, passende Logs und überschneidende
    Oracle-Reports automatisch zusammenführen."""
    from .conftest import SERVER
    from .test_oracle_reports import ADDM_TXT, ASH_HTML
    with _client(tmp_path / "data") as c:
        c.app.state.jobs.wait()
        for name, body in [("access.log", ACCESS), ("server1.log", SERVER), ("awrrpt_1_100_101.html", AWR_HTML),
                           ("addmrpt_1.txt", ADDM_TXT), ("ashrpt_1.html", ASH_HTML)]:
            c.put("/api/upload", params={"name": name, "source": "wls01"}, content=body.encode())
        c.app.state.jobs.wait()
        names = [o["name"] for o in c.get("/api/outputs").json()]
        assert "BORA_2026-09-27_1000-1100.zip" in names                 # AWR 10:00:05-11:00:07
        assert "BORA_2026-09-27_1000-1300.zip" not in names             # ADDM hängt am AWR, kein eigenes Paket
        assert "BORA_2026-09-27_0930-0945.zip" not in names             # ASH ohne AWR-Überschneidung: kein Paket
        z = zipfile.ZipFile(io.BytesIO(c.get("/download/BORA_2026-09-27_1000-1100.zip").content))
        n = set(z.namelist())
        assert {"awr/wls01/awrrpt_1_100_101.html", "awr/wls01/addmrpt_1.txt",
                "access/wls01/access.log", "server/wls01/server1.log"} <= n
        assert "awr/wls01/ashrpt_1.html" not in n                      # 09:30-09:45 liegt außerhalb
        assert z.read("access/wls01/access.log").count(b"\n") == 1      # nur 10:15 aus dem Zeitraum
        srv = z.read("server/wls01/server1.log")
        assert b"NullPointerException" in srv and b"Nachmittag" not in srv


def test_awr_package_removed_when_report_deleted(tmp_path: Path):
    with _client(tmp_path / "data") as c:
        c.app.state.jobs.wait()
        c.put("/api/upload", params={"name": "access.log", "source": "wls01"}, content=ACCESS.encode())
        c.put("/api/upload", params={"name": "awrrpt_1_100_101.html", "source": "wls01"}, content=AWR_HTML.encode())
        c.app.state.jobs.wait()
        assert "BORA_2026-09-27_1000-1100.zip" in [o["name"] for o in c.get("/api/outputs").json()]
        fid = next(f["id"] for f in c.get("/api/files").json() if f["rel"].endswith(".html"))
        c.post(f"/files/{fid}/delete")
        c.app.state.jobs.wait()
        assert c.get("/api/outputs").json() == []  # kein AWR-Report mehr -> kein Paket


def test_no_awr_no_package(tmp_path: Path):
    """Findet sich kein AWR-Report, wird kein Paket erzeugt - weder automatisch noch per Button."""
    from .conftest import SERVER
    from .test_oracle_reports import ASH_HTML
    with _client(tmp_path / "data") as c:
        c.app.state.jobs.wait()
        for name, body in [("access.log", ACCESS), ("server1.log", SERVER), ("ashrpt_1.html", ASH_HTML)]:
            c.put("/api/upload", params={"name": name}, content=body.encode())
        c.app.state.jobs.wait()
        assert c.get("/api/outputs").json() == []
        assert "Keine AWR-Reports gefunden" in c.get("/").text
        for mode in ("day", "awr"):
            c.post("/build", data={"mode": mode})
            c.app.state.jobs.wait()
            assert c.get("/api/outputs").json() == []


def test_require_awr_can_be_disabled(tmp_path: Path):
    with _client(tmp_path / "data", require_awr=False, day_packages=True) as c:
        c.app.state.jobs.wait()
        c.put("/api/upload", params={"name": "access.log"}, content=ACCESS.encode())
        c.app.state.jobs.wait()
        assert [o["name"] for o in c.get("/api/outputs").json()] == ["BORA_2026-09-26.zip", "BORA_2026-09-27.zip"]


def test_reset_deletes_everything(tmp_path: Path):
    data = tmp_path / "data"
    src = tmp_path / "quelle"
    src.mkdir()
    (src / "server9.log").write_text("####<Sep 27, 2026 10:30:00,000 AM CEST> <Info> <x>\n")
    with TestClient(create_app(Settings(data_dir=data, source_dirs=[src]))) as c:
        c.app.state.jobs.wait()
        c.put("/api/upload", params={"name": "access.log", "source": "wls01"}, content=ACCESS.encode())
        c.put("/api/upload", params={"name": "awrrpt_1_100_101.html", "source": "wls01"}, content=AWR_HTML.encode())
        c.app.state.jobs.wait()
        assert c.get("/api/outputs").json() and c.get("/api/files").json()

        c.post("/reset", data={"confirm": ""})              # ohne Bestätigung: nichts passiert
        c.app.state.jobs.wait()
        assert c.get("/api/outputs").json()

        c.post("/reset", data={"confirm": "RESET"})
        c.app.state.jobs.wait()
        assert c.get("/api/status").json()["result"]["katalog"] == "geleert"
        assert c.get("/api/outputs").json() == [] and c.get("/api/files").json() == []
        assert list((data / "inbox").iterdir()) == [] and list((data / "output").iterdir()) == []
        assert (src / "server9.log").exists()               # eingebundene Quelle unberührt
        c.post("/scan")
        c.app.state.jobs.wait()
        assert [f["rel"] for f in c.get("/api/files").json()] == ["server9.log"]


def test_default_only_awr_packages_with_snap_time_lines(tmp_path: Path):
    """Standard: nur AWR-Pakete; aus den Logs nur die Zeilen zwischen Begin und End Snap Time."""
    from .conftest import SERVER
    with _client(tmp_path / "data") as c:
        c.app.state.jobs.wait()
        for name, body in [("access.log", ACCESS), ("server1.log", SERVER), ("awrrpt_1_100_101.html", AWR_HTML)]:
            c.put("/api/upload", params={"name": name, "source": "wls01"}, content=body.encode())
        c.app.state.jobs.wait()
        assert [o["name"] for o in c.get("/api/outputs").json()] == ["BORA_2026-09-27_1000-1100.zip"]  # kein Tages-Paket
        z = zipfile.ZipFile(io.BytesIO(c.get("/download/BORA_2026-09-27_1000-1100.zip").content))
        access, server = z.read("access/wls01/access.log"), z.read("server/wls01/server1.log")
        assert len(access) < len(ACCESS) and len(server) < len(SERVER)                      # nicht die ganzen Dateien
        assert access == b'10.0.0.3 - - [27/Sep/2026:10:15:00 +0200] "GET /bora/list HTTP/1.1" 200 2048\n'
        assert b"10:05:00" in server and b"10:20:00" in server and b"1:05:00,000 PM" not in server


def test_awr_without_logs_no_package(tmp_path: Path):
    """AWR-Report ohne passende Log-Zeilen in der Snap Time -> kein Paket; kommen Logs dazu -> Paket."""
    from .conftest import SERVER_ROTATED
    with _client(tmp_path / "data") as c:
        c.app.state.jobs.wait()
        c.put("/api/upload", params={"name": "awrrpt_1_100_101.html", "source": "db"}, content=AWR_HTML.encode())
        c.app.state.jobs.wait()
        assert c.get("/api/outputs").json() == []                      # nur AWR
        c.put("/api/upload", params={"name": "server1.log", "source": "wls01"}, content=SERVER_ROTATED.encode())
        c.app.state.jobs.wait()
        assert c.get("/api/outputs").json() == []                      # Log nur vom 26.09. 23:00 -> passt nicht
        c.post("/build", data={"mode": "awr"})                         # auch per Button nicht
        c.app.state.jobs.wait()
        assert c.get("/api/outputs").json() == []
        assert "kein Paket" in c.get("/api/status").json()["result"]["hinweis"]
        c.put("/api/upload", params={"name": "access.log", "source": "wls01"}, content=ACCESS.encode())
        c.app.state.jobs.wait()
        assert [o["name"] for o in c.get("/api/outputs").json()] == ["BORA_2026-09-27_1000-1100.zip"]


def test_access_or_server_log_suffices(tmp_path: Path):
    """Zum AWR-Report genügt ein Access- ODER ein Server-Log mit Zeilen in der Snap Time."""
    from .conftest import SERVER
    for name, body, entry in [("access.log", ACCESS, "access/wls01/access.log"),
                              ("server1.log", SERVER, "server/wls01/server1.log")]:
        with _client(tmp_path / name) as c:
            c.app.state.jobs.wait()
            c.put("/api/upload", params={"name": "awrrpt_1_100_101.html", "source": "db"}, content=AWR_HTML.encode())
            c.put("/api/upload", params={"name": name, "source": "wls01"}, content=body.encode())
            c.app.state.jobs.wait()
            assert [o["name"] for o in c.get("/api/outputs").json()] == ["BORA_2026-09-27_1000-1100.zip"], name
            z = zipfile.ZipFile(io.BytesIO(c.get("/download/BORA_2026-09-27_1000-1100.zip").content))
            assert {entry, "awr/db/awrrpt_1_100_101.html"} <= set(z.namelist())


def test_catalog_section_hidden(tmp_path: Path):
    with _client(tmp_path / "data") as c:
        c.app.state.jobs.wait()
        c.put("/api/upload", params={"name": "access.log"}, content=ACCESS.encode())
        c.app.state.jobs.wait()
        html = c.get("/").text
        assert "Katalog &amp; Kategorisierung" not in html and 'class="path"' not in html
        assert "Alles zurücksetzen und löschen" in html
        assert c.get("/api/files").json()          # Katalog im Hintergrund weiterhin vorhanden


def test_old_day_packages_without_awr_are_removed(tmp_path: Path):
    """Altbestand aus früheren Versionen (Tages-Paket ohne AWR) wird beim Einlesen entfernt."""
    data = tmp_path / "data"
    (data / "output").mkdir(parents=True)
    (data / "output" / "BORA_2026-04-10.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)   # altes Paket
    with _client(data) as c:
        c.app.state.jobs.wait()                                          # Start-Scan räumt auf
        assert c.get("/api/status").json()["result"]["tagespakete_entfernt"] == 1
        assert c.get("/api/outputs").json() == []
        c.put("/api/upload", params={"name": "access.log"}, content=ACCESS.encode())
        c.app.state.jobs.wait()
        html = c.get("/").text
        assert "0 AWR-Reports" in html and "1 Access-Logs" in html


def test_no_redundancy_in_packages(tmp_path: Path):
    """Identische Dateien und derselbe AWR-Report als .html + .txt landen nur einmal im Paket."""
    from .conftest import AWR_TXT, SERVER
    awr_txt_same_snaps = AWR_TXT.replace("102 27-Sep-26 12:59:50", "100 27-Sep-26 10:00:05").replace(
        "103 27-Sep-26 14:00:10", "101 27-Sep-26 11:00:07")
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w") as z:
        z.writestr("logs/access.log", ACCESS)                    # identisch zum Einzel-Upload
        z.writestr("logs/server1.log", SERVER)
    with _client(tmp_path / "data") as c:
        c.app.state.jobs.wait()
        c.put("/api/upload", params={"name": "access.log", "source": "wls01"}, content=ACCESS.encode())
        c.put("/api/upload", params={"name": "access_kopie.log", "source": "wls01"}, content=ACCESS.encode())
        c.put("/api/upload", params={"name": "export.zip", "source": "wls01"}, content=bundle.getvalue())
        c.put("/api/upload", params={"name": "awrrpt_1_100_101.html", "source": "db"}, content=AWR_HTML.encode())
        c.put("/api/upload", params={"name": "awrrpt_1_100_101.txt", "source": "db"}, content=awr_txt_same_snaps.encode())
        c.put("/api/upload", params={"name": "awrrpt_kopie.html", "source": "db2"}, content=AWR_HTML.encode())
        c.app.state.jobs.wait()
        z = zipfile.ZipFile(io.BytesIO(c.get("/download/BORA_2026-09-27_1000-1100.zip").content))
        names = [n for n in z.namelist() if n != "manifest.json"]
        assert sum(n.startswith("access/") for n in names) == 1, names   # 3x gleiche access.log -> 1x
        assert sum(n.startswith("server/") for n in names) == 1, names
        assert [n for n in names if n.startswith("awr/")] == ["awr/db/awrrpt_1_100_101.html"]  # HTML behalten
        manifest = json.loads(z.read("manifest.json"))
        awr = next(e for e in manifest["dateien"] if e["kategorie"] == "awr")
        assert set(awr["duplikate_ausgelassen"]) == {"inbox/db/awrrpt_1_100_101.txt", "inbox/db2/awrrpt_kopie.html"}
        acc = next(e for e in manifest["dateien"] if e["kategorie"] == "access")
        assert len(acc["duplikate_ausgelassen"]) == 2


def test_different_content_is_kept(tmp_path: Path):
    """Nur echte Duplikate werden ausgelassen - unterschiedliche Logs zweier Hosts bleiben beide."""
    other = ACCESS.replace("10.0.0.3", "10.9.9.9")
    with _client(tmp_path / "data") as c:
        c.app.state.jobs.wait()
        c.put("/api/upload", params={"name": "access.log", "source": "wls01"}, content=ACCESS.encode())
        c.put("/api/upload", params={"name": "access.log", "source": "wls02"}, content=other.encode())
        c.put("/api/upload", params={"name": "awrrpt_1_100_101.html", "source": "db"}, content=AWR_HTML.encode())
        c.app.state.jobs.wait()
        z = zipfile.ZipFile(io.BytesIO(c.get("/download/BORA_2026-09-27_1000-1100.zip").content))
        assert {"access/wls01/access.log", "access/wls02/access.log"} <= set(z.namelist())


def test_other_files_with_same_date_are_packed(tmp_path: Path):
    """Sonstige Dateien (weder Log noch Oracle-Report) mit passendem Datum kommen ganz ins Paket."""
    threaddump = "2026-09-27 10:31:12\nFull thread dump OpenJDK 64-Bit Server VM:\n\"main\" prio=5\n"
    with _client(tmp_path / "data") as c:
        c.app.state.jobs.wait()
        for name, body in [("access.log", ACCESS), ("awrrpt_1_100_101.html", AWR_HTML),
                           ("threaddump.txt", threaddump),                      # Datum aus Inhalt
                           ("gc_20260927.csv", "heap;used\n1;2\n"),             # Datum aus Dateiname
                           ("nmon_27.09.2026.txt", "AAA,host,x\n"),             # Datum aus Dateiname (dt.)
                           ("sar_2026-09-26.txt", "Linux 5.4\n"),               # anderer Tag -> nicht
                           ("notizen.txt", "ohne Datum\n")]:                     # kein Datum -> nicht
            c.put("/api/upload", params={"name": name, "source": "wls01"}, content=body.encode())
        c.put("/api/upload", params={"name": "heap_20260927.bin", "source": "wls01"}, content=b"\0\1\2" * 10)
        c.app.state.jobs.wait()
        z = zipfile.ZipFile(io.BytesIO(c.get("/download/BORA_2026-09-27_1000-1100.zip").content))
        other = sorted(n for n in z.namelist() if n.startswith("sonstige/"))
        assert other == ["sonstige/wls01/gc_20260927.csv", "sonstige/wls01/heap_20260927.bin",
                         "sonstige/wls01/nmon_27.09.2026.txt", "sonstige/wls01/threaddump.txt"]
        assert z.read("sonstige/wls01/threaddump.txt").decode() == threaddump      # ganz, unverändert
        m = {e["eintrag"]: e for e in json.loads(z.read("manifest.json"))["dateien"]}
        assert m["sonstige/wls01/threaddump.txt"]["datum_aus"] == "inhalt"
        assert m["sonstige/wls01/gc_20260927.csv"]["datum_aus"] == "dateiname (nur Datum)"
        assert "sonstige Dateien mit Datum" in c.get("/").text


def test_other_files_alone_create_no_package(tmp_path: Path):
    with _client(tmp_path / "data") as c:
        c.app.state.jobs.wait()
        c.put("/api/upload", params={"name": "gc_20260927.csv"}, content=b"a;b\n")
        c.put("/api/upload", params={"name": "awrrpt_1_100_101.html"}, content=AWR_HTML.encode())
        c.app.state.jobs.wait()
        assert c.get("/api/outputs").json() == []      # AWR + sonstige Datei, aber kein Log -> kein Paket


def test_date_from_name():
    from datetime import datetime
    from app.detect import date_from_name
    assert date_from_name("gc_20260927.log") == datetime(2026, 9, 27)
    assert date_from_name("dump-2026-09-27_1030.txt") == datetime(2026, 9, 27, 10, 30)
    assert date_from_name("jstack_20260927103015.txt") == datetime(2026, 9, 27, 10, 30, 15)
    assert date_from_name("gc_20260927_1.log") == datetime(2026, 9, 27)
    assert date_from_name("export_27.09.2026.csv") == datetime(2026, 9, 27)
    assert date_from_name("server1.log00001") is None and date_from_name("v20261399.txt") is None


def test_other_files_by_original_file_time(tmp_path: Path):
    """Ohne Datum in Inhalt/Name zählt das Original-Änderungsdatum (Browser: lastModified, ZIP-Eintrag)."""
    from datetime import datetime
    ms = datetime(2026, 9, 27, 10, 45).timestamp() * 1000
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w") as z:
        z.writestr(zipfile.ZipInfo("export/konfig.xml", date_time=(2026, 9, 27, 10, 20, 0)), "<cfg/>")
        z.writestr(zipfile.ZipInfo("export/frueh.xml", date_time=(2026, 9, 27, 9, 0, 0)), "<frueh/>")  # vor dem Zeitraum
        z.writestr(zipfile.ZipInfo("export/alt.xml", date_time=(2025, 1, 1, 9, 0, 0)), "<alt/>")
    with _client(tmp_path / "data") as c:
        c.app.state.jobs.wait()
        c.put("/api/upload", params={"name": "access.log", "source": "wls01"}, content=ACCESS.encode())
        c.put("/api/upload", params={"name": "awrrpt_1_100_101.html", "source": "wls01"}, content=AWR_HTML.encode())
        c.put("/api/upload", params={"name": "bild.png", "source": "wls01", "mtime": ms}, content=b"\x89PNG\0\0")
        c.put("/api/upload", params={"name": "unbekannt.dat", "source": "wls01"}, content=b"\0\1")  # ohne mtime
        c.put("/api/upload", params={"name": "export.zip", "source": "wls01"}, content=bundle.getvalue())
        c.app.state.jobs.wait()
        z = zipfile.ZipFile(io.BytesIO(c.get("/download/BORA_2026-09-27_1000-1100.zip").content))
        other = sorted(n for n in z.namelist() if n.startswith("sonstige/"))
        assert other == ["sonstige/wls01/bild.png", "sonstige/wls01/export/export/konfig.xml"], other
        m = {e["eintrag"]: e for e in json.loads(z.read("manifest.json"))["dateien"]}
        assert m["sonstige/wls01/bild.png"]["datum_aus"] == "dateizeit"


def test_other_files_must_fit_time_frame(tmp_path: Path):
    """Sonstige Dateien kommen in das Paket, dessen Zeitrahmen sie treffen - nicht nur nach Tag."""
    from .conftest import AWR_TXT, SERVER
    with _client(tmp_path / "data") as c:
        c.app.state.jobs.wait()
        for name, body in [("server1.log", SERVER), ("awrrpt_1_100_101.html", AWR_HTML), ("awrrpt_1_102_103.txt", AWR_TXT),
                           ("dump_2026-09-27_1030.txt", "Full thread dump A\n"),           # 10:30 -> 1000-1100
                           ("dump_2026-09-27_1330.txt", "Full thread dump B\n"),           # 13:30 -> 1259-1400
                           ("dump_2026-09-27_1600.txt", "Full thread dump C\n"),           # 16:00 -> keins
                           ("gc.log", "2026-09-27 10:40:00 GC pause\n2026-09-27 13:10:00 GC pause\n"),  # beide
                           ("gc_20260927.csv", "heap;used\n")]:                            # nur Datum -> beide
            c.put("/api/upload", params={"name": name, "source": "wls01"}, content=body.encode())
        c.app.state.jobs.wait()

        def other(pkg):
            z = zipfile.ZipFile(io.BytesIO(c.get(f"/download/{pkg}").content))
            return sorted(n.split("/")[-1] for n in z.namelist() if n.startswith("sonstige/"))

        assert other("BORA_2026-09-27_1000-1100.zip") == ["dump_2026-09-27_1030.txt", "gc.log", "gc_20260927.csv"]
        assert other("BORA_2026-09-27_1259-1400.zip") == ["dump_2026-09-27_1330.txt", "gc.log", "gc_20260927.csv"]


def test_content_check_of_other_files(tmp_path: Path):
    """Inhalt wird geprüft: Zeitangaben überall in der Zeile, einzelne Ausreißer-Daten dehnen den
    Zeitrahmen nicht, und bei Widerspruch zum Dateinamen zählt der Inhalt."""
    long_json = '{"meta":"' + "x" * 600 + '","ts":"2026-09-27T10:25:00Z","v":1}\n'          # Datum weit hinten
    copyright_ = "Copyright 2019-01-01 Firma\n2026-09-27 13:30:00 Messung\n"                 # Ausreißer 2019
    wrong_name = "2026-09-27 10:40:00 wirklich vom 27.09.\n"                                  # Name sagt 10.04.
    html_export = "<html><body><table><tr><td>Stand: 27.09.2026 10:50</td></tr></table></body></html>\n"
    other_day = "<x><time>2026-09-26T10:30:00</time></x>\n"                                  # anderer Tag
    with _client(tmp_path / "data") as c:
        c.app.state.jobs.wait()
        for name, body in [("access.log", ACCESS), ("awrrpt_1_100_101.html", AWR_HTML),
                           ("metrics.json", long_json), ("messung.txt", copyright_),
                           ("export_2026-04-10.txt", wrong_name), ("oem_export.html", html_export),
                           ("config_dump.xml", other_day)]:
            c.put("/api/upload", params={"name": name, "source": "wls01"}, content=body.encode())
        c.app.state.jobs.wait()
        z = zipfile.ZipFile(io.BytesIO(c.get("/download/BORA_2026-09-27_1000-1100.zip").content))
        other = sorted(n.split("/")[-1] for n in z.namelist() if n.startswith("sonstige/"))
        # messung.txt: nur 2019 + 13:30 belegt -> nicht im 10-11-Uhr-Paket; config_dump.xml: 26.09.
        assert other == ["export_2026-04-10.txt", "metrics.json", "oem_export.html"], other
        m = {e["eintrag"].split("/")[-1]: e for e in json.loads(z.read("manifest.json"))["dateien"]}
        assert m["metrics.json"]["datum_aus"] == "inhalt"
        assert m["export_2026-04-10.txt"]["inhaltspruefung"].startswith("Inhalt maßgeblich")
        assert "2026-04-10" in m["export_2026-04-10.txt"]["inhaltspruefung"]
        assert m["oem_export.html"]["inhaltspruefung"] == "Inhalt passt"


def test_find_all_timestamps_anywhere():
    from datetime import datetime
    from app.timestamps import find_all
    stamps, dates = find_all('a;b;c;"2026-09-27 10:15:00";x;27.09.2026;<d>2026-09-28</d>')
    assert datetime(2026, 9, 27, 10, 15) in stamps
    assert set(dates) == {datetime(2026, 9, 27), datetime(2026, 9, 28)}
    assert find_all("Version 1.2.3 build 4711") == ([], [])


def _docx(text: str) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f"<w:document><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>")
    return b.getvalue()


def _xlsx(cell: str) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/sharedStrings.xml", f'<sst><si><t>Zeitpunkt</t></si><si><t>{cell}</t></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml", "<worksheet><sheetData/></worksheet>")
    return b.getvalue()


def _pdf(page_text: str, created: str) -> bytes:
    import zlib
    stream = zlib.compress(f"BT /F1 12 Tf 72 712 Td [({page_text[:10]}) -250 ({page_text[10:]})] TJ ET".encode())
    return (b"%PDF-1.4\n1 0 obj << /Length " + str(len(stream)).encode() + b" /Filter /FlateDecode >>\nstream\n" + stream +
            b"\nendstream\nendobj\n2 0 obj << /CreationDate (D:" + created.encode() + b") >>\nendobj\n%%EOF\n")


def test_reads_contents_of_office_pdf_binary_and_compressed(tmp_path: Path):
    """Inhalte werden gelesen und auf das Datum geprüft - auch Office, PDF, Binär, bz2/xz."""
    import bz2
    import lzma
    from .conftest import SERVER
    files = {
        "bericht.docx": _docx("Messung vom 27.09.2026 10:35 Uhr"),                  # passt
        "auswertung.xlsx": _xlsx("2026-09-27 10:12:00"),                             # passt
        "report.pdf": _pdf("27.09.2026 10:20", "20250101090000"),                    # Seite passt
        "dump.bin": b"\0\0\1HEADER timestamp=2026-09-27 10:44:00 end\0\0\2",       # Textstelle passt
        "alt.docx": _docx("Protokoll vom 26.09.2026 10:35"),                         # anderer Tag
        "alt.bin": b"\0\0created 2026-09-26 10:44:00\0",                             # anderer Tag
        "server1.log.bz2": bz2.compress(SERVER.encode()),                            # Log, bz2
        "server2.log.xz": lzma.compress(SERVER.replace("<host>", "<host2>").encode()),  # Log, xz
    }
    with _client(tmp_path / "data") as c:
        c.app.state.jobs.wait()
        c.put("/api/upload", params={"name": "awrrpt_1_100_101.html", "source": "db"}, content=AWR_HTML.encode())
        for name, body in files.items():
            c.put("/api/upload", params={"name": name, "source": "wls01"}, content=body)
        c.app.state.jobs.wait()
        z = zipfile.ZipFile(io.BytesIO(c.get("/download/BORA_2026-09-27_1000-1100.zip").content))
        names = set(z.namelist())
        other = sorted(n.split("/")[-1] for n in names if n.startswith("sonstige/"))
        assert other == ["auswertung.xlsx", "bericht.docx", "dump.bin", "report.pdf"], other
        assert {"server/wls01/server1.log", "server/wls01/server2.log"} <= names     # bz2/xz entpackt
        assert b"NullPointerException" in z.read("server/wls01/server1.log")
        m = {e["eintrag"].split("/")[-1]: e for e in json.loads(z.read("manifest.json"))["dateien"]}
        assert m["bericht.docx"]["inhalt_gelesen_als"] == "office"
        assert m["report.pdf"]["inhalt_gelesen_als"] == "pdf"
        assert m["dump.bin"]["inhalt_gelesen_als"] == "binary"
        assert all(m[n]["datum_aus"] == "inhalt" for n in other)


def test_log_content_checked_in_whole_line(tmp_path: Path):
    """Logs mit Zeitstempel mitten in der Zeile (z.B. JSON-Logs) werden ebenfalls im Inhalt geprüft
    und zeilengenau zugeschnitten; jede Datei im Manifest trägt ihr Prüfergebnis."""
    pad = "x" * 300                                       # Zeitstempel erst nach > 256 Zeichen
    lines = [f'{{"level":"INFO","ctx":"{pad}","time":"2026-09-27T{h}:00","msg":"m{h}"}}' for h in ("09:30", "10:30", "12:30")]
    json_log = "\n".join(lines) + "\n"
    with _client(tmp_path / "data") as c:
        c.app.state.jobs.wait()
        c.put("/api/upload", params={"name": "server_json.log", "source": "wls01"}, content=json_log.encode())
        c.put("/api/upload", params={"name": "awrrpt_1_100_101.html", "source": "db"}, content=AWR_HTML.encode())
        c.app.state.jobs.wait()
        f = next(x for x in c.get("/api/files").json() if x["rel"].endswith("server_json.log"))
        assert f["category"] == "server" and f["info"]["ts_mode"] == "ganze Zeile" and not f["error"]
        z = zipfile.ZipFile(io.BytesIO(c.get("/download/BORA_2026-09-27_1000-1100.zip").content))
        body = z.read("server/wls01/server_json.log").decode()
        assert "m10:30" in body and "m09:30" not in body and "m12:30" not in body   # nur 10:30
        for e in json.loads(z.read("manifest.json"))["dateien"]:
            assert e.get("inhaltspruefung"), e["eintrag"]                          # jede Datei geprüft
        srv = next(e for e in json.loads(z.read("manifest.json"))["dateien"] if e["kategorie"] == "server")
        assert srv["inhaltspruefung"].startswith("zeilengenau: 1 von 3 Zeilen")
