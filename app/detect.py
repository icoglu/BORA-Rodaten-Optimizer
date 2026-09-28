"""Kategorisierung der Rohdaten und Metadaten-Extraktion."""
from __future__ import annotations

import gzip
import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Callable, Optional

from .timestamps import LineTimestampParser, parse_text

ACCESS = "access"
SERVER = "server"
AWR = "awr"
UNKNOWN = "unknown"
IGNORE = "ignore"
CATEGORIES = (ACCESS, SERVER, AWR, UNKNOWN, IGNORE)
PACKABLE = (ACCESS, SERVER, AWR)
LOG_CATEGORIES = (ACCESS, SERVER)

SNIFF_BYTES = 64 * 1024

_RX_ACCESS_NAME = re.compile(r"access.*\.log", re.I)
_RX_SERVER_NAME = re.compile(r"server.*\.log", re.I)
# Oracle-Datenbank-Reports (HTML oder Text) - alle in Kategorie "awr"
ORACLE_REPORT_TYPES: list[tuple[str, re.Pattern[bytes]]] = [
    ("AWR Compare", re.compile(rb"WORKLOAD REPOSITORY COMPARE PERIOD REPORT", re.I)),
    ("AWR RAC/Global", re.compile(rb"WORKLOAD REPOSITORY (?:RAC|GLOBAL) REPORT", re.I)),
    ("AWR", re.compile(rb"WORKLOAD REPOSITORY REPORT", re.I)),
    ("ASH", re.compile(rb"ASH Report\s+For\b", re.I)),
    ("ADDM", re.compile(rb"ADDM Report for Task|Automatic Database Diagnostic Monitor", re.I)),
    ("Statspack", re.compile(rb"STATSPACK report for", re.I)),
]
_RX_AWR_NAME = re.compile(r"awr|ashrpt|addmrpt|^sp_\d|statspack|spreport", re.I)
_RX_ACCESS_CONTENT = re.compile(rb'\[\d{1,2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2}[^\]]*\]\s+"(?:GET|POST|PUT|DELETE|HEAD|OPTIONS|PATCH)\s')
_RX_W3C_CONTENT = re.compile(rb"^#Fields:.*(?:cs-method|cs-uri)", re.M)
_RX_SERVER_CONTENT = re.compile(rb"^####<", re.M)


def is_gzip(path: Path) -> bool:
    with open(path, "rb") as fh:
        return fh.read(2) == b"\x1f\x8b"


def open_binary(path: Path) -> BinaryIO:
    """Öffnet Datei (transparent auch .gz) im Binärmodus - Rohdaten bleiben byte-genau."""
    if is_gzip(path):
        return gzip.open(path, "rb")  # type: ignore[return-value]
    return open(path, "rb")


def logical_name(name: str) -> str:
    """Dateiname ohne Kompressionsendung (server.log00012.gz -> server.log00012)."""
    return name[:-3] if name.lower().endswith(".gz") else name


def sniff(path: Path) -> bytes:
    try:
        with open_binary(path) as fh:
            return fh.read(SNIFF_BYTES)
    except (OSError, EOFError):
        return b""


def oracle_report_type(head: bytes) -> Optional[str]:
    """Typ eines Oracle-Reports (AWR, ASH, ADDM, ...) anhand des Inhalts, sonst None."""
    for name, rx in ORACLE_REPORT_TYPES:
        if rx.search(head):
            return name
    return None


JAVA_ARCHIVES = (".ear", ".war", ".jar", ".rar")


def classify(path: Path, head: Optional[bytes] = None) -> str:
    """Kategorie aus Inhalt (vorrangig) und Dateinamen ableiten."""
    name = logical_name(path.name)
    if name.lower().endswith(JAVA_ARCHIVES):
        return IGNORE  # Anwendungsarchive: nicht öffnen, nicht entpacken, nicht paketieren
    head = sniff(path) if head is None else head
    if oracle_report_type(head):
        return AWR
    if _RX_ACCESS_CONTENT.search(head) or _RX_W3C_CONTENT.search(head):
        return ACCESS
    if _RX_SERVER_CONTENT.search(head):
        return SERVER
    if _RX_AWR_NAME.search(name) and name.lower().endswith((".html", ".htm", ".txt", ".lst")):
        return AWR
    if _RX_ACCESS_NAME.search(name):
        return ACCESS
    if _RX_SERVER_NAME.search(name):
        return SERVER
    return UNKNOWN


# --------------------------------------------------------------------------- AWR

@dataclass
class AwrInfo:
    begin: Optional[datetime] = None
    end: Optional[datetime] = None
    begin_snap: Optional[int] = None
    end_snap: Optional[int] = None
    db_name: Optional[str] = None
    instance: Optional[str] = None
    report_type: Optional[str] = None
    time_source: Optional[str] = None
    sha256: str = ""
    # Einzelne Analysezeiträume; bei AWR-Compare zwei getrennte Perioden
    periods: list[tuple[datetime, datetime]] = field(default_factory=list)


_RX_TAGS = re.compile(r"<[^>]+>")
_RX_WS = re.compile(r"[ \t\r\f\v]+")
_RX_SNAP = re.compile(r"(Begin|End) Snap:\s+(\d+)\s+(\S+\s+\d{1,2}:\d{2}:\d{2})")
_RX_ASH = re.compile(r"Analysis (Begin|End) Time:\s+(\S+\s+\d{1,2}:\d{2}:\d{2})")
_ORA_TS = r"\d{1,2}-[A-Za-z]{3}-\d{2,4}\s+\d{1,2}[:.]\d{2}(?:[:.]\d{2})?(?:\s*[AP]M)?"
_RX_ADDM = re.compile(r"Time period starts at\s+(" + _ORA_TS + r").*?Time period ends at\s+(" + _ORA_TS + ")", re.S | re.I)
_RX_ORA_TS = re.compile(_ORA_TS, re.I)
# AWR-Compare: Zeilen "1st"/"2nd" mit Begin Snap Id/Time und End Snap Id/Time
_RX_COMPARE_ROW = re.compile(
    r"\b(1st|2nd)\)?\s+(?:[A-Za-z_][\w$#]*\s+)?(\d+)\s+(" + _ORA_TS + r")(?:\s*\(\w{2,3}\))?\s+(\d+)\s+(" + _ORA_TS + ")",
    re.I)
_RX_DBNAME = re.compile(r"DB Name\s+DB Id.*?\n\s*(?:[-\s]+\n\s*)?(\S+)\s+(\d+)\s+(\S+)", re.S)


def awr_text(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    if "<html" in text[:4096].lower() or "<table" in text.lower():
        text = _RX_TAGS.sub(" ", text)
        text = html.unescape(text)
    return _RX_WS.sub(" ", text)


def parse_oracle_ts(value: str) -> Optional[datetime]:
    """Oracle-Zeitformate: 27-Sep-26 10:00:05 | 27-SEP-26 10.00.05 AM | 27-Sep-26 10:00."""
    m = re.match(r"(\d{1,2}-[A-Za-z]{3}-\d{2,4})\s+(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?\s*([AP]M)?",
                 value.strip(), re.I)
    if not m:
        return None
    hour = int(m[2])
    if m[5]:
        hour = hour % 12 + (12 if m[5].upper() == "PM" else 0)
    return parse_text(f"{m[1]} {hour:02d}:{m[3]}:{m[4] or '00'}")


# optionale Uhrzeit direkt nach dem Datum: _1030, -103000, T10:30:00, " 10.30"
_NAME_TIME = r"(?:[T_\-. ]?([01]\d|2[0-3])[:.\-h]?([0-5]\d)(?:[:.\-]?([0-5]\d))?)?"
_RX_NAME_DATE = [
    # 2026-09-27, 20260927, 2026_09_27 (+ Uhrzeit)
    re.compile(r"(?<!\d)(20\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])" + _NAME_TIME + r"(?!\d)"),
    # 27.09.2026 (+ Uhrzeit)
    re.compile(r"(?<!\d)(0[1-9]|[12]\d|3[01])\.(0[1-9]|1[0-2])\.(20\d{2})" + _NAME_TIME + r"(?!\d)"),
]


def datetime_from_name(name: str) -> Optional[tuple[datetime, bool]]:
    """Datum (ggf. mit Uhrzeit) im Dateinamen. Liefert (Zeitpunkt, Uhrzeit_bekannt)."""
    m = _RX_NAME_DATE[0].search(name)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    else:
        m = _RX_NAME_DATE[1].search(name)
        if not m:
            return None
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
    hh, mi, ss = m[4], m[5], m[6]
    try:
        if hh is not None and mi is not None:
            return datetime(y, mo, d, int(hh), int(mi), int(ss or 0)), True
        return datetime(y, mo, d), False
    except ValueError:
        return None


def date_from_name(name: str) -> Optional[datetime]:
    """Zeitpunkt aus dem Dateinamen (ohne Uhrzeit: 00:00)."""
    r = datetime_from_name(name)
    return r[0] if r else None


# Dateizeiten vor 2000 gelten als unbekannt (Uploads ohne Original-Zeitstempel werden auf 0 gesetzt)
TRUSTED_MTIME_FROM = datetime(2000, 1, 1).timestamp()


def date_from_mtime(path: Path) -> Optional[datetime]:
    """Änderungszeitpunkt der Datei - nur wenn er als Original-Zeitstempel bekannt ist."""
    mtime = path.stat().st_mtime
    if mtime < TRUSTED_MTIME_FROM:
        return None
    return datetime.fromtimestamp(mtime).replace(microsecond=0)


def is_binary(path: Path) -> bool:
    try:
        with open_binary(path) as fh:
            return b"\0" in fh.read(8192)
    except (OSError, EOFError):
        return True


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open_binary(path) as fh:
        while chunk := fh.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def parse_awr(path: Path) -> AwrInfo:
    """Zeitfenster eines Oracle-Reports (AWR/ASH/ADDM/Statspack/Compare) bestimmen."""
    with open_binary(path) as fh:
        raw = fh.read(4 * 1024 * 1024)  # Kopfbereich reicht vollständig
    text = awr_text(raw)
    info = AwrInfo(report_type=oracle_report_type(raw[:SNIFF_BYTES]), sha256=file_sha256(path))
    # 1) Snapshot-Intervall (AWR, AWR-Global, Statspack; Compare: beide Perioden)
    for kind, snap, ts in _RX_SNAP.findall(text):
        parsed = parse_oracle_ts(ts)
        if parsed is None:
            continue
        if kind == "Begin" and (info.begin is None or parsed < info.begin):
            info.begin, info.begin_snap = parsed, int(snap)
        elif kind == "End" and (info.end is None or parsed > info.end):
            info.end, info.end_snap = parsed, int(snap)
    if info.begin and info.end:
        info.time_source = "snapshot"
    # 1b) AWR-Compare: zwei getrennte Perioden statt einer Gesamtspanne
    if info.report_type == "AWR Compare":
        periods = {}
        for label, _bs, b, _es, e in _RX_COMPARE_ROW.findall(text):
            pb, pe = parse_oracle_ts(b), parse_oracle_ts(e)
            if pb and pe and label.lower() not in periods:
                periods[label.lower()] = (pb, pe)
        if periods:
            info.periods = sorted(periods.values())
            info.begin = min(p[0] for p in info.periods)
            info.end = max(p[1] for p in info.periods)
            info.time_source = "snapshot"
    # 2) ASH: Analysis Begin/End Time
    if not (info.begin and info.end):
        found = {k: parse_oracle_ts(v) for k, v in _RX_ASH.findall(text)}
        if found.get("Begin") and found.get("End"):
            info.begin, info.end, info.time_source = found["Begin"], found["End"], "analysis"
    # 3) ADDM: Time period starts/ends at
    if not (info.begin and info.end):
        m = _RX_ADDM.search(text)
        if m:
            info.begin, info.end, info.time_source = parse_oracle_ts(m[1]), parse_oracle_ts(m[2]), "analysis"
    # 4) Fallback: früheste/späteste Oracle-Zeitangabe im Kopfbereich
    if not (info.begin and info.end):
        stamps = [t for t in (parse_oracle_ts(v) for v in _RX_ORA_TS.findall(text[:20000])) if t]
        if len(stamps) >= 2:
            info.begin, info.end, info.time_source = min(stamps), max(stamps), "fallback"
    if info.begin and info.end and not info.periods:
        info.periods = [(info.begin, info.end)]
    m = _RX_DBNAME.search(text)
    if m:
        info.db_name = m.group(1)
        info.instance = m.group(3)
    return info


# --------------------------------------------------------------------------- Logs

@dataclass
class LogInfo:
    first: Optional[datetime] = None
    last: Optional[datetime] = None
    lines: int = 0
    stamped_lines: int = 0
    days: set[str] = field(default_factory=set)
    sha256: str = ""  # über den (entpackten) Inhalt - erkennt identische Dateien


def scan_log(path: Path, progress: Optional[Callable[[float], None]] = None) -> LogInfo:
    """``progress(anteil 0..1)`` wird bei großen, unkomprimierten Dateien periodisch gemeldet."""
    info = LogInfo()
    parser = LineTimestampParser()
    size = path.stat().st_size
    report = progress is not None and not is_gzip(path) and size > 0
    read = 0
    digest = hashlib.sha256()
    with open_binary(path) as fh:
        for line in fh:
            digest.update(line)
            info.lines += 1
            if report:
                read += len(line)
                if info.lines % 200_000 == 0:
                    progress(read / size)
            ts = parser.parse(line)
            if ts is None:
                continue
            info.stamped_lines += 1
            if info.first is None or ts < info.first:
                info.first = ts
            if info.last is None or ts > info.last:
                info.last = ts
            info.days.add(ts.date().isoformat())
    info.sha256 = digest.hexdigest()
    return info


def days_between(begin: datetime, end: datetime) -> list[str]:
    """Alle Kalendertage, die das Intervall [begin, end] berührt."""
    out, d = [], begin.date()
    while d <= end.date():
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out
