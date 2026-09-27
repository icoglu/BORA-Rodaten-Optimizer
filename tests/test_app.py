import io
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
