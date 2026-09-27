from datetime import datetime
from pathlib import Path

from app import detect
from app.timestamps import LineTimestampParser, parse_text


def test_timestamp_formats():
    p = LineTimestampParser()
    assert p.parse(b'1.2.3.4 - - [27/Sep/2026:10:15:00 +0200] "GET / HTTP/1.1"') == datetime(2026, 9, 27, 10, 15)
    assert p.parse(b"####<Sep 27, 2026 1:05:00,000 PM CEST> <Info>") == datetime(2026, 9, 27, 13, 5)
    assert p.parse(b"####<Sep 27, 2026, 12:05:00,000 AM CEST> <Info>") == datetime(2026, 9, 27, 0, 5)
    assert p.parse(b"2026-09-27 10:15:30,123 INFO x") == datetime(2026, 9, 27, 10, 15, 30)
    assert p.parse(b"2026-09-27\t10:15:30\tGET\t/") == datetime(2026, 9, 27, 10, 15, 30)
    assert p.parse(b"27.09.2026 10:15:30 INFO") == datetime(2026, 9, 27, 10, 15, 30)
    assert p.parse("####<27.09.2026 10:15 Uhr MESZ>".encode()) == datetime(2026, 9, 27, 10, 15)
    assert p.parse(b"\tat de.bora.Foo.bar(Foo.java:42)") is None
    assert parse_text("27-Sep-26 10:00:05") == datetime(2026, 9, 27, 10, 0, 5)


def test_classify_and_awr(sample_dir: Path):
    d = sample_dir / "inbox" / "wls01"
    assert detect.classify(d / "access.log") == detect.ACCESS
    assert detect.classify(d / "server1.log") == detect.SERVER
    assert detect.classify(d / "server1.log00001.gz") == detect.SERVER
    assert detect.classify(d / "awrrpt_1_100_101.html") == detect.AWR
    assert detect.classify(d / "report_102_103.txt") == detect.AWR
    assert detect.classify(d / "notizen.txt") == detect.UNKNOWN

    html = detect.parse_awr(d / "awrrpt_1_100_101.html")
    assert (html.begin, html.end) == (datetime(2026, 9, 27, 10, 0, 5), datetime(2026, 9, 27, 11, 0, 7))
    assert (html.begin_snap, html.end_snap) == (100, 101)
    txt = detect.parse_awr(d / "report_102_103.txt")
    assert (txt.begin, txt.end) == (datetime(2026, 9, 27, 12, 59, 50), datetime(2026, 9, 27, 14, 0, 10))
    assert txt.db_name == "BORA" and txt.instance == "bora1"


def test_scan_log_multiline(sample_dir: Path):
    info = detect.scan_log(sample_dir / "inbox" / "wls01" / "server1.log")
    assert info.lines == 5 and info.stamped_lines == 3
    assert info.days == {"2026-09-27"}
