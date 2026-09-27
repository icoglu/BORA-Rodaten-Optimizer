"""Datei-Katalog (SQLite): welche Rohdaten liegen vor, welche Kategorie,
welcher Zeitraum. Ergebnisse werden anhand (Größe, mtime) gecacht."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Optional

from . import detect

# Erhöhen, wenn sich die Analyse ändert - vorhandene Einträge werden dann neu
# analysiert (manuelle Kategorien bleiben erhalten).
ANALYSIS_VERSION = "2"

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


class Catalog:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
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

    # ------------------------------------------------------------------ Scan
    def scan(self, roots: dict[str, Path], progress: Callable[..., None] = lambda *_a: None) -> dict:
        """Verzeichnisse rekursiv einlesen; nur neue/geänderte Dateien analysieren."""
        seen: set[str] = set()
        stats = {"neu": 0, "unverändert": 0, "entfernt": 0, "fehler": 0}
        with self._conn() as c:
            rows = c.execute("SELECT path,size,mtime,override FROM files").fetchall()
        known = {r["path"]: (r["size"], r["mtime"]) for r in rows}
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
            stats["neu"] += 1
        with self._conn() as c:
            for key in set(known) - seen:
                c.execute("DELETE FROM files WHERE path=?", (key,))
                stats["entfernt"] += 1
            stats["fehler"] = c.execute("SELECT COUNT(*) FROM files WHERE error IS NOT NULL").fetchone()[0]
        return stats

    def reanalyse(self, file_id: int) -> None:
        """Zeitraum neu bestimmen (z.B. nach manueller Kategorie-Änderung)."""
        rec = self.get(file_id)
        if rec is None or not rec.path.is_file():
            return
        st = rec.path.stat()
        root = Path(str(rec.path)[: -len(rec.rel)])
        self._analyse_and_store(rec.root, root, rec.path, st.st_size, st.st_mtime, rec.override)

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
                        "periods": [[b.isoformat(), e.isoformat()] for b, e in a.periods]}
                if first and last:
                    days = sorted({d for b, e in a.periods for d in detect.days_between(b, e)})
                else:
                    error = "Oracle-Report: Beginn/Ende nicht gefunden"
            elif category in detect.LOG_CATEGORIES:
                li = detect.scan_log(path, file_progress)
                first, last, days = li.first, li.last, sorted(li.days)
                info = {"lines": li.lines, "stamped_lines": li.stamped_lines}
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
