"""Datei-Katalog (SQLite): welche Rohdaten liegen vor, welche Kategorie,
welcher Zeitraum. Ergebnisse werden anhand (Größe, mtime) gecacht."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator, Optional

from . import detect

# Erhöhen, wenn sich die Analyse ändert - vorhandene Einträge werden dann neu
# analysiert (manuelle Kategorien bleiben erhalten).
ANALYSIS_VERSION = "9"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,
    rel         TEXT NOT NULL,
    root        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    detected    TEXT NOT NULL,
    override    TEXT,
    first_ts    TEXT,
    last_ts     TEXT,
    days        TEXT NOT NULL DEFAULT '[]',
    info        TEXT NOT NULL DEFAULT '{}',
    error       TEXT,
    scanned_at  TEXT NOT NULL
);
"""


@dataclass
class FileRecord:
    id: int
    path: Path
    rel: str
    root: str
    size: int
    detected: str
    override: Optional[str]
    first: Optional[datetime]
    last: Optional[datetime]
    days: list[str]
    info: dict
    error: Optional[str]

    @property
    def category(self) -> str:
        return self.override or self.detected

    @property
    def deletable(self) -> bool:
        return self.root == "inbox"


def _dt(v: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(v) if v else None


def _consistency(intervals, named, mtime_dt) -> str:
    """Passt der Inhalt zu Dateiname bzw. Dateizeit? (Der Inhalt hat Vorrang.)"""
    content_days = {d for b, e in intervals for d in detect.days_between(b, e)}
    notes = []
    if named and named[0].date().isoformat() not in content_days:
        notes.append(f"Dateiname nennt {named[0].date().isoformat()}")
    if mtime_dt and mtime_dt.date().isoformat() not in content_days and not named:
        notes.append(f"Dateizeit {mtime_dt.date().isoformat()}")
    return "Inhalt passt" if not notes else "Inhalt maßgeblich – abweichend: " + ", ".join(notes)


class Catalog:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        # Tage, deren Daten sich beim letzten scan()/reanalyse() geändert haben
        self.last_changed_days: set[str] = set()
        with self._conn() as c:
            c.executescript(SCHEMA)
            row = c.execute("SELECT value FROM meta WHERE key='analysis_version'").fetchone()
            if not row or row[0] != ANALYSIS_VERSION:
                c.execute("UPDATE files SET mtime=-1")  # erzwingt Neuanalyse beim nächsten Scan
                c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('analysis_version',?)", (ANALYSIS_VERSION,))

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ Lesen
    def _record(self, r: sqlite3.Row) -> FileRecord:
        return FileRecord(
            id=r["id"], path=Path(r["path"]), rel=r["rel"], root=r["root"], size=r["size"],
            detected=r["detected"], override=r["override"], first=_dt(r["first_ts"]),
            last=_dt(r["last_ts"]), days=json.loads(r["days"]), info=json.loads(r["info"]),
            error=r["error"],
        )

    def all(self) -> list[FileRecord]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM files ORDER BY root, rel").fetchall()
        return [self._record(r) for r in rows]

    def get(self, file_id: int) -> Optional[FileRecord]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return self._record(r) if r else None

    # --------------------------------------------------------------- Schreiben
    def set_override(self, file_id: int, category: Optional[str]) -> None:
        if category is not None and category not in detect.CATEGORIES:
            raise ValueError(f"Unbekannte Kategorie: {category}")
        with self._conn() as c:
            c.execute("UPDATE files SET override=? WHERE id=?", (category, file_id))

    def remove(self, file_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM files WHERE id=?", (file_id,))

    def clear(self) -> None:
        """Katalog leeren (inkl. manueller Kategorien)."""
        with self._conn() as c:
            c.execute("DELETE FROM files")
        self.last_changed_days = set()

    # ------------------------------------------------------------------ Scan
    def scan(self, roots: dict[str, Path], progress: Callable[..., None] = lambda *_a: None) -> dict:
        """Verzeichnisse rekursiv einlesen; nur neue/geänderte Dateien analysieren."""
        seen: set[str] = set()
        stats = {"neu": 0, "unverändert": 0, "entfernt": 0, "fehler": 0}
        with self._conn() as c:
            rows = c.execute("SELECT path,size,mtime,override,days FROM files").fetchall()
        known = {r["path"]: (r["size"], r["mtime"]) for r in rows}
        old_days = {r["path"]: json.loads(r["days"]) for r in rows}
        changed: set[str] = set()
        overrides = {r["path"]: r["override"] for r in rows}
        candidates = []
        for label, root in roots.items():
            if root.is_dir():
                candidates += [(label, root, p) for p in sorted(root.rglob("*"))
                               if p.is_file() and not any(x.startswith(".") for x in p.relative_to(root).parts)]
        for n, (label, root, path) in enumerate(candidates):
            key = str(path)
            seen.add(key)
            st = path.stat()
            if known.get(key) == (st.st_size, st.st_mtime):
                stats["unverändert"] += 1
                continue
            name = f"Analysiere {n + 1}/{len(candidates)}: {label}/{path.relative_to(root)}"
            progress(name, n * 100 / len(candidates))
            file_progress = lambda frac, _n=n, _name=name: progress(_name, (_n + frac) * 100 / len(candidates))
            self._analyse_and_store(label, root, path, st.st_size, st.st_mtime, overrides.get(key),
                                    file_progress)
            changed.update(old_days.get(key, []))
            changed.update(self._days_of(key))
            stats["neu"] += 1
        with self._conn() as c:
            for key in set(known) - seen:
                c.execute("DELETE FROM files WHERE path=?", (key,))
                changed.update(old_days.get(key, []))
                stats["entfernt"] += 1
            stats["fehler"] = c.execute("SELECT COUNT(*) FROM files WHERE error IS NOT NULL").fetchone()[0]
        self.last_changed_days = changed
        return stats

    def _days_of(self, path: str) -> list[str]:
        with self._conn() as c:
            r = c.execute("SELECT days FROM files WHERE path=?", (path,)).fetchone()
        return json.loads(r["days"]) if r else []

    def all_days(self) -> set[str]:
        """Alle Tage, zu denen paketierbare Daten vorliegen."""
        return {d for r in self.all() if r.category in detect.PACKABLE for d in r.days}

    def reanalyse(self, file_id: int) -> None:
        """Zeitraum neu bestimmen (z.B. nach manueller Kategorie-Änderung)."""
        rec = self.get(file_id)
        self.last_changed_days = set(rec.days) if rec else set()
        if rec is None or not rec.path.is_file():
            return
        st = rec.path.stat()
        root = Path(str(rec.path)[: -len(rec.rel)])
        self._analyse_and_store(rec.root, root, rec.path, st.st_size, st.st_mtime, rec.override)
        self.last_changed_days.update(self._days_of(str(rec.path)))

    def _analyse_and_store(self, label: str, root: Path, path: Path, size: int, mtime: float,
                           override: Optional[str] = None,
                           file_progress: Optional[Callable[[float], None]] = None) -> None:
        detected, first, last, days, info, error = detect.UNKNOWN, None, None, [], {}, None
        try:
            detected = detect.classify(path)
            category = override or detected
            if category == detect.AWR:
                a = detect.parse_awr(path)
                first, last = a.begin, a.end
                info = {"report_type": a.report_type, "begin_snap": a.begin_snap, "end_snap": a.end_snap,
                        "db_name": a.db_name, "instance": a.instance, "time_source": a.time_source,
                        "sha256": a.sha256,
                        "periods": [[b.isoformat(), e.isoformat()] for b, e in a.periods]}
                if first and last:
                    days = sorted({d for b, e in a.periods for d in detect.days_between(b, e)})
                else:
                    error = "Oracle-Report: Beginn/Ende nicht gefunden"
            elif category == detect.UNKNOWN:
                # Sonstige Dateien: Inhalt prüfen (ganze Datei, überall in der Zeile);
                # nur wenn er keine Zeitangaben enthält: Dateiname, dann Dateizeit
                ci = detect.scan_content(path, file_progress)
                named = detect.datetime_from_name(path.name)
                mtime_dt = detect.date_from_mtime(path)
                if ci and ci.intervals:
                    first, last = ci.intervals[0][0], ci.intervals[-1][1]
                    days = sorted({d for b, e in ci.intervals for d in detect.days_between(b, e)})
                    info = {"sha256": ci.sha256, "date_source": "inhalt", "zeitangaben": ci.stamps,
                            "gelesen_als": ci.method,
                            "intervals": [[b.isoformat(), e.isoformat()] for b, e in ci.intervals],
                            "pruefung": _consistency(ci.intervals, named, mtime_dt)}
                else:
                    # Zeitrahmen der Datei: Zeitpunkt (Name mit Uhrzeit, Dateizeit) oder ganzer Tag
                    info = {"sha256": ci.sha256, "gelesen_als": ci.method,
                            "pruefung": f"kein Datum im Inhalt ({ci.method})"}
                    if named and named[1]:
                        first = last = named[0]
                        info["date_source"] = "dateiname"
                    elif named:
                        first, last = named[0], named[0] + timedelta(days=1) - timedelta(seconds=1)
                        info["date_source"] = "dateiname (nur Datum)"
                    elif mtime_dt is not None:
                        first = last = mtime_dt
                        info["date_source"] = "dateizeit"
                    if first and last:
                        days = detect.days_between(first, last)
            elif category in detect.LOG_CATEGORIES:
                li = detect.scan_log(path, file_progress)
                first, last, days = li.first, li.last, sorted(li.days)
                info = {"lines": li.lines, "stamped_lines": li.stamped_lines, "sha256": li.sha256,
                        "ts_mode": "ganze Zeile" if li.whole_line else "Zeilenanfang"}
                if li.lines and not li.stamped_lines:
                    error = "Keine Zeitstempel erkannt"
        except Exception as exc:  # defekte Datei darf den Scan nicht abbrechen
            error = f"{type(exc).__name__}: {exc}"
        with self._conn() as c:
            c.execute(
                """INSERT INTO files(path,rel,root,size,mtime,detected,first_ts,last_ts,days,info,error,scanned_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET rel=excluded.rel, root=excluded.root, size=excluded.size,
                     mtime=excluded.mtime, detected=excluded.detected, first_ts=excluded.first_ts,
                     last_ts=excluded.last_ts, days=excluded.days, info=excluded.info,
                     error=excluded.error, scanned_at=excluded.scanned_at""",
                (str(path), str(path.relative_to(root)), label, size, mtime, detected,
                 first.isoformat() if first else None, last.isoformat() if last else None,
                 json.dumps(days), json.dumps(info), error, datetime.now().isoformat(timespec="seconds")),
            )
