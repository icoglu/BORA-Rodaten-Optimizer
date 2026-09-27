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


def plan_windows(records: list[FileRecord], mode: str, prefix: str = "BORA", margin_min: int = 0,
                 date_from: Optional[date] = None, date_to: Optional[date] = None) -> list[Window]:
    if mode not in MODES:
        raise ValueError(f"Unbekannter Modus: {mode}")
    usable = [r for r in records if r.category in detect.PACKABLE and r.first and r.last]
    awrs = [r for r in usable if r.category == detect.AWR]
    windows: dict[str, Window] = {}

    if mode == "day":
        days: set[str] = set()
        for r in usable:
            days.update(r.days or detect.days_between(r.first, r.last))  # type: ignore[arg-type]
        for d in sorted(days):
            day = date.fromisoformat(d)
            if not _in_range(day, date_from, date_to):
                continue
            start = datetime.combine(day, time.min)
            windows[d] = Window(f"{prefix}_{d}", start, start + timedelta(days=1))
        for w in windows.values():
            w.awr = [a for a in awrs if any(w.overlaps(b, e) for b, e in awr_periods(a))]
    else:
        margin = timedelta(minutes=max(margin_min, 0))
        for a in sorted(awrs, key=lambda r: r.first):  # type: ignore[arg-type,return-value]
            for begin, end in awr_periods(a):
                if not (_in_range(begin.date(), date_from, date_to) or _in_range(end.date(), date_from, date_to)):
                    continue
                name = f"{prefix}_{_fmt_range(begin, end)}"
                start, stop = begin - margin, end + margin + timedelta(seconds=1)
                w = windows.get(name)
                if w is None:
                    windows[name] = Window(name, start, stop, [a])
                else:  # z.B. RAC: mehrere Instanzen im selben Snapshot-Intervall
                    w.start, w.end = min(w.start, start), max(w.end, stop)
                    if a not in w.awr:
                        w.awr.append(a)
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
          mode: str, progress: Callable[..., None] = lambda *_a: None) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
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
            if not slices and not w.awr:
                continue
            progress(f"Erzeuge {w.name}.zip ({idx + 1}/{len(windows)})", 80 + idx * 20 / max(len(windows), 1))
            results.append(_write_zip(w, slices, out_dir, mode))
            shutil.rmtree(tmp / str(idx), ignore_errors=True)
    return results


def _write_zip(w: Window, slices: list[tuple[FileRecord, _Slice]], out_dir: Path, mode: str) -> dict:
    target = out_dir / f"{w.name}.zip"
    part = target.with_suffix(".zip.part")
    manifest: dict = {
        "paket": w.name,
        "modus": mode,
        "zeitraum": {"von": w.start.isoformat(), "bis_exklusiv": w.end.isoformat()},
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
            zf.write(a.path, arc)
            manifest["dateien"].append({
                "eintrag": arc, "kategorie": detect.AWR, "quelle": f"{a.root}/{a.rel}",
                "von": a.first.isoformat() if a.first else None,
                "bis": a.last.isoformat() if a.last else None, **{k: v for k, v in a.info.items() if v},
            })
        for rec, sl in sorted(slices, key=lambda t: (t[0].category, t[0].root, t[0].rel)):
            arc = unique(entry_name(rec))
            zf.write(sl.path, arc)
            manifest["dateien"].append({
                "eintrag": arc, "kategorie": rec.category, "quelle": f"{rec.root}/{rec.rel}",
                "zeilen": sl.lines, "von": sl.first.isoformat() if sl.first else None,
                "bis": sl.last.isoformat() if sl.last else None,
            })
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    os.replace(part, target)
    return {"zip": target.name, "dateien": len(manifest["dateien"]), "groesse": target.stat().st_size}
