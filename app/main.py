"""BORA Rohdaten-Optimizer - Web-GUI zum Sammeln, Kategorisieren und
zeitrahmengerechten Paketieren von access.log, server*.log* und AWR-Reports."""
from __future__ import annotations

import base64
import re
import secrets
import tempfile
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import anyio
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import archives, detect, packager, selfcheck
from .catalog import Catalog
from .config import Settings
from .jobs import JobRunner

BASE = Path(__file__).parent
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
UPLOAD_CHUNK = 4 * 1024 * 1024


def safe_name(name: str, fallback: str = "datei") -> str:
    name = Path(name.replace("\\", "/")).name
    name = _SAFE.sub("_", name).strip("._")
    return name or fallback


def _is_within(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_dirs()
    # Multipart-Uploads auf das Daten-Volume spoolen, nicht ins Container-/tmp
    tempfile.tempdir = str(settings.work_dir)
    catalog = Catalog(settings.db_path)
    jobs = JobRunner()
    templates = Jinja2Templates(directory=str(BASE / "templates"))
    templates.env.filters["size"] = _human_size
    templates.env.filters["dt"] = lambda v: v.strftime("%Y-%m-%d %H:%M:%S") if v else "–"

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        app.state.selfcheck = selfcheck.report(settings)
        start_scan()  # Katalog beim Start mit dem Dateisystem abgleichen
        yield

    app = FastAPI(title="BORA Rohdaten-Optimizer", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
    app.state.settings, app.state.catalog, app.state.jobs = settings, catalog, jobs
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

    # ------------------------------------------------------------ Basic-Auth
    if settings.auth_user and settings.auth_password:
        expected = f"{settings.auth_user}:{settings.auth_password}".encode()

        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            if request.url.path == "/healthz":
                return await call_next(request)
            header = request.headers.get("authorization", "")
            ok = False
            if header.lower().startswith("basic "):
                try:
                    ok = secrets.compare_digest(base64.b64decode(header[6:]), expected)
                except ValueError:
                    ok = False
            if not ok:
                return Response("Anmeldung erforderlich", 401, {"WWW-Authenticate": 'Basic realm="BORA"'})
            return await call_next(request)

    # --------------------------------------------------------------- Helfer
    def roots() -> dict[str, Path]:
        r = {"inbox": settings.inbox_dir}
        for d in settings.source_dirs:
            label = safe_name(d.name, "quelle")
            while label in r:
                label += "_"
            r[label] = d
        return r

    def repackage_days(days: set[str], progress) -> dict:
        """Tages-Pakete der betroffenen Tage neu erstellen; Tage ohne Daten mehr
        -> veraltetes ZIP entfernen. Regel: alle Dateien eines Tages (nach
        Zeitstempel im Inhalt) in genau ein ZIP BORA_JJJJ-MM-TT.zip."""
        if not settings.auto_package:
            return {}
        records = catalog.all()
        planned = packager.plan_windows(records, "day", settings.zip_prefix, require_awr=settings.require_awr)
        windows = [w for w in planned if w.start.date().isoformat() in days]
        built = packager.build(records, windows, settings.output_dir, settings.work_dir, "day",
                               lambda msg, pct=None: progress(f"Tages-Pakete: {msg}", pct)) if windows else []
        valid = {f"{w.name}.zip" for w in planned}
        removed = 0
        for p in settings.output_dir.glob("*.zip"):   # auch Tage ohne (mehr) AWR-Report entfernen
            if day_zip.match(p.name) and p.name not in valid:
                p.unlink()
                removed += 1
        out = {"tagespakete_aktualisiert": len(built)}
        if removed:
            out["tagespakete_entfernt"] = removed
        return out

    day_zip = re.compile(re.escape(settings.zip_prefix) + r"_\d{4}-\d{2}-\d{2}\.zip$")
    awr_zip = re.compile(re.escape(settings.zip_prefix) + r"_\d{4}-\d{2}-\d{2}_\d{4}-(\d{4}|\d{4}-\d{2}-\d{2}_\d{4})\.zip$")

    def repackage_awr(days: set[str], progress) -> dict:
        """Regel 1: Je AWR-Aufzeichnungszeitraum ein ZIP mit AWR-Report(s), passenden
        Log-Zeilen und überschneidenden Oracle-Reports. Neu erstellt werden Zeiträume
        auf geänderten Tagen und fehlende Pakete; nicht mehr gültige werden entfernt."""
        if not settings.auto_package:
            return {}
        records = catalog.all()
        windows = packager.plan_windows(records, "awr", settings.zip_prefix, settings.awr_margin_min,
                                             require_awr=settings.require_awr)
        todo = [w for w in windows
                if not (settings.output_dir / f"{w.name}.zip").exists()
                or any(d in days for d in detect.days_between(w.start, w.end - timedelta(seconds=1)))]
        built = packager.build(records, todo, settings.output_dir, settings.work_dir, "awr",
                               lambda msg, pct=None: progress(f"AWR-Pakete: {msg}", pct)) if todo else []
        valid = {f"{w.name}.zip" for w in windows}
        removed = 0
        for p in settings.output_dir.glob("*.zip"):
            if awr_zip.match(p.name) and p.name not in valid:
                p.unlink()
                removed += 1
        out = {"awr_pakete_aktualisiert": len(built)}
        if removed:
            out["awr_pakete_entfernt"] = removed
        return out

    def repackage(days: set[str], progress) -> dict:
        """Regel 1: AWR-Zeiträume, Regel 2: Tage."""
        out = repackage_awr(days, progress)
        out.update(repackage_days(days, progress))
        return out

    def missing_day_packages() -> set[str]:
        return {d for d in catalog.all_days()
                if not (settings.output_dir / f"{settings.zip_prefix}_{d}.zip").exists()}

    def scan_job(progress) -> dict:
        extracted = archives.extract_pending(settings.inbox_dir, progress, settings.skip_extract)
        result = catalog.scan(roots(), progress)
        if settings.auto_package:
            result.update(repackage(catalog.last_changed_days | missing_day_packages(), progress))
        if extracted["archive"] or extracted["fehler"]:
            result.update({"archive_entpackt": extracted["archive"], "dateien_aus_archiven": extracted["entpackt"]})
        if extracted["uebersprungen"]:
            result["nicht_entpackt"] = len(extracted["uebersprungen"])
        if extracted["fehler"]:
            result["archivfehler"] = "; ".join(extracted["fehler"])
        return result

    def start_scan() -> bool:
        """Scan (inkl. Entpacken) starten oder im Anschluss an den laufenden Job einreihen."""
        return jobs.submit_or_queue("Scan", scan_job)

    def upload_dir(source: str) -> tuple[str, Path]:
        label = safe_name(source, "") or f"upload-{datetime.now():%Y%m%d-%H%M%S}"
        dest = settings.inbox_dir / label
        dest.mkdir(parents=True, exist_ok=True)
        return label, dest

    def check_limit(written: int, name: str) -> None:
        if settings.max_upload_mb and written > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f"{name}: größer als {settings.max_upload_mb} MB")

    def outputs() -> list[dict]:
        items = []
        for p in sorted(settings.output_dir.glob("*.zip")):
            st = p.stat()
            items.append({"name": p.name, "size": st.st_size, "mtime": datetime.fromtimestamp(st.st_mtime)})
        return items

    def back(msg: str = "", level: str = "info") -> RedirectResponse:
        url = "/"
        if msg:
            url += "?" + urlencode({"msg": msg, "level": level})
        return RedirectResponse(url, status_code=303)

    # --------------------------------------------------------------- Seiten
    @app.get("/")
    def index(request: Request, msg: str = "", level: str = "info"):
        files = catalog.all()
        counts = {c: 0 for c in detect.CATEGORIES}
        for f in files:
            counts[f.category] += 1
        plan_day = packager.plan_windows(files, "day", settings.zip_prefix, require_awr=settings.require_awr)
        plan_awr = packager.plan_windows(files, "awr", settings.zip_prefix, require_awr=settings.require_awr)
        return templates.TemplateResponse(request, "index.html", {
            "files": files, "counts": counts, "categories": detect.CATEGORIES,
            "outputs": outputs(), "job": jobs.state, "msg": msg, "level": level,
            "plan_day": plan_day, "plan_awr": plan_awr, "roots": roots(),
            "warnings": [c for c in getattr(app.state, "selfcheck", []) if not c["ok"]],
            "auto_package": settings.auto_package, "require_awr": settings.require_awr,
            "has_awr": any(f.category == detect.AWR and packager.is_awr_report(f) for f in files),
        })

    @app.get("/api/selfcheck")
    def api_selfcheck():
        return selfcheck.run(settings)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    # --------------------------------------------------------------- Sammeln
    @app.put("/api/upload")
    async def upload_stream(request: Request, name: str, source: str = ""):
        """Streaming-Upload: Request-Body wird direkt auf das Volume geschrieben
        (kein Multipart, keine Zwischenkopie) - geeignet für ZIPs > 4 GB."""
        fname = safe_name(name)
        label, dest = upload_dir(source)
        target, part = dest / fname, dest / f".{fname}.part"
        written = 0
        try:
            async with await anyio.open_file(part, "wb") as fh:
                async for chunk in request.stream():
                    written += len(chunk)
                    check_limit(written, fname)
                    await fh.write(chunk)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        expected = request.headers.get("content-length")
        if expected and int(expected) != written:
            part.unlink(missing_ok=True)
            raise HTTPException(400, f"{fname}: Upload unvollständig ({written} von {expected} Bytes)")
        part.replace(target)
        started = start_scan()
        return {"datei": f"{label}/{fname}", "bytes": written, "scan": "gestartet" if started else "eingereiht"}

    @app.post("/upload")
    async def upload(files: list[UploadFile] = File(...), source: str = Form("")):
        """Formular-Upload (Fallback ohne JavaScript)."""
        label, dest = upload_dir(source)
        saved = 0
        for up in files:
            if not up.filename:
                continue
            fname = safe_name(up.filename)
            target, part = dest / fname, dest / f".{fname}.part"
            written = 0
            try:
                with open(part, "wb") as fh:
                    while chunk := await up.read(UPLOAD_CHUNK):
                        written += len(chunk)
                        check_limit(written, fname)
                        fh.write(chunk)
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            part.replace(target)
            saved += 1
        start_scan()
        return back(f"{saved} Datei(en) nach inbox/{label} hochgeladen. Archive werden entpackt, Scan läuft …")

    @app.post("/scan")
    def scan():
        if not start_scan():
            return back("Scan wird nach dem laufenden Job ausgeführt.", "warn")
        return back("Scan gestartet …")

    # --------------------------------------------------------- Kategorisieren
    @app.post("/files/{file_id}/category")
    def set_category(file_id: int, category: str = Form(...)):
        rec = catalog.get(file_id)
        if not rec:
            raise HTTPException(404)
        if category != "auto" and category not in detect.CATEGORIES:
            raise HTTPException(400, "Unbekannte Kategorie")
        catalog.set_override(file_id, None if category == "auto" else category)

        def job(progress):
            # Zeitraum mit dem Parser der (neuen) Kategorie ermitteln, betroffene Tage neu paketieren
            catalog.reanalyse(file_id)
            return repackage(catalog.last_changed_days | set(rec.days), progress)

        jobs.submit_or_queue("Neuanalyse", job)
        label = "automatische Kategorie" if category == "auto" else f"Kategorie → {category}"
        return back(f"{rec.rel}: {label}")

    @app.post("/files/{file_id}/delete")
    def delete_file(file_id: int):
        rec = catalog.get(file_id)
        if not rec:
            raise HTTPException(404)
        if not rec.deletable or not _is_within(settings.inbox_dir, rec.path):
            raise HTTPException(403, "Nur hochgeladene Dateien (inbox) können gelöscht werden")
        rec.path.unlink(missing_ok=True)
        catalog.remove(file_id)
        days = set(rec.days)
        jobs.submit_or_queue("Pakete", lambda p: repackage(days, p))
        return back(f"{rec.rel} gelöscht")

    # ------------------------------------------------------------- Paketieren
    @app.post("/build")
    def build(mode: str = Form("day"), margin: int = Form(0),
              date_from: str = Form(""), date_to: str = Form("")):
        if mode not in packager.MODES:
            raise HTTPException(400, "Unbekannter Modus")
        try:
            dfrom = date.fromisoformat(date_from) if date_from else None
            dto = date.fromisoformat(date_to) if date_to else None
        except ValueError:
            return back("Ungültiges Datum", "error")

        def run(progress):
            records = catalog.all()
            windows = packager.plan_windows(records, mode, settings.zip_prefix, margin, dfrom, dto,
                                            require_awr=settings.require_awr)
            if not windows:
                return {"zips": [], "hinweis": "Keine passenden Zeitfenster gefunden"}
            return {"zips": packager.build(records, windows, settings.output_dir,
                                           settings.work_dir, mode, progress)}

        if not jobs.submit("Paketierung", run):
            return back("Es läuft bereits ein Job.", "warn")
        return back("Paketierung gestartet …")

    # ---------------------------------------------------------------- Ausgabe
    def output_path(name: str) -> Path:
        if safe_name(name) != name or not name.endswith(".zip"):
            raise HTTPException(400, "Ungültiger Dateiname")
        p = settings.output_dir / name
        if not p.is_file():
            raise HTTPException(404)
        return p

    @app.get("/download/{name}")
    def download(name: str):
        return FileResponse(output_path(name), media_type="application/zip", filename=name)

    @app.post("/outputs/{name}/delete")
    def delete_output(name: str):
        output_path(name).unlink()
        return back(f"{name} gelöscht")

    @app.post("/outputs/delete-all")
    def delete_all_outputs():
        n = 0
        for p in settings.output_dir.glob("*.zip"):
            p.unlink()
            n += 1
        return back(f"{n} Paket(e) gelöscht")

    # -------------------------------------------------------------------- API
    @app.get("/api/status")
    def api_status():
        s = jobs.state
        return {"name": s.name, "running": s.running, "message": s.message, "percent": s.percent,
                "started": s.started,
                "finished": s.finished, "error": s.error, "result": s.result}

    @app.get("/api/files")
    def api_files():
        return [{"id": f.id, "root": f.root, "rel": f.rel, "category": f.category, "detected": f.detected,
                 "size": f.size, "first": f.first.isoformat() if f.first else None,
                 "last": f.last.isoformat() if f.last else None, "days": f.days, "info": f.info,
                 "error": f.error} for f in catalog.all()]

    @app.get("/api/outputs")
    def api_outputs():
        return JSONResponse([{**o, "mtime": o["mtime"].isoformat(timespec="seconds")} for o in outputs()])

    return app


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"

