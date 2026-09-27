"""Sicheres Entpacken hochgeladener Archive (ZIP inkl. Zip64 > 4 GB, TAR/TAR.GZ).

Das Entpacken läuft im Hintergrund-Job, nicht im HTTP-Request, damit auch
mehrere Gigabyte große Pakete ohne Timeouts verarbeitet werden.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import Callable

Progress = Callable[..., None]  # progress(meldung, prozent=None)

ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz")
BROKEN_SUFFIX = ".defekt"
# Java-Anwendungsarchive: werden beim Entpacken übersprungen (nicht auf die Platte geschrieben)
# und selbst nie entpackt - sie enthalten keine Log-/Report-Daten.
DEFAULT_SKIP = (".ear", ".war", ".jar", ".rar")
CHUNK = 4 * 1024 * 1024
_RX_STEM = re.compile(r"\.(zip|tar|tar\.gz|tgz)$", re.I)


class ArchiveError(ValueError):
    pass


def is_archive(path: Path) -> bool:
    return path.name.lower().endswith(ARCHIVE_SUFFIXES)


def _check_members(dest: Path, names: list[str]) -> None:
    base = dest.resolve()
    for name in names:
        try:
            (base / name).resolve().relative_to(base)
        except ValueError:
            raise ArchiveError(f"Unzulässiger Pfad im Archiv: {name}") from None


def _check_space(dest: Path, needed: int) -> None:
    free = shutil.disk_usage(dest).free
    if needed > free:
        raise ArchiveError(f"Zu wenig Speicherplatz: benötigt {needed / 2**30:.1f} GB, "
                           f"frei {free / 2**30:.1f} GB")


def _keep_zip_time(target: Path, info: zipfile.ZipInfo) -> None:
    """Original-Zeitstempel des ZIP-Eintrags übernehmen (TAR erledigt das selbst)."""
    try:
        ts = datetime(*info.date_time).timestamp()
        os.utime(target, (ts, ts))
    except (ValueError, OSError, OverflowError):
        pass


def _skipped(name: str, skip: tuple[str, ...]) -> bool:
    return name.lower().endswith(skip)


def extract_archive(archive: Path, dest: Path, progress: Progress = lambda *_a: None,
                    skip: tuple[str, ...] = DEFAULT_SKIP) -> tuple[int, list[str]]:
    """Archiv nach ``dest`` entpacken. Pfade und Platzbedarf werden *vor* dem
    ersten geschriebenen Byte geprüft. Einträge mit Endung aus ``skip``
    (z.B. .ear) werden übersprungen. Liefert (Anzahl entpackt, übersprungene Namen)."""
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.lower().endswith(".zip"):
        try:
            zf = zipfile.ZipFile(archive)  # liest Zip64 (> 4 GB / > 65535 Einträge) transparent
        except zipfile.BadZipFile as exc:
            raise ArchiveError(f"Kein gültiges ZIP: {exc}") from None
        with zf:
            files = [i for i in zf.infolist() if not i.is_dir()]
            skipped = [i.filename for i in files if _skipped(i.filename, skip)]
            members = [i for i in files if not _skipped(i.filename, skip)]
            _check_members(dest, [i.filename for i in members])
            _check_space(dest, sum(i.file_size for i in members))
            total = sum(i.file_size for i in members) or 1
            done = 0
            for n, info in enumerate(members, 1):
                label = f"Entpacke {archive.name}: {n}/{len(members)} {info.filename}"
                progress(label, done * 100 / total)
                target = dest / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    since = 0
                    while chunk := src.read(CHUNK):
                        dst.write(chunk)
                        done += len(chunk)
                        since += len(chunk)
                        if since >= 64 * CHUNK:  # alle ~256 MB melden (große Einzeldateien)
                            progress(label, done * 100 / total)
                            since = 0
                _keep_zip_time(target, info)
            progress(f"Entpacke {archive.name}: fertig", 100)
            return len(members), skipped
    try:
        with tarfile.open(archive) as tf:
            files = [m for m in tf.getmembers() if m.isfile()]
            skipped = [m.name for m in files if _skipped(m.name, skip)]
            members = [m for m in files if not _skipped(m.name, skip)]
            _check_members(dest, [m.name for m in members])
            _check_space(dest, sum(m.size for m in members))
            total = sum(m.size for m in members) or 1
            done = 0
            for n, m in enumerate(members, 1):
                progress(f"Entpacke {archive.name}: {n}/{len(members)} {m.name}", done * 100 / total)
                tf.extract(m, dest, filter="data")
                done += m.size
            return len(members), skipped
    except tarfile.TarError as exc:
        raise ArchiveError(f"Kein gültiges TAR: {exc}") from None


def extract_pending(inbox: Path, progress: Progress = lambda *_a: None,
                    skip: tuple[str, ...] = DEFAULT_SKIP) -> dict:
    """Alle Archive in der Inbox entpacken (Zielordner = Archivname ohne Endung)
    und danach löschen. Defekte Archive werden in ``*.defekt`` umbenannt."""
    stats: dict = {"archive": 0, "entpackt": 0, "uebersprungen": [], "fehler": []}
    for archive in sorted(p for p in inbox.rglob("*") if p.is_file() and is_archive(p)):
        if any(part.startswith(".") for part in archive.relative_to(inbox).parts):
            continue  # laufende Uploads (.*.part) ignorieren
        dest = archive.with_name(_RX_STEM.sub("", archive.name))
        if dest.exists() and not dest.is_dir():
            dest = dest.with_name(dest.name + "_entpackt")
        try:
            count, skipped = extract_archive(archive, dest, progress, skip)
            stats["entpackt"] += count
            stats["uebersprungen"] += [f"{archive.name}:{s}" for s in skipped]
            archive.unlink()
            stats["archive"] += 1
        except (ArchiveError, OSError, EOFError, zipfile.BadZipFile) as exc:
            shutil.rmtree(dest, ignore_errors=True)
            archive.rename(archive.with_name(archive.name + BROKEN_SUFFIX))
            stats["fehler"].append(f"{archive.name}: {exc}")
    return stats
