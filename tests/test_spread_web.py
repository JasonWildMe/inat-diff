"""Tests for the public spread map pages (read side). Uses the conftest temp database."""
import json, re, sys
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
pytest.importorskip("fastapi")
pytest.importorskip("slowapi")
from fastapi.testclient import TestClient
from webapp import main
from webapp.models import (SessionLocal, init_db, TrackedSpecies, SourceSync, CountyDetection,
                           SpreadSource, SyncStatus)

init_db()
client = TestClient(main.app)
HOSTILE = '</script><img src=x onerror=alert(1)>'


def embedded(html):
    m = re.search(r'<script type="application/json" id="spread-data">(.*?)</script>', html, re.S)
    assert m, "payload script element missing"
    return json.loads(m.group(1))


@pytest.fixture
def species():
    """One species: iNat ok, EDDMapS stale (later refresh failed), GBIF never built."""
    db = SessionLocal()
    sp = TrackedSpecies(inat_taxon_id=990001, gbif_taxon_key=5, eddmaps_subject_id=7,
                        scientific_name="Testus " + HOSTILE, common_name="Test bug")
    t = datetime(2026, 11, 1, 3, 0)
    sp.syncs = [
        SourceSync(source=SpreadSource.INAT, status=SyncStatus.IDLE, last_success_at=t, last_attempt_at=t),
        SourceSync(source=SpreadSource.EDDMAPS, status=SyncStatus.FAILED, last_success_at=t,
                   last_attempt_at=t, error_message="ScanIncomplete: secret worker detail"),
        SourceSync(source=SpreadSource.GBIF, status=SyncStatus.FAILED, last_attempt_at=t,
                   error_message="boom"),
    ]
    db.add(sp); db.commit()
    rows = [("42011", SpreadSource.INAT, date(2015, 11, 7), "10912226"),
            ("42011", SpreadSource.EDDMAPS, date(2014, 9, 22), HOSTILE),
            ("42071", SpreadSource.INAT, date(2017, 6, 1), "5"),
            ("72127", SpreadSource.INAT, date(2018, 1, 1), "6"),          # Puerto Rico: not shown
            ("10001", SpreadSource.GBIF, date(2010, 1, 1), "7"),          # GBIF never succeeded
            ("42001", SpreadSource.EDDMAPS, date(2099, 5, 1), "8"),       # future date: dropped
            ("42071", SpreadSource.EDDMAPS, date(2099, 5, 1), "9"),       # future; iNat row stays
            ("42017", SpreadSource.EDDMAPS, date(2019, 5, 1), "10")]      # EDDMapS-only county
    db.add_all(CountyDetection(species_id=sp.id, fips=f, source=s, first_date=d, first_record_id=r)
               for f, s, d, r in rows)
    db.commit()
    yield sp.inat_taxon_id
    db.query(CountyDetection).filter_by(species_id=sp.id).delete()
    db.delete(db.get(TrackedSpecies, sp.id)); db.commit(); db.close()


def test_map_page_payload(species):
    r = client.get(f"/spread/{species}")
    assert r.status_code == 200
    p = embedded(r.text)
    assert p["sources"]["inat"] == {"status": "ok", "last_success": "2026-11-01"}
    assert p["sources"]["eddmaps"] == {"status": "stale", "last_success": "2026-11-01"}
    assert p["sources"]["gbif"] == {"status": "unavailable", "last_success": None}
    assert p["counties"] == {
        "42011": {"inat": ["2015-11-07", "10912226"], "eddmaps": ["2014-09-22", HOSTILE]},
        "42071": {"inat": ["2017-06-01", "5"]},
        "42017": {"eddmaps": ["2019-05-01", "10"]},
    }
    assert p["year"] == datetime.utcnow().year
    assert "secret worker detail" not in r.text and "boom" not in r.text


def test_map_page_escapes_hostile_values(species):
    r = client.get(f"/spread/{species}")
    assert HOSTILE not in r.text                     # neither in the HTML nor inside the JSON
    assert "<img src=x" not in r.text


def test_spread_csp_and_versioned_assets(species):
    for url in ("/spread", f"/spread/{species}"):
        csp = client.get(url).headers["content-security-policy"]
        assert "script-src 'self';" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    html = client.get(f"/spread/{species}").text
    assert re.search(r'src="/static/spread/spread\.js\?v=[0-9a-f]{10}"', html)
    assert re.search(r'data-topo="/static/spread/counties\.topo\.json\?v=[0-9a-f]{10}"', html)
    assert "document.write" not in html              # inline footer script would be blocked by CSP


def test_index_counts_shown_counties_from_usable_sources(species):
    r = client.get("/spread")
    assert r.status_code == 200
    assert f'href="/spread/{species}"' in r.text
    assert "3 counties with a record" in r.text      # PR, future and never-built GBIF rows excluded
    assert HOSTILE not in r.text


def test_unknown_and_inactive_species_404(species):
    assert client.get("/spread/123456789").status_code == 404
    db = SessionLocal()
    db.query(TrackedSpecies).filter_by(inat_taxon_id=species).update({"is_active": False}); db.commit(); db.close()
    assert client.get(f"/spread/{species}").status_code == 404
    assert f'href="/spread/{species}"' not in client.get("/spread").text


def test_not_linked_source():
    db = SessionLocal()
    sp = TrackedSpecies(inat_taxon_id=990002, scientific_name="Solo inat")
    sp.syncs = [SourceSync(source=s) for s in SpreadSource]
    db.add(sp); db.commit()
    try:
        p = embedded(client.get("/spread/990002").text)
        assert p["sources"]["gbif"]["status"] == "not_linked"
        assert p["sources"]["inat"]["status"] == "pending"
        assert p["counties"] == {}
    finally:
        db.delete(sp); db.commit(); db.close()


TOPO = main.spread_view.STATIC_DIR / "spread" / "counties.topo.json"


def test_topojson_covers_states_and_dc_only():
    topo = json.loads(TOPO.read_text())
    ids = [g["id"] for g in topo["objects"]["counties"]["geometries"]]
    assert len(ids) == len(set(ids)) == 3143
    assert all(main.spread_view.shown(i) for i in ids) and "11001" in ids


def test_topojson_exterior_rings_are_clockwise():
    """d3-geo draws a counter-clockwise exterior ring as the whole globe minus the county."""
    topo = json.loads(TOPO.read_text())
    (sx, sy), (tx, ty) = topo["transform"]["scale"], topo["transform"]["translate"]
    arcs = []
    for arc in topo["arcs"]:
        x = y = 0
        pts = []
        for dx, dy in arc:
            x += dx; y += dy
            pts.append((x * sx + tx, y * sy + ty))
        arcs.append(pts)

    def ring(idx):
        pts = []
        for i in idx:
            a = arcs[i] if i >= 0 else arcs[~i][::-1]
            pts.extend(a if not pts else a[1:])
        return pts

    def signed_area(pts):   # shoelace in lon/lat; > 0 means counter-clockwise
        return sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:])) / 2

    bad = []
    for g in topo["objects"]["counties"]["geometries"]:
        polys = [g["arcs"]] if g["type"] == "Polygon" else g["arcs"]
        bad += [g["id"] for p in polys if signed_area(ring(p[0])) >= 0]
    assert not bad, bad


def test_unlinking_a_published_source_hides_its_rows(species):
    db = SessionLocal()
    db.query(TrackedSpecies).filter_by(inat_taxon_id=species).update({"eddmaps_subject_id": None}); db.commit(); db.close()
    p = embedded(client.get(f"/spread/{species}").text)
    assert p["sources"]["eddmaps"] == {"status": "not_linked", "last_success": None}
    assert p["counties"]["42011"] == {"inat": ["2015-11-07", "10912226"]}
    assert "42017" not in p["counties"]
    assert "2 counties with a record" in client.get("/spread").text   # was 3: same rule on the index


def test_index_distinguishes_no_data_from_measured_zero():
    db = SessionLocal()
    t = datetime(2026, 11, 1)
    a = TrackedSpecies(inat_taxon_id=990003, scientific_name="Never built")
    a.syncs = [SourceSync(source=SpreadSource.INAT)]
    b = TrackedSpecies(inat_taxon_id=990004, scientific_name="Built empty")
    b.syncs = [SourceSync(source=SpreadSource.INAT, last_success_at=t, last_attempt_at=t)]
    db.add_all([a, b]); db.commit()
    try:
        html = client.get("/spread").text
        assert re.search(r"Never built.*?no published data yet", html, re.S)
        assert re.search(r"Built empty.*?0 counties with a record", html, re.S)
    finally:
        db.delete(a); db.delete(b); db.commit(); db.close()


def test_missing_geometry_still_renders(species, monkeypatch):
    monkeypatch.setattr(main.spread_view, "_asset_urls", {})
    monkeypatch.setattr(main.spread_view, "STATIC_DIR", Path("/nonexistent"))
    r = client.get(f"/spread/{species}")
    assert r.status_code == 200
    assert 'data-topo="/static/spread/counties.topo.json"' in r.text
    assert embedded(r.text)["counties"]


def test_out_of_range_taxon_id_is_rejected_cleanly():
    assert client.get(f"/spread/{2**63}").status_code == 422
    assert client.get("/spread/0").status_code == 422


def test_large_stored_taxon_id_is_reachable():
    db = SessionLocal()
    sp = TrackedSpecies(inat_taxon_id=2**31, scientific_name="Big id")
    db.add(sp); db.commit()
    try:
        assert client.get(f"/spread/{2**31}").status_code == 200
    finally:
        db.delete(sp); db.commit(); db.close()
