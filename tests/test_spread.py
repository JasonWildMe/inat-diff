"""Tests for spread (county first-detection) ingestion, tasks and the iNat pacer.

Network is never used: sources are fed canned pages; Redis is fakeredis.
"""
import os, sys, tempfile
from datetime import datetime, date, timedelta
from pathlib import Path
from unittest import mock

TEST_DB = Path(tempfile.mkdtemp()) / "spread.db"   # app-level DATABASE_URL is forced in conftest.py
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
pytest.importorskip("shapely")
pytest.importorskip("celery")

from webapp import spread_sources as src
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from webapp.models import (Base, TrackedSpecies, SourceSync, CountyDetection, SpreadSource, SyncStatus)

# These tests delete rows, so they never touch the app's configured database: they use an
# engine of their own on a freshly created temp file, and the task module is pointed at it.
test_engine = create_engine(f"sqlite:///{TEST_DB}")
assert Path(test_engine.url.database) == TEST_DB
Base.metadata.create_all(test_engine)
SessionLocal = sessionmaker(bind=test_engine, autoflush=False)
COUNTIES = src.county_index()
BERKS = (40.4170, -75.9270)       # well inside Berks County, PA (42011)
LANCASTER = (40.0420, -76.2500)   # inside Lancaster County, PA (42071)
_edge = COUNTIES.by_fips["42011"].boundary.intersection(COUNTIES.by_fips["42071"].boundary)
_pt = _edge.interpolate(0.5, normalized=True)
BORDER = (_pt.y, _pt.x)           # a point on the Berks-Lancaster line


# ----------------------------------------------------------------- county assignment

def test_assign_inside_county():
    assert COUNTIES.assign(*BERKS, 30) == "42011"
    assert COUNTIES.assign(*LANCASTER, None) == "42071"   # unknown accuracy -> 1 km footprint


def test_assign_rejects_uncertain_points():
    assert COUNTIES.assign(*BERKS, 20_000) is None         # > 10 km
    assert COUNTIES.assign(*BORDER, 2_000) is None         # footprint spans two counties
    assert COUNTIES.assign(*BORDER, None) is None
    assert COUNTIES.assign("x", "y", 10) is None
    assert COUNTIES.assign(95, 0, 10) is None


def test_contradicts():
    assert COUNTIES.contradicts("42071", *BERKS, 30) is True
    assert COUNTIES.contradicts("42011", *BERKS, 30) is False
    assert COUNTIES.contradicts("42011", *BERKS, 50_000) is False  # unusable coords never contradict


# ----------------------------------------------------------------- iNat

def inat_obs(i, day="2020-07-01", loc=BERKS, obscured=False, acc=10):
    return {"id": i, "observed_on": day, "location": f"{loc[0]},{loc[1]}", "obscured": obscured,
            "positional_accuracy": acc}


def fake_inat(pages, recount):
    """get_page that serves `pages` in order, then answers the recount request."""
    calls = iter(pages)

    def get_page(params):
        if params.get("per_page") == 0:
            return {"total_results": recount, "results": []}
        return next(calls)
    return get_page


def test_iter_inat_filters_and_completes():
    full = [inat_obs(i) for i in range(1, 201)]
    full[0] = inat_obs(1, obscured=True)
    full[1] = inat_obs(2, day=None)
    pages = [{"total_results": 203, "results": full},
             {"total_results": 203, "results": [inat_obs(201, loc=LANCASTER), inat_obs(202), inat_obs(203)]}]
    stats = {}
    recs = list(src.iter_inat(fake_inat(pages, 203), 1, COUNTIES, 10, stats))
    assert stats["records_seen"] == 203
    assert len(recs) == 201 and ("42071", "2020-07-01", "201") in recs


def test_iter_inat_rejects_non_increasing_ids():
    pages = [{"total_results": 2, "results": [inat_obs(5), inat_obs(5)]}]
    with pytest.raises(src.ScanIncomplete):
        list(src.iter_inat(fake_inat(pages, 2), 1, COUNTIES, 10, {}))


def test_iter_inat_detects_truncation_by_recount():
    pages = [{"total_results": 5000, "results": [inat_obs(1)]}]   # spurious short page
    with pytest.raises(src.ScanIncomplete):
        list(src.iter_inat(fake_inat(pages, 5000), 1, COUNTIES, 100, {}))


def test_iter_inat_oversized():
    pages = [{"total_results": 10_000, "results": []}]
    with pytest.raises(src.ScanOversized):
        list(src.iter_inat(fake_inat(pages, 10_000), 1, COUNTIES, 10, {}))


def test_iter_inat_defers_when_budget_cannot_finish():
    pages = [{"total_results": 1000, "results": [inat_obs(i) for i in range(1, 201)]}]
    asked = []

    def ensure(n):
        asked.append(n)
        raise RuntimeError("no budget")
    with pytest.raises(RuntimeError):
        list(src.iter_inat(fake_inat(pages, 1000), 1, COUNTIES, 100, {}, ensure_budget=ensure))
    assert asked == [6]   # 5 more pages + terminal page + recount - page 1 already spent


def test_iter_inat_empty_scan_of_nonempty_source_fails():
    pages = [{"total_results": 8, "results": []}]
    with pytest.raises(src.ScanIncomplete):
        list(src.iter_inat(fake_inat(pages, 8), 1, COUNTIES, 10, {}))
    pages = [{"total_results": 0, "results": []}]                 # first page says 0, recount says 8
    with pytest.raises(src.ScanIncomplete):
        list(src.iter_inat(fake_inat(pages, 8), 1, COUNTIES, 10, {}))


# ----------------------------------------------------------------- GBIF / EDDMapS

class FakeSession:
    def __init__(self, pages):
        self.pages = list(pages)

    def get(self, url, params=None, timeout=None):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = self.pages.pop(0)
        return resp


def gbif_rec(key, y=2019, m=5, d=2, loc=BERKS, **kw):
    r = {"key": key, "year": y, "month": m, "day": d, "eventDate": f"{y}-{m:02d}",
         "decimalLatitude": loc[0], "decimalLongitude": loc[1], "coordinateUncertaintyInMeters": 50}
    r.update(kw)
    return r


def test_iter_gbif_eligibility():
    page = {"count": 4, "endOfRecords": True, "results": [
        gbif_rec(1), gbif_rec(2, references="https://www.inaturalist.org/observations/9"),
        gbif_rec(3, eventDate="2019-05-01/2019-05-30"), gbif_rec(4, d=None)]}
    stats = {}
    recs = list(src.iter_gbif(7, COUNTIES, stats, session=FakeSession([page])))
    assert recs == [("42011", "2019-05-02", "1")] and stats["records_seen"] == 4


def test_iter_gbif_county_label_conflict_and_empty_scan():
    page = {"count": 2, "endOfRecords": True, "results": [
        gbif_rec(1, county="Lancaster"), gbif_rec(2, county="Berks Co.")]}
    assert [r[2] for r in src.iter_gbif(7, COUNTIES, {}, session=FakeSession([page]))] == ["2"]
    with pytest.raises(src.ScanIncomplete):
        list(src.iter_gbif(7, COUNTIES, {}, session=FakeSession([{"count": 8, "endOfRecords": True, "results": []}])))


def test_iter_gbif_repeated_page_fails():
    p = {"count": 600, "endOfRecords": False, "results": [gbif_rec(i) for i in range(300)]}
    with pytest.raises(src.ScanIncomplete):
        with mock.patch.object(src.time, "sleep"):
            list(src.iter_gbif(7, COUNTIES, {}, session=FakeSession([p, dict(p, endOfRecords=True)])))


def edd_rec(oid, **kw):
    r = {"objectid": oid, "reviewed": True, "infestationstatus": "Positive", "eradicationstatus": "Positive",
         "yearaccuracy": None, "dateuncertaintyindays": None, "observationdate": "2018-09-01T00:00:00.000Z",
         "fipscode": "42011", "coordinates": f"{BERKS[0]}, {BERKS[1]}", "coordinateuncertaintyinmeters": 10,
         "url": None, "projectname": "PA Dept of Agriculture", "reporter": "Survey", "OriginalRecordID": None}
    r.update(kw)
    return r


def test_iter_eddmaps_eligibility():
    rows = [edd_rec(1), edd_rec(2, yearaccuracy="To the Year"),
            edd_rec(3, url="https://www.inaturalist.org/observations/1"), edd_rec(4, projectname="iNaturalist API"),
            edd_rec(5, reviewed=False), edd_rec(6, infestationstatus="Negative"),
            edd_rec(7, dateuncertaintyindays=1), edd_rec(8, fipscode="42071"),          # coords contradict FIPS
            edd_rec(9, fipscode="42071", coordinates=None),                            # source-reported FIPS
            edd_rec(10, infestationstatus=None, eradicationstatus=None),              # no presence evidence
            edd_rec(11, infestationstatus=None, eradicationstatus="Eradicated"),
            edd_rec(12, infestationstatus=None, eradicationstatus="Treated")]
    page = {"totalrows": 12, "nextpage": "", "data": rows}
    stats = {}
    recs = list(src.iter_eddmaps(77293, COUNTIES, stats, session=FakeSession([page])))
    assert recs == [("42011", "2018-09-01", "1"), ("42071", "2018-09-01", "9")]
    assert stats["records_seen"] == 12


def test_iter_eddmaps_empty_scan_fails():
    with pytest.raises(src.ScanIncomplete):
        list(src.iter_eddmaps(1, COUNTIES, {}, session=FakeSession([{"totalrows": 8, "nextpage": "", "data": []}])))


def test_iter_eddmaps_accepts_only_one_record_gap():
    rows = [edd_rec(i) for i in range(1, 4)]
    assert len(list(src.iter_eddmaps(1, COUNTIES, {}, session=FakeSession(
        [{"totalrows": 4, "nextpage": "", "data": rows}])))) == 3
    with pytest.raises(src.ScanIncomplete):
        list(src.iter_eddmaps(1, COUNTIES, {}, session=FakeSession([{"totalrows": 5, "nextpage": "", "data": rows}])))
    with pytest.raises(src.ScanIncomplete):       # a missing terminal marker is not "last page"
        list(src.iter_eddmaps(1, COUNTIES, {}, session=FakeSession([{"totalrows": 4, "data": rows}])))


def test_county_label_keeps_jurisdiction_type():
    assert src._county_conflicts("Baltimore County", "Baltimore city")
    assert not src._county_conflicts("Baltimore", "Baltimore city")
    assert not src._county_conflicts("Juneau City and Borough", "Juneau City and Borough")
    assert not src._county_conflicts("St. Louis Co.", "St. Louis County")


def test_retry_delay_parses_both_forms():
    assert src.retry_delay("30", 0) == 30
    assert src.retry_delay(None, 1) == 20
    assert src.retry_delay("3600", 0) is None      # longer than we will wait: give up this run
    assert 0 <= src.retry_delay("Wed, 21 Oct 2015 07:28:00 GMT", 0) <= 1   # past date -> now


def test_iter_eddmaps_totalrows_mismatch():
    pages = [{"totalrows": 50, "nextpage": "p2", "data": [edd_rec(1)]},
             {"totalrows": 50, "nextpage": "", "data": []}]
    with pytest.raises(src.ScanIncomplete), mock.patch.object(src.time, "sleep"):
        list(src.iter_eddmaps(1, COUNTIES, {}, session=FakeSession(pages)))


# ----------------------------------------------------------------- tasks

from webapp import spread_tasks
from webapp.inat_pacer import BudgetExhausted



@pytest.fixture(autouse=True)
def _spread_tasks_use_test_engine():
    with mock.patch.object(spread_tasks, "SessionLocal", SessionLocal):
        yield


@pytest.fixture
def species():
    db = SessionLocal()
    db.query(CountyDetection).delete()
    db.query(SourceSync).delete()
    db.query(TrackedSpecies).delete()
    sp = TrackedSpecies(inat_taxon_id=324726, gbif_taxon_key=5157899, eddmaps_subject_id=77293,
                        scientific_name="Lycorma delicatula")
    sp.syncs = [SourceSync(source=s) for s in SpreadSource]
    db.add(sp)
    db.commit()
    sid = sp.id
    db.close()
    yield sid


def detections(sid, source=SpreadSource.INAT):
    db = SessionLocal()
    rows = {(r.fips, r.first_date.isoformat(), r.first_record_id)
            for r in db.query(CountyDetection).filter_by(species_id=sid, source=source)}
    sync = db.query(SourceSync).filter_by(species_id=sid, source=source).one()
    state = (sync.status, sync.claim_started_at, sync.last_success_at, sync.error_message)
    db.close()
    return rows, state


def run_with(records, sid, exc=None, during=None, force=True):
    def gen(source, source_id, stats):
        for r in records:
            if during:
                during()
            yield r
        if exc:
            raise exc
        stats["records_seen"] = len(records)
    with mock.patch.object(spread_tasks, "_records", side_effect=gen):
        return spread_tasks.sync_species_source.run(sid, "inat", force=force)


def test_sync_publishes_earliest_with_deterministic_ties(species):
    out = run_with([("42011", "2020-01-02", "50"), ("42011", "2019-06-01", "9"),
                    ("42011", "2019-06-01", "10"), ("42071", "2021-03-03", "77")], species)
    assert out["status"] == "ok" and out["counties"] == 2
    rows, state = detections(species)
    assert rows == {("42011", "2019-06-01", "9"), ("42071", "2021-03-03", "77")}
    assert state[0] == SyncStatus.IDLE and state[1] is None and state[2] is not None


def test_sync_rebuild_replaces_and_failure_keeps_previous(species):
    run_with([("42011", "2019-06-01", "9"), ("42071", "2021-03-03", "77")], species)
    run_with([("42011", "2019-06-01", "9")], species)                         # 42071 record gone
    assert detections(species)[0] == {("42011", "2019-06-01", "9")}
    out = run_with([("42003", "2018-01-01", "1")], species, exc=src.ScanIncomplete("short"))
    rows, state = detections(species)
    assert out["status"] == "failed" and rows == {("42011", "2019-06-01", "9")}
    assert state[0] == SyncStatus.FAILED and "ScanIncomplete" in state[3]


def test_sync_budget_deferral_releases_without_success(species):
    run_with([("42011", "2019-06-01", "9")], species)
    before = detections(species)
    out = run_with([("42003", "2018-01-01", "1")], species, exc=BudgetExhausted("cap"))
    rows, state = detections(species)
    assert out["status"] == "deferred" and rows == before[0]
    assert state[0] == SyncStatus.IDLE and state[1] is None and state[2] == before[1][2]


def test_sync_lost_claim_publishes_nothing(species):
    def steal():
        db = SessionLocal()
        db.query(SourceSync).filter_by(species_id=species, source=SpreadSource.INAT).update(
            {SourceSync.claim_started_at: datetime.utcnow() + timedelta(seconds=5)})
        db.commit()
        db.close()
    out = run_with([("42011", "2019-06-01", "9")], species, during=steal)
    assert out["status"] == "lost_claim" and detections(species)[0] == set()


def test_sync_skips_when_already_running(species):
    db = SessionLocal()
    db.query(SourceSync).filter_by(species_id=species, source=SpreadSource.INAT).update(
        {SourceSync.status: SyncStatus.RUNNING, SourceSync.claim_started_at: datetime.utcnow()})
    db.commit()
    db.close()
    assert run_with([("42011", "2019-06-01", "9")], species)["status"] == "already_running_or_fresh"


def test_sync_skips_fresh_unless_forced(species):
    run_with([("42011", "2019-06-01", "9")], species)
    assert run_with([("42071", "2018-01-01", "1")], species, force=False)["status"] == "already_running_or_fresh"
    assert run_with([("42071", "2018-01-01", "1")], species, force=True)["status"] == "ok"


def test_dispatch_selects_due_rows(species):
    db = SessionLocal()
    rows = {s.source: s for s in db.query(SourceSync).filter_by(species_id=species)}
    rows[SpreadSource.INAT].last_success_at = datetime.utcnow() - timedelta(days=1)       # fresh
    rows[SpreadSource.INAT].last_attempt_at = datetime.utcnow() - timedelta(days=1)
    rows[SpreadSource.GBIF].last_success_at = datetime.utcnow() - timedelta(days=30)      # stale -> due
    rows[SpreadSource.GBIF].last_attempt_at = datetime.utcnow() - timedelta(days=30)
    rows[SpreadSource.EDDMAPS].error_message = "ScanOversized: oversized"                 # oversized, recent
    rows[SpreadSource.EDDMAPS].last_attempt_at = datetime.utcnow() - timedelta(days=2)
    db.commit()
    db.close()
    with mock.patch.object(spread_tasks.sync_species_source, "delay") as delay:
        assert spread_tasks.dispatch_spread_syncs.run() == {"queued": 1}
    delay.assert_called_once_with(species, "gbif")


# ----------------------------------------------------------------- pacer

def test_pacer_spacing_count_and_cap():
    fakeredis = pytest.importorskip("fakeredis")
    pytest.importorskip("lupa")
    from webapp import inat_pacer
    r = fakeredis.FakeRedis()
    with mock.patch.object(inat_pacer, "_client", r), \
         mock.patch.object(inat_pacer, "INAT_DAILY_REQUEST_CAP", 3), \
         mock.patch.object(inat_pacer, "INAT_WORKER_INTERVAL", 0.05):
        pace = inat_pacer.make_worker_pacer(capped=True)
        t0 = __import__("time").monotonic()
        pace(); pace()
        assert __import__("time").monotonic() - t0 >= 0.045      # second call waited for the slot
        inat_pacer.count_web_request()                            # web traffic counts toward the cap
        assert inat_pacer.requests_today() == 3
        with pytest.raises(BudgetExhausted):
            pace()
        inat_pacer.make_worker_pacer(capped=False)()              # reports are never capped
        assert inat_pacer.requests_today() == 4


# ----------------------------------------------------------------- report renderer

def test_quality_lookup_pacing_failure_is_per_species():
    from inat_diff import visualize

    from inat_diff.exceptions import iNatAPIError

    class Boom:
        def __init__(self, exc):
            self.exc = exc

        def get_observations(self, **kw):
            raise self.exc
    visualize._fetch_highest_quality_grade.cache_clear()
    with mock.patch.object(visualize.time, "sleep"):
        grade, err = visualize._fetch_highest_quality_grade(12345, 1, Boom(iNatAPIError("gate unavailable")))
    assert grade is None and "API error" in err

    class SoftLimit(Exception):        # stands in for celery's SoftTimeLimitExceeded
        pass
    with pytest.raises(SoftLimit):
        visualize._fetch_highest_quality_grade(12346, 1, Boom(SoftLimit()))
