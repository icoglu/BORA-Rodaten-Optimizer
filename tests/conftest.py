from __future__ import annotations

import gzip
import os
from pathlib import Path

import pytest

ACCESS = """\
10.0.0.1 - - [26/Sep/2026:23:59:58 +0200] "GET /bora/start HTTP/1.1" 200 512
10.0.0.2 - - [27/Sep/2026:00:00:01 +0200] "POST /bora/api HTTP/1.1" 201 17
10.0.0.3 - - [27/Sep/2026:10:15:00 +0200] "GET /bora/list HTTP/1.1" 200 2048
10.0.0.4 - - [27/Sep/2026:11:30:00 +0200] "GET /bora/list HTTP/1.1" 500 99
"""

SERVER = """\
####<Sep 27, 2026 10:05:00,123 AM CEST> <Info> <Server> <host> <AdminServer> <main> <<WLS Kernel>> <> <> <1> <BEA-000000> <Start>
####<Sep 27, 2026 10:20:00,000 AM CEST> <Error> <HTTP> <host> <AdminServer> <[ACTIVE]> <<anonymous>> <> <> <2> <BEA-101020> <Fehler>
java.lang.NullPointerException
\tat de.bora.Foo.bar(Foo.java:42)
####<Sep 27, 2026 1:05:00,000 PM CEST> <Info> <Server> <host> <AdminServer> <main> <<WLS Kernel>> <> <> <3> <BEA-000001> <Nachmittag>
"""

SERVER_ROTATED = """\
####<Sep 26, 2026 11:00:00,000 PM CEST> <Info> <Server> <host> <AdminServer> <main> <<WLS Kernel>> <> <> <1> <BEA-000000> <Gestern>
"""

AWR_HTML = """<html><head><title>AWR Report for DB: BORA, Inst: bora1, Snaps: 100-101</title></head><body>
<h1>WORKLOAD REPOSITORY report for</h1>
<table><tr><th>DB Name</th><th>DB Id</th><th>Instance</th><th>Inst num</th></tr>
<tr><td>BORA</td><td>123456</td><td>bora1</td><td>1</td></tr></table>
<table>
<tr><td></td><th>Snap Id</th><th>Snap Time</th><th>Sessions</th></tr>
<tr><td class='awrc'>Begin Snap:</td><td class='awrc'>100</td><td class='awrc'>27-Sep-26 10:00:05</td><td>45</td></tr>
<tr><td class='awrnc'>End Snap:</td><td class='awrnc'>101</td><td class='awrnc'>27-Sep-26 11:00:07</td><td>47</td></tr>
</table></body></html>
"""

AWR_TXT = """
WORKLOAD REPOSITORY report for

DB Name         DB Id    Instance     Inst Num Startup Time    Release     RAC
------------ ----------- ------------ -------- --------------- ----------- ---
BORA              123456 bora1               1 01-Sep-26 08:00 19.0.0.0.0  NO

              Snap Id      Snap Time      Sessions Curs/Sess
            --------- ------------------- -------- ---------
Begin Snap:       102 27-Sep-26 12:59:50        45       2.3
  End Snap:       103 27-Sep-26 14:00:10        47       2.4
"""


@pytest.fixture()
def sample_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data" / "inbox" / "wls01"
    d.mkdir(parents=True)
    (d / "access.log").write_text(ACCESS)
    (d / "server1.log").write_text(SERVER)
    with gzip.open(d / "server1.log00001.gz", "wt") as fh:
        fh.write(SERVER_ROTATED)
    (d / "awrrpt_1_100_101.html").write_text(AWR_HTML)
    (d / "report_102_103.txt").write_text(AWR_TXT)
    (d / "notizen.txt").write_text("irgendwas\n")
    os.utime(d / "notizen.txt", (0, 0))  # Datei ohne bekanntes Datum
    return tmp_path / "data"
