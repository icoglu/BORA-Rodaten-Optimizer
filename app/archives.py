"""Sicheres Entpacken hochgeladener Archive (ZIP inkl. Zip64 > 4 GB, TAR/TAR.GZ).

Das Entpacken läuft im Hintergrund-Job, nicht im HTTP-Request, damit auch
mehrere Gigabyte große Pakete ohne Timeouts verarbeitet werden.
"""
from __future__ import annotations

import re
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import Callable

ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz")
BROKEN_SUFFIX = ".defekt"
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


def extract_archive(archive: Path, dest: Path, progress: Callable[[str], None] = lambda _m: None) -> int:
    """Archiv nach ``dest`` entpacken. Pfade und Platzbedarf werden *vor* dem
    ersten geschriebenen Byte geprüft. Liefert die Anzahl entpackter Dateien."""
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.lower().endswith(".zip"):
        try:
            zf = zipfile.ZipFile(archive)  # liest Zip64 (> 4 GB / > 65535 Einträge) transparent
        except zipfile.BadZipFile as exc:
            raise ArchiveError(f"Kein gültiges ZIP: {exc}") from None
        with zf:
            members = [i for i in zf.infolist() if not i.is_dir()]
            _check_members(dest, [i.filename for i in members])
            _check_space(dest, sum(i.file_size for i in members))
            for n, info in enumerate(members, 1):
                progress(f"Entpacke {archive.name}: {n}/{len(members)} {info.filename}")
                target = dest / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK)
            return len(members)
    try:
        with tarfile.open(archive) as tf:
            members = [m for m in tf.getmembers() if m.isfile()]
            _check_members(dest, [m.name for m in members])
            _check_space(dest, sum(m.size for m in members))
            progress(f"Entpacke {archive.name}: {len(members)} Dateien")
            tf.extractall(dest, members=members, filter="data")
            return len(members)
    except tarfile.TarError as exc:
        raise ArchiveError(f"Kein gültiges TAR: {exc}") from None


def extract_pending(inbox: Path, progress: Callable[[str], None] = lambda _m: None) -> dict:
    """Alle Archive in der Inbox entpacken (Zielordner = Archivname ohne Endung)
    und danach löschen. Defekte Archive werden in ``*.defekt`` umbenannt."""
    stats: dict = {"archive": 0, "entpackt": 0, "fehler": []}
    for archive in sorted(p for p in inbox.rglob("*") if p.is_file() and is_archive(p)):
        if any(part.startswith(".") for part in archive.relative_to(inbox).parts):
            continue  # laufende Uploads (.*.part) ignorieren
        dest = archive.with_name(_RX_STEM.sub("", archive.name))
        if dest.exists() and not dest.is_dir():
            dest = dest.with_name(dest.name + "_entpackt")
        try:
            stats["entpackt"] += extract_archive(archive, dest, progress)
            archive.unlink()
            stats["archive"] += 1
        except (ArchiveError, OSError, EOFError, zipfile.BadZipFile) as exc:
            shutil.rmtree(dest, ignore_errors=True)
            archive.rename(archive.with_name(archive.name + BROKEN_SUFFIX))
            stats["fehler"].append(f"{archive.name}: {exc}")
    return stats
