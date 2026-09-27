"""Laufzeitkonfiguration (ausschließlich über Umgebungsvariablen)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _paths(value: str) -> list[Path]:
    return [Path(p).resolve() for p in value.split(":") if p.strip()]


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("BORA_DATA_DIR", "/data")).resolve())
    # Zusätzliche (typischerweise read-only gemountete) Quellverzeichnisse, ":"-getrennt
    source_dirs: list[Path] = field(default_factory=lambda: _paths(os.environ.get("BORA_SOURCE_DIRS", "")))
    zip_prefix: str = field(default_factory=lambda: os.environ.get("BORA_ZIP_PREFIX", "BORA"))
    # 0 = unbegrenzt (Uploads > 4 GB werden gestreamt, ZIPs per Zip64 gelesen)
    # Tages-Pakete nach jedem Upload/Einlesen automatisch erstellen bzw. aktualisieren
    auto_package: bool = field(default_factory=lambda: os.environ.get("BORA_AUTO_PACKAGE", "1").lower() not in ("0", "false", "nein", "no"))
    # Beim Entpacken hochgeladener Archive überspringen (Java-Anwendungsarchive)
    skip_extract: tuple = field(default_factory=lambda: tuple(
        s.strip().lower() if s.strip().startswith(".") else "." + s.strip().lower()
        for s in os.environ.get("BORA_SKIP_EXTRACT", ".ear,.war,.jar,.rar").split(",") if s.strip()))
    # Puffer in Minuten um den AWR-Aufzeichnungszeitraum bei automatischen AWR-Paketen
    awr_margin_min: int = field(default_factory=lambda: int(os.environ.get("BORA_AWR_MARGIN_MIN", "0")))
    max_upload_mb: int = field(default_factory=lambda: int(os.environ.get("BORA_MAX_UPLOAD_MB", "0")))
    auth_user: str = field(default_factory=lambda: os.environ.get("BORA_USER", ""))
    auth_password: str = field(default_factory=lambda: os.environ.get("BORA_PASSWORD", ""))

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "output"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "catalog.sqlite3"

    def ensure_dirs(self) -> None:
        for d in (self.inbox_dir, self.output_dir, self.work_dir):
            d.mkdir(parents=True, exist_ok=True)
