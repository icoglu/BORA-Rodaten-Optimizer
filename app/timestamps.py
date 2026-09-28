"""Zeitstempel-Erkennung für Access-, Server- und AWR-Formate.

Alle Zeiten werden als *naive* lokale Zeit so interpretiert, wie sie in der
Datei stehen (Zeitzonen-Offsets werden bewusst ignoriert), damit Access-Log,
Server-Log und AWR-Report derselben Maschine auf einer gemeinsamen Zeitachse
liegen.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Callable, Optional

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "mär": 3, "mrz": 3, "apr": 4, "may": 5, "mai": 5,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "okt": 10, "nov": 11,
    "dec": 12, "dez": 12,
}

# Nur der Zeilenanfang wird untersucht - Zeitstempel stehen dort, Inhalte
# weiter hinten (z.B. in Stacktraces) sollen nicht fälschlich matchen.
HEAD_BYTES = 256
WHOLE_LINE_BYTES = 64 * 1024


def _month(name: str) -> int:
    return MONTHS[name[:3].lower()]  # KeyError -> kein gültiger Zeitstempel


def _year(y: str) -> int:
    v = int(y)
    return v + 2000 if v < 100 else v


def _hour12(h: str, ampm: Optional[str]) -> int:
    hour = int(h)
    if ampm:
        ampm = ampm.upper()
        if ampm == "PM" and hour < 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
    return hour


_M = r"([A-Za-zäÄ]{3,4})\.?"

# (Regex, Konverter) - Reihenfolge = Priorität
PATTERNS: list[tuple[re.Pattern[str], Callable[[re.Match[str]], datetime]]] = [
    # Apache/WebLogic Common/Combined Log Format: [27/Sep/2026:10:00:00 +0200]
    (re.compile(r"\[(\d{1,2})/" + _M + r"/(\d{4}):(\d{2}):(\d{2}):(\d{2})"),
     lambda m: datetime(int(m[3]), _month(m[2]), int(m[1]), int(m[4]), int(m[5]), int(m[6]))),
    # WebLogic Server-Log: ####<Sep 27, 2026 10:15:30,123 AM CEST> bzw. <Sep 27, 2026, 10:15:30 AM CEST>
    (re.compile(r"<" + _M + r" (\d{1,2}),? (\d{4}),? (\d{1,2}):(\d{2}):(\d{2})(?:[,.]\d+)?\s?([AaPp][Mm])?"),
     lambda m: datetime(int(m[3]), _month(m[1]), int(m[2]), _hour12(m[4], m[7]), int(m[5]), int(m[6]))),
    # ISO / log4j / W3C-Extended: 2026-09-27 10:15:30 | 2026-09-27T10:15:30 | 2026-09-27<TAB>10:15:30
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})[T\s](\d{2}):(\d{2}):(\d{2})"),
     lambda m: datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6]))),
    # Deutsches Format: 27.09.2026 10:15:30 bzw. 27.09.2026 10:15
    (re.compile(r"(\d{2})\.(\d{2})\.(\d{4}),?\s(\d{1,2}):(\d{2})(?::(\d{2}))?"),
     lambda m: datetime(int(m[3]), int(m[2]), int(m[1]), int(m[4]), int(m[5]), int(m[6] or 0))),
    # Oracle / Tomcat: 27-Sep-2026 10:15:30 bzw. 27-Sep-26 10:15:30
    (re.compile(r"(\d{1,2})-" + _M + r"-(\d{2,4})\s(\d{2}):(\d{2}):(\d{2})"),
     lambda m: datetime(_year(m[3]), _month(m[2]), int(m[1]), int(m[4]), int(m[5]), int(m[6]))),
    # Englisch ausgeschrieben ohne Klammern: Sep 27, 2026 10:15:30 AM
    (re.compile(r"\b" + _M + r" (\d{1,2}),? (\d{4}),? (\d{1,2}):(\d{2}):(\d{2})(?:[,.]\d+)?\s?([AaPp][Mm])?"),
     lambda m: datetime(int(m[3]), _month(m[1]), int(m[2]), _hour12(m[4], m[7]), int(m[5]), int(m[6]))),
]


def _try(idx: int, text: str) -> Optional[datetime]:
    rx, conv = PATTERNS[idx]
    m = rx.search(text)
    if not m:
        return None
    try:
        return conv(m)
    except (KeyError, ValueError):
        return None


def parse_text(text: str) -> Optional[datetime]:
    for i in range(len(PATTERNS)):
        ts = _try(i, text)
        if ts:
            return ts
    return None


class LineTimestampParser:
    """Zustandsbehafteter Parser: merkt sich das zuletzt erfolgreiche Muster
    einer Datei, um große Logs schnell zu verarbeiten."""

    def __init__(self, whole_line: bool = False) -> None:
        self._preferred: Optional[int] = None
        # whole_line: Zeitstempel irgendwo in der Zeile suchen (z.B. JSON-Logs), nicht nur am Anfang
        self.whole_line = whole_line

    def parse(self, raw: bytes) -> Optional[datetime]:
        if self.whole_line:
            stamps, _ = find_all(raw[:WHOLE_LINE_BYTES].decode("utf-8", errors="replace"))
            return stamps[0] if stamps else None
        text = raw[:HEAD_BYTES].decode("utf-8", errors="replace")
        if self._preferred is not None:
            ts = _try(self._preferred, text)
            if ts:
                return ts
        for i in range(len(PATTERNS)):
            if i == self._preferred:
                continue
            ts = _try(i, text)
            if ts:
                self._preferred = i
                return ts
        return None


# Reines Datum ohne Uhrzeit (z.B. in CSV/XML/HTML): 2026-09-27 | 27.09.2026
_DATE_ONLY = [
    (re.compile(r"(?<!\d)(20\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(?![T\s:.]?\d)"),
     lambda m: datetime(int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"(?<!\d)(0[1-9]|[12]\d|3[01])\.(0[1-9]|1[0-2])\.(20\d{2})(?![,\s]*\d{1,2}[:.]\d)"),
     lambda m: datetime(int(m[3]), int(m[2]), int(m[1]))),
]
PLAUSIBLE_FROM, PLAUSIBLE_TO = datetime(2000, 1, 1), datetime(2100, 1, 1)


def find_all(text: str) -> tuple[list[datetime], list[datetime]]:
    """Alle Zeitangaben einer Zeile - nicht nur am Zeilenanfang.
    Liefert (Zeitpunkte mit Uhrzeit, reine Datumsangaben)."""
    stamps: list[datetime] = []
    for rx, conv in PATTERNS:
        for m in rx.finditer(text):
            try:
                ts = conv(m)
            except (KeyError, ValueError):
                continue
            if PLAUSIBLE_FROM <= ts < PLAUSIBLE_TO:
                stamps.append(ts)
    dates: list[datetime] = []
    for rx, conv in _DATE_ONLY:
        for m in rx.finditer(text):
            try:
                d = conv(m)
            except ValueError:
                continue
            if PLAUSIBLE_FROM <= d < PLAUSIBLE_TO:
                dates.append(d)
    return stamps, dates
