"""Zeitfenster-Bildung und ZIP-Erzeugung.

Regel: Alle Reports werden passend zum Zeitrahmen zusammengeführt und in
einzelne, mit Datum gekennzeichnete ZIP-Pakete geschrieben.

* Modus ``day``: ein ZIP je Kalendertag (``BORA_2026-09-27.zip``). Enthält alle
  AWR-Reports, deren Snapshot-Intervall den Tag berührt, sowie genau die
  Access-/Server-Log-Zeilen dieses Tages.
* Modus ``awr``: ein ZIP je AWR-Snapshot-Intervall
  (``BORA_2026-09-27_1000-1100.zip``). Enthält den/die AWR-Reports und die
  Log-Zeilen innerhalb des Intervalls (optional ± Puffer in Minuten).

Log-Zeilen werden byte-genau übernommen (Rohdaten). Zeilen ohne eigenen
Zeitstempel (z.B. Stacktraces) gehören zum vorangehenden Eintrag.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Optional

from . import detect
from .catalog import FileRecord
from .timestamps import LineTimestampParser

MODES = ("day", "awr")
MAX_PENDING_LINES = 50_000


@dataclass
class Window:
    name: str
    start: datetime
    end: datetime  # exklusiv
    awr: list[FileRecord] = field(default_factory=list)
    # Einheitlicher Zeitstempel für das ZIP und alle Einträge (Tag 00:00 bzw. AWR-Beginn ohne Puffer)
    stamp: Optional[datetime] = None

    @property
    def timestamp(self) -> datetime:
        return self.stamp or self.start

    def contains(self, ts: datetime) -> bool:
        return self.start <= ts < self.end

    def overlaps(self, first: Optional[datetime], last: Optional[datetime]) -> bool:
        return first is not None and last is not None and first < self.end and last >= self.start


def _fmt_range(begin: datetime, end: datetime) -> str:
    if begin.date() == end.date():
        return f"{begin:%Y-%m-%d_%H%M}-{end:%H%M}"
    return f"{begin:%Y-%m-%d_%H%M}-{end:%Y-%m-%d_%H%M}"


def _in_range(d: date, date_from: Optional[date], date_to: Optional[date]) -> bool:
    return (date_from is None or d >= date_from) and (date_to is None or d <= date_to)


def awr_periods(rec: FileRecord) -> list[tuple[datetime, datetime]]:
    """Analysezeiträume eines Oracle-Reports (AWR-Compare: zwei getrennte)."""
    periods = [(datetime.fromisoformat(b), datetime.fromisoformat(e)) for b, e in rec.info.get("periods") or []]
    if not periods and rec.first and rec.last:
        periods = [(rec.first, rec.last)]
    return periods


def is_awr_report(rec: FileRecord) -> bool:
    """Echter AWR-Report (auch RAC/Global/Compare) - im Gegensatz zu ASH/ADDM/Statspack."""
    return (rec.info.get("report_type") or "AWR").upper().startswith("AWR")


def _preference(r: FileRecord) -> tuple:
    """Welche von mehreren gleichwertigen Dateien behalten wird: HTML vor Text,
    Upload (inbox) vor eingebundener Quelle, dann die zuerst erfasste (Original)."""
    html = r.rel.lower().endswith((".html", ".htm"))
    return (not html, r.root != "inbox", r.id)


def deduplicate(records: list[FileRecord]) -> tuple[list[FileRecord], dict[int, list[FileRecord]]]:
    """Redundanzen entfernen:
    1. identischer Inhalt (SHA-256) -> nur eine Datei,
    2. derselbe AWR-/Oracle-Report in mehreren Formaten (gleiche DB, Instanz,
       Report-Typ und Snap-IDs, z.B. .html und .txt) -> HTML behalten.
    Liefert (behaltene Datensätze, {id_behalten: [ausgelassene Duplikate]})."""
    dups: dict[int, list[FileRecord]] = {}
    keep: dict[tuple, FileRecord] = {}

    def key_of(r: FileRecord) -> Optional[tuple]:
        i = r.info
        if r.category == detect.AWR and i.get("begin_snap") is not None and i.get("end_snap") is not None:
            return ("awr", i.get("report_type"), i.get("db_name"), i.get("instance"), i.get("begin_snap"),
                    i.get("end_snap"), tuple(map(tuple, i.get("periods") or [])))
        if i.get("sha256"):
            return ("sha", r.category, i["sha256"])
        return None

    out: list[FileRecord] = []
    for r in sorted(records, key=_preference):
        k = key_of(r) if r.category in detect.PACKABLE else None
        if k is None:
            out.append(r)
            continue
        if k in keep:
            dups.setdefault(keep[k].id, []).append(r)
            continue
        # gleicher Inhalt wie ein bereits behaltener Report (anderer Schlüsseltyp)?
        sha_key = ("sha", r.category, r.info.get("sha256"))
        if k[0] == "awr" and r.info.get("sha256") and sha_key in keep:
            dups.setdefault(keep[sha_key].id, []).append(r)
            continue
        keep[k] = r
        if k[0] == "awr" and r.info.get("sha256"):
            keep[sha_key] = r
        out.append(r)
    return sorted(out, key=lambda r: r.id), dups


def has_log_candidates(w: Window, records: list[FileRecord]) -> bool:
    """Schnelle Vorab-Prüfung (Katalog): überschneidet sich eine Log-Datei mit dem Fenster?
    Die genaue Prüfung auf Zeilenebene erfolgt in build()."""
    return any(r.category in detect.LOG_CATEGORIES and w.overlaps(r.first, r.last) for r in records)


def plan_windows(records: list[FileRecord], mode: str, prefix: str = "BORA", margin_min: int = 0,
                 date_from: Optional[date] = None, date_to: Optional[date] = None,
                 require_awr: bool = True) -> list[Window]:
    """Zeitfenster planen. ``require_awr``: ohne AWR-Report kein Paket - Tages-Pakete
    nur für Tage mit AWR-Report, andere Oracle-Reports nur zusammen mit einem AWR."""
    if mode not in MODES:
        raise ValueError(f"Unbekannter Modus: {mode}")
    records, _ = deduplicate(records)
    usable = [r for r in records if r.category in detect.PACKABLE and r.first and r.last]
    awrs = [r for r in usable if r.category == detect.AWR]
    windows: dict[str, Window] = {}

    if mode == "day":
        days: set[str] = set()
        for r in usable:
            days.update(r.days or detect.days_between(r.first, r.last))  # type: ignore[arg-type]
        if require_awr:
            days &= {d for a in awrs if is_awr_report(a) for b, e in awr_periods(a) for d in detect.days_between(b, e)}
        for d in sorted(days):
            day = date.fromisoformat(d)
            if not _in_range(day, date_from, date_to):
                continue
            start = datetime.combine(day, time.min)
            windows[d] = Window(f"{prefix}_{d}", start, start + timedelta(days=1))
        for w in windows.values():
            w.awr = [a for a in awrs if any(w.overlaps(b, e) for b, e in awr_periods(a))]
    else:
        # Regel 1: Jeder AWR-Report (HTML/Text) bestimmt einen Aufzeichnungszeitraum.
        # Dazu werden die Log-Zeilen dieses Zeitraums und alle anderen Oracle-Reports
        # (ASH, ADDM, Statspack), die sich damit überschneiden, zusammengeführt.
        margin = timedelta(minutes=max(margin_min, 0))
        primary = [r for r in awrs if is_awr_report(r)]
        secondary = [r for r in awrs if not is_awr_report(r)]

        def add_window(rep: FileRecord, begin: datetime, end: datetime) -> None:
            name = f"{prefix}_{_fmt_range(begin, end)}"
            start, stop = begin - margin, end + margin + timedelta(seconds=1)
            w = windows.get(name)
            if w is None:
                windows[name] = Window(name, start, stop, [rep], stamp=begin.replace(second=0))
            else:  # z.B. RAC: mehrere Instanzen im selben Snapshot-Intervall
                w.start, w.end = min(w.start, start), max(w.end, stop)
                w.stamp = min(w.timestamp, begin.replace(second=0))
                if rep not in w.awr:
                    w.awr.append(rep)

        def wanted(begin: datetime, end: datetime) -> bool:
            return _in_range(begin.date(), date_from, date_to) or _in_range(end.date(), date_from, date_to)

        for rep in sorted(primary, key=lambda r: r.first):  # type: ignore[arg-type,return-value]
            for begin, end in awr_periods(rep):
                if wanted(begin, end):
                    add_window(rep, begin, end)
        awr_windows = list(windows.values())
        for rep in sorted(secondary, key=lambda r: r.first):  # type: ignore[arg-type,return-value]
            for begin, end in awr_periods(rep):
                hits = [w for w in awr_windows if w.overlaps(begin, end)]
                for w in hits:
                    if rep not in w.awr:
                        w.awr.append(rep)
                if not hits and not require_awr and wanted(begin, end):  # nur ohne AWR-Pflicht: eigenes Paket
                    add_window(rep, begin, end)
    return sorted(windows.values(), key=lambda w: (w.start, w.name))


def entry_name(rec: FileRecord, category: Optional[str] = None, strip_gz: bool = True) -> str:
    rel = PurePosixPath(*Path(rec.rel).parts)
    name = detect.logical_name(rel.name) if strip_gz else rel.name
    parts = [category or rec.category]
    if rec.root != "inbox":
        parts.append(rec.root)
    parts.extend(rel.parent.parts)
    parts.append(name)
    return "/".join(parts)


@dataclass
class _Slice:
    path: Path
    handle: Optional[BinaryIO]
    lines: int = 0
    first: Optional[datetime] = None
    last: Optional[datetime] = None


def _slice_log(rec: FileRecord, windows: list[Window], by_day: Optional[dict[str, int]],
               tmp: Path) -> dict[int, _Slice]:
    """Eine Log-Datei einmal sequentiell lesen und auf die Zeitfenster verteilen."""
    slices: dict[int, _Slice] = {}
    candidates = [i for i, w in enumerate(windows) if w.overlaps(rec.first, rec.last)]
    if not candidates:
        return slices
    parser = LineTimestampParser()
    pending: list[bytes] = []
    current: Optional[datetime] = None
    targets: list[int] = []

    def targets_for(ts: datetime) -> list[int]:
        if by_day is not None:
            idx = by_day.get(ts.date().isoformat())
            return [idx] if idx is not None else []
        return [i for i in candidates if windows[i].contains(ts)]

    def write(idx: int, line: bytes, ts: datetime) -> None:
        s = slices.get(idx)
        if s is None:
            p = tmp / str(idx) / f"{rec.id}.part"
            p.parent.mkdir(parents=True, exist_ok=True)
            s = slices[idx] = _Slice(p, open(p, "wb"))
        assert s.handle is not None
        s.handle.write(line)
        s.lines += 1
        if s.first is None:
            s.first = ts
        s.last = ts

    try:
        with detect.open_binary(rec.path) as fh:
            for line in fh:
                ts = parser.parse(line)
                if ts is not None and ts != current:
                    current, targets = ts, targets_for(ts)
                if current is None:
                    if len(pending) < MAX_PENDING_LINES:
                        pending.append(line)
                    continue
                if pending:
                    for p in pending:
                        for i in targets:
                            write(i, p, current)
                    pending.clear()
                for i in targets:
                    write(i, line, current)
    finally:
        for s in slices.values():
            if s.handle:
                s.handle.close()
                s.handle = None
    return slices


def build(records: list[FileRecord], windows: list[Window], out_dir: Path, work_dir: Path,
          mode: str, progress: Callable[..., None] = lambda *_a: None,
          require_logs: bool = True) -> list[dict]:
    """ZIPs erzeugen. ``require_logs``: Zeitfenster ohne passende Log-Zeilen
    (z.B. AWR-Report ohne Logs) erhalten kein Paket."""
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    records, dups = deduplicate(records)
    logs = [r for r in records if r.category in detect.LOG_CATEGORIES and r.first and r.last]
    by_day = {w.start.date().isoformat(): i for i, w in enumerate(windows)} if mode == "day" else None
    results: list[dict] = []

    with tempfile.TemporaryDirectory(dir=work_dir, prefix="build-") as tmpname:
        tmp = Path(tmpname)
        # 1) Logs zerschneiden - jede Datei wird genau einmal gelesen
        per_window: dict[int, list[tuple[FileRecord, _Slice]]] = {}
        for n, rec in enumerate(logs, 1):
            progress(f"Zerlege Log {n}/{len(logs)}: {rec.root}/{rec.rel}", (n - 1) * 80 / max(len(logs), 1))
            for idx, sl in _slice_log(rec, windows, by_day, tmp).items():
                per_window.setdefault(idx, []).append((rec, sl))

        # 2) Ein ZIP je Zeitfenster
        for idx, w in enumerate(windows):
            slices = per_window.get(idx, [])
            if not slices and (require_logs or not w.awr):
                continue  # keine Log-Zeilen im Zeitraum -> kein Paket
            progress(f"Erzeuge {w.name}.zip ({idx + 1}/{len(windows)})", 80 + idx * 20 / max(len(windows), 1))
            results.append(_write_zip(w, slices, out_dir, mode, dups))
            shutil.rmtree(tmp / str(idx), ignore_errors=True)
    return results


def _zip_time(ts: datetime) -> tuple[int, int, int, int, int, int]:
    ts = max(ts, datetime(1980, 1, 1))  # ZIP/DOS-Zeit beginnt 1980
    return (ts.year, ts.month, ts.day, ts.hour, ts.minute, ts.second - ts.second % 2)


def _add(zf: zipfile.ZipFile, src: Path, arcname: str, stamp: datetime) -> None:
    """Datei mit vorgegebenem Zeitstempel ins ZIP streamen (statt Datei-mtime)."""
    info = zipfile.ZipInfo(arcname, date_time=_zip_time(stamp))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    with open(src, "rb") as fh, zf.open(info, "w", force_zip64=True) as out:
        shutil.copyfileobj(fh, out, 4 * 1024 * 1024)


def _write_zip(w: Window, slices: list[tuple[FileRecord, _Slice]], out_dir: Path, mode: str,
               dups: Optional[dict[int, list[FileRecord]]] = None) -> dict:
    dups = dups or {}

    def dup_info(rec: FileRecord) -> dict:
        d = dups.get(rec.id)
        return {"duplikate_ausgelassen": [f"{x.root}/{x.rel}" for x in d]} if d else {}

    target = out_dir / f"{w.name}.zip"
    part = target.with_suffix(".zip.part")
    manifest: dict = {
        "paket": w.name,
        "modus": mode,
        "zeitraum": {"von": w.start.isoformat(), "bis_exklusiv": w.end.isoformat()},
        "zeitstempel": w.timestamp.isoformat(),
        "erstellt": datetime.now().isoformat(timespec="seconds"),
        "dateien": [],
    }
    used: set[str] = set()

    def unique(name: str) -> str:
        base, n = name, 1
        while name in used:
            n += 1
            name = f"{base}.{n}"
        used.add(name)
        return name

    with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
        for a in sorted(w.awr, key=lambda r: (r.first, r.rel)):  # type: ignore[arg-type,return-value]
            arc = unique(entry_name(a, strip_gz=False))
            _add(zf, a.path, arc, w.timestamp)
            manifest["dateien"].append({
                "eintrag": arc, "kategorie": detect.AWR, "quelle": f"{a.root}/{a.rel}",
                "von": a.first.isoformat() if a.first else None,
                "bis": a.last.isoformat() if a.last else None,
                **{k: v for k, v in a.info.items() if v and k != "sha256"}, **dup_info(a),
            })
        for rec, sl in sorted(slices, key=lambda t: (t[0].category, t[0].root, t[0].rel)):
            arc = unique(entry_name(rec))
            _add(zf, sl.path, arc, w.timestamp)
            manifest["dateien"].append({
                "eintrag": arc, "kategorie": rec.category, "quelle": f"{rec.root}/{rec.rel}",
                "zeilen": sl.lines, "von": sl.first.isoformat() if sl.first else None,
                "bis": sl.last.isoformat() if sl.last else None, **dup_info(rec),
            })
        minfo = zipfile.ZipInfo("manifest.json", date_time=_zip_time(w.timestamp))
        minfo.compress_type = zipfile.ZIP_DEFLATED
        minfo.external_attr = 0o644 << 16
        zf.writestr(minfo, json.dumps(manifest, indent=2, ensure_ascii=False))
    stamp = w.timestamp.timestamp()
    os.utime(part, (stamp, stamp))  # auch das ZIP selbst trägt den Paket-Zeitstempel
    os.replace(part, target)
    return {"zip": target.name, "dateien": len(manifest["dateien"]), "groesse": target.stat().st_size}
