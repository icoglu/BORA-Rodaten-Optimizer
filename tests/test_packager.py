import json
import zipfile
from datetime import date
from pathlib import Path

from app import packager
from app.catalog import Catalog


def _catalog(data: Path) -> Catalog:
    cat = Catalog(data / "catalog.sqlite3")
    cat.scan({"inbox": data / "inbox"})
    return cat


def _read(zpath: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(zpath) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def test_day_mode(sample_dir: Path):
    cat = _catalog(sample_dir)
    recs = cat.all()
    windows = packager.plan_windows(recs, "day")
    assert [w.name for w in windows] == ["BORA_2026-09-26", "BORA_2026-09-27"]
    out = sample_dir / "output"
    res = packager.build(recs, windows, out, sample_dir / "work", "day")
    assert {r["zip"] for r in res} == {"BORA_2026-09-26.zip", "BORA_2026-09-27.zip"}

    d26 = _read(out / "BORA_2026-09-26.zip")
    assert d26["access/wls01/access.log"].count(b"\n") == 1
    assert b"Gestern" in d26["server/wls01/server1.log00001"]  # .gz entpackt, Rohzeile erhalten
    assert not any(n.startswith("awr/") for n in d26)

    d27 = _read(out / "BORA_2026-09-27.zip")
    assert d27["access/wls01/access.log"].count(b"\n") == 3
    server = d27["server/wls01/server1.log"]
    assert b"NullPointerException" in server and b"\tat de.bora.Foo.bar" in server  # Stacktrace mitgenommen
    assert "awr/wls01/awrrpt_1_100_101.html" in d27 and "awr/wls01/report_102_103.txt" in d27
    manifest = json.loads(d27["manifest.json"])
    assert manifest["paket"] == "BORA_2026-09-27" and len(manifest["dateien"]) == 4
    assert not any("notizen" in n for n in d27)
    assert not list((sample_dir / "work").iterdir())  # Temp-Daten aufgeräumt


def test_awr_mode_with_margin(sample_dir: Path):
    cat = _catalog(sample_dir)
    recs = cat.all()
    windows = packager.plan_windows(recs, "awr", margin_min=10)
    assert [w.name for w in windows] == ["BORA_2026-09-27_1000-1100", "BORA_2026-09-27_1259-1400"]
    out = sample_dir / "output"
    packager.build(recs, windows, out, sample_dir / "work", "awr")

    first = _read(out / "BORA_2026-09-27_1000-1100.zip")
    assert list(k for k in first if k.startswith("awr/")) == ["awr/wls01/awrrpt_1_100_101.html"]
    assert first["access/wls01/access.log"] == b'10.0.0.3 - - [27/Sep/2026:10:15:00 +0200] "GET /bora/list HTTP/1.1" 200 2048\n'
    assert first["server/wls01/server1.log"].count(b"\n") == 4  # 09:55-11:10 inkl. Stacktrace

    second = _read(out / "BORA_2026-09-27_1259-1400.zip")
    assert b"Nachmittag" in second["server/wls01/server1.log"]
    assert "access/wls01/access.log" not in second


def test_date_filter(sample_dir: Path):
    recs = _catalog(sample_dir).all()
    windows = packager.plan_windows(recs, "day", date_from=date(2026, 9, 27), date_to=date(2026, 9, 27))
    assert [w.name for w in windows] == ["BORA_2026-09-27"]
    packager.build(recs, windows, sample_dir / "output", sample_dir / "work", "day")
    assert [p.name for p in (sample_dir / "output").iterdir()] == ["BORA_2026-09-27.zip"]


def test_override_to_ignore(sample_dir: Path):
    cat = _catalog(sample_dir)
    access = next(r for r in cat.all() if r.rel.endswith("access.log"))
    cat.set_override(access.id, "ignore")
    recs = cat.all()
    windows = packager.plan_windows(recs, "day")
    packager.build(recs, windows, sample_dir / "output", sample_dir / "work", "day")
    assert not any(n.startswith("access/") for n in _read(sample_dir / "output" / "BORA_2026-09-27.zip"))


def test_catalog_reanalyses_after_version_change(sample_dir: Path):
    import sqlite3
    cat = _catalog(sample_dir)
    access = next(r for r in cat.all() if r.rel.endswith("access.log"))
    cat.set_override(access.id, "ignore")
    with sqlite3.connect(sample_dir / "catalog.sqlite3") as c:
        c.execute("UPDATE meta SET value='0' WHERE key='analysis_version'")
    cat = Catalog(sample_dir / "catalog.sqlite3")        # Update eingespielt
    stats = cat.scan({"inbox": sample_dir / "inbox"})
    assert stats["neu"] == len(cat.all())                # alles neu analysiert
    assert next(r for r in cat.all() if r.id == access.id).category == "ignore"  # manuelle Wahl bleibt
