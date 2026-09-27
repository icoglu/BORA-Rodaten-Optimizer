from datetime import datetime
from pathlib import Path

from app import detect

ASH_HTML = """<html><head><title>ASH Report</title></head><body>
<h1 class="awr">ASH Report For BORA/bora1</h1>
<table><tr><th>DB Name</th><th>DB Id</th><th>Instance</th></tr>
<tr><td>BORA</td><td>123456</td><td>bora1</td></tr></table>
<table>
<tr><td>Analysis Begin Time:</td><td>27-Sep-26 09:30:00</td></tr>
<tr><td>Analysis End Time:</td><td>27-Sep-26 09:45:00</td></tr>
</table></body></html>
"""

ADDM_TXT = """ADDM Report for Task 'TASK_4711'
---------------------------------

Analysis Period
---------------
AWR snapshot range from 100 to 101.
Time period starts at 27-SEP-26 10.00.05 AM
Time period ends at 27-SEP-26 01.00.07 PM
"""

STATSPACK_TXT = """STATSPACK report for

Database    DbId    Instance    Inst Num Startup Time    Release     RAC
~~~~~~~~ ----------- ------------ -------- --------------- ----------- ---
            123456 bora1               1 01-Sep-26 08:00 19.0.0.0.0  NO

              Snap Id     Snap Time      Sessions Curs/Sess Comment
            --------- ------------------ -------- --------- -------------------
Begin Snap:        11 27-Sep-26 08:00:00       40       1.1
  End Snap:        12 27-Sep-26 09:00:00       41       1.2
"""

COMPARE_HTML = """<html><body><h1>WORKLOAD REPOSITORY COMPARE PERIOD REPORT</h1>
<table><tr><th>Snapshot Set</th><th>DB Name</th><th>Begin Snap Id</th><th>Begin Snap Time</th><th>End Snap Id</th><th>End Snap Time</th></tr>
<tr><td>First (1st)</td><td>BORA</td><td>100</td><td>26-Sep-26 10:00:05 (Sat)</td><td>101</td><td>26-Sep-26 11:00:07 (Sat)</td></tr>
<tr><td>Second (2nd)</td><td>BORA</td><td>200</td><td>27-Sep-26 10:00:02 (Sun)</td><td>201</td><td>27-Sep-26 11:00:04 (Sun)</td></tr>
</table></body></html>
"""

SERVER_LOG_HTML = """<html><body><table>
<tr><th>Zeit</th><th>Severity</th><th>Nachricht</th></tr>
<tr><td>Sep 27, 2026 10:05:00 AM CEST</td><td>Error</td><td>BEA-101020 Fehler</td></tr>
<tr><td>27.09.2026 11:15:00</td><td>Info</td><td>Weiter</td></tr>
</table></body></html>
"""


def _w(tmp: Path, name: str, content: str) -> Path:
    p = tmp / name
    p.write_text(content)
    return p


def test_ash_html(tmp_path: Path):
    p = _w(tmp_path, "ashrpt_1.html", ASH_HTML)
    assert detect.classify(p) == detect.AWR
    info = detect.parse_awr(p)
    assert info.report_type == "ASH" and info.time_source == "analysis"
    assert (info.begin, info.end) == (datetime(2026, 9, 27, 9, 30), datetime(2026, 9, 27, 9, 45))


def test_addm_text_ampm(tmp_path: Path):
    p = _w(tmp_path, "report.txt", ADDM_TXT)
    assert detect.classify(p) == detect.AWR
    info = detect.parse_awr(p)
    assert info.report_type == "ADDM"
    assert (info.begin, info.end) == (datetime(2026, 9, 27, 10, 0, 5), datetime(2026, 9, 27, 13, 0, 7))


def test_statspack(tmp_path: Path):
    info = detect.parse_awr(_w(tmp_path, "sp_11_12.lst", STATSPACK_TXT))
    assert info.report_type == "Statspack" and (info.begin_snap, info.end_snap) == (11, 12)
    assert (info.begin, info.end) == (datetime(2026, 9, 27, 8), datetime(2026, 9, 27, 9))


def test_awr_compare_spans_both_periods(tmp_path: Path):
    p = _w(tmp_path, "awrdiff_1_100_1_200.html", COMPARE_HTML)
    assert detect.classify(p) == detect.AWR
    info = detect.parse_awr(p)
    assert info.report_type == "AWR Compare"
    assert (info.begin, info.end) == (datetime(2026, 9, 26, 10, 0, 5), datetime(2026, 9, 27, 11, 0, 4))
    assert info.periods == [(datetime(2026, 9, 26, 10, 0, 5), datetime(2026, 9, 26, 11, 0, 7)),
                            (datetime(2026, 9, 27, 10, 0, 2), datetime(2026, 9, 27, 11, 0, 4))]


def test_awr_compare_gives_one_package_per_period(tmp_path: Path):
    from app import packager
    from app.catalog import Catalog
    inbox = tmp_path / "inbox" / "db"
    inbox.mkdir(parents=True)
    _w(inbox, "awrdiff_1_100_1_200.html", COMPARE_HTML)
    cat = Catalog(tmp_path / "c.sqlite3")
    cat.scan({"inbox": tmp_path / "inbox"})
    recs = cat.all()
    assert recs[0].days == ["2026-09-26", "2026-09-27"]
    names = [w.name for w in packager.plan_windows(recs, "awr")]
    assert names == ["BORA_2026-09-26_1000-1100", "BORA_2026-09-27_1000-1100"]  # nicht eine 25-h-Spanne


def test_server_log_as_html(tmp_path: Path):
    p = _w(tmp_path, "server1.log.html", SERVER_LOG_HTML)
    assert detect.classify(p) == detect.SERVER
    info = detect.scan_log(p)
    assert (info.first, info.last) == (datetime(2026, 9, 27, 10, 5), datetime(2026, 9, 27, 11, 15))
