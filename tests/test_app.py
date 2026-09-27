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
    with _client(data) as c:
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
        names = [o["name"] for o in c.get("/api/outputs").json()]
        assert names == ["BORA_2026-09-27.zip", "BORA_2026-09-27_1000-1100.zip"]
        for n in names:
            z = zipfile.ZipFile(io.BytesIO(c.get(f"/download/{n}").content))
            assert not any(e.startswith("access/") for e in z.namelist())


def test_auto_package_can_be_disabled(tmp_path: Path):
    with _client(tmp_path / "data", auto_package=False) as c:
        c.app.state.jobs.wait()
        c.put("/api/upload", params={"name": "access.log"}, content=ACCESS.encode())
        c.app.state.jobs.wait()
        assert c.get("/api/outputs").json() == []


def test_category_change_updates_day_packages(tmp_path: Path):
    with _client(tmp_path / "data") as c:
        c.app.state.jobs.wait()
        c.put("/api/upload", params={"name": "access.log", "source": "wls01"}, content=ACCESS.encode())
        c.put("/api/upload", params={"name": "awrrpt_1_100_101.html", "source": "wls01"}, content=AWR_HTML.encode())
        c.app.state.jobs.wait()
        fid = next(f["id"] for f in c.get("/api/files").json() if f["rel"] == "wls01/access.log")
        c.post(f"/files/{fid}/category", data={"category": "ignore"})
        c.app.state.jobs.wait()
        assert [o["name"] for o in c.get("/api/outputs").json()] == ["BORA_2026-09-27.zip", "BORA_2026-09-27_1000-1100.zip"]
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
        z = zipfile.ZipFile(io.BytesIO(c.get("/download/BORA_2026-09-27.zip").content))
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
    with _client(tmp_path / "data", require_awr=False) as c:
        c.app.state.jobs.wait()
        c.put("/api/upload", params={"name": "access.log"}, content=ACCESS.encode())
        c.app.state.jobs.wait()
        assert [o["name"] for o in c.get("/api/outputs").json()] == ["BORA_2026-09-26.zip", "BORA_2026-09-27.zip"]
