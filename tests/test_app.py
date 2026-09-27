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
        assert names == ["BORA_2026-09-26.zip", "BORA_2026-09-27.zip"]
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
