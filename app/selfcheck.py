"""Start-Diagnose im Container: Ergebnis erscheint in ``docker logs`` und
unter ``/api/selfcheck``. Probleme verhindern den Start nicht, sondern werden
mit konkreter Abhilfe gemeldet."""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from .config import Settings

log = logging.getLogger("uvicorn.error")
MIN_FREE_GB = 5


def run(settings: Settings) -> list[dict]:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, hint: str = "") -> None:
        checks.append({"pruefung": name, "ok": ok, "detail": detail, "abhilfe": "" if ok else hint})

    # Datenverzeichnis beschreibbar?
    try:
        with tempfile.NamedTemporaryFile(dir=settings.data_dir, prefix=".selfcheck-"):
            pass
        add("Datenverzeichnis", True, f"{settings.data_dir} beschreibbar")
    except OSError as exc:
        add("Datenverzeichnis", False, f"{settings.data_dir}: {exc}",
            "Volume für /data einbinden (z.B. -v bora-data:/data) bzw. Rechte für UID 10001 setzen")

    # Freier Speicher
    try:
        free_gb = shutil.disk_usage(settings.data_dir).free / 2**30
        add("Speicherplatz", free_gb >= MIN_FREE_GB, f"{free_gb:.1f} GB frei auf /data",
            f"Mindestens {MIN_FREE_GB} GB empfohlen - große Uploads/Pakete brauchen ein Vielfaches der Log-Größe")
    except OSError as exc:
        add("Speicherplatz", False, str(exc), "Volume für /data prüfen")

    # Quellverzeichnisse
    for d in settings.source_dirs:
        if not d.is_dir():
            add(f"Quelle {d}", False, "nicht vorhanden",
                f"Verzeichnis mounten, z.B. -v /pfad/zu/logs:{d}:ro")
        elif not os.access(d, os.R_OK | os.X_OK):
            add(f"Quelle {d}", False, "nicht lesbar", "Leserechte für UID 10001 (bzw. 'other') vergeben")
        else:
            n = sum(1 for p in Path(d).rglob("*")
                    if p.is_file() and not any(x.startswith(".") for x in p.relative_to(d).parts))
            add(f"Quelle {d}", True, f"{n} Datei(en)")

    # Zugangsschutz
    auth = bool(settings.auth_user and settings.auth_password)
    add("Zugangsschutz", True, "Basic-Auth aktiv" if auth else "keiner (BORA_USER/BORA_PASSWORD setzen, falls nötig)")
    return checks


def report(settings: Settings) -> list[dict]:
    checks = run(settings)
    port = os.environ.get("BORA_PORT", "8088")
    log.info("=" * 64)
    log.info("BORA Rohdaten-Optimizer - Start-Diagnose")
    for c in checks:
        line = f"  [{'OK ' if c['ok'] else 'WARN'}] {c['pruefung']}: {c['detail']}"
        (log.info if c["ok"] else log.warning)(line)
        if c["abhilfe"]:
            log.warning("         -> %s", c["abhilfe"])
    log.info("GUI: http://<host>:%s", port)
    log.info("=" * 64)
    return checks
