"""Integration tests for webapp hardening (needs webapp/requirements.txt + pytest).

Uses a temporary SQLite DB; iNaturalist, Celery and Mandrill are mocked.
"""
import os, sys, tempfile, base64
from pathlib import Path
from datetime import datetime, timedelta
from unittest import mock

TMP = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/t.db"
os.environ["SECRET_KEY"] = "test-secret"
os.environ.pop("PROCAPTCHA_SECRET", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
pytest.importorskip("fastapi")
pytest.importorskip("celery")
pytest.importorskip("slowapi")
from fastapi.testclient import TestClient
from webapp import main, tasks
from webapp.models import SessionLocal, Subscription, Token, Report, ReportStatus, init_db, Frequency
from webapp.auth import create_admin_user
from webapp.csrf import generate_csrf_token
from inat_diff.visualize import generate_html

init_db()
client = TestClient(main.app)
PLACE = {"id": 46, "name": "Oregon", "display_name": "Oregon, US"}


@pytest.fixture(autouse=True)
def mocks():
    with mock.patch.object(main.iNatClient, "get_place_by_id", return_value=PLACE), \
         mock.patch.object(main.iNatClient, "resolve_place_with_info", return_value=(46, PLACE)), \
         mock.patch.object(main.send_verification_email_task, "delay") as vdelay, \
         mock.patch.object(main.send_confirmation_email_task, "delay") as cdelay:
        main.limiter.reset()
        yield vdelay, cdelay


def subscribe(email="a@example.com", **kw):
    data = {"email": email, "region": " Oregon ", "csrf_token": generate_csrf_token(), "place_id": "46"}
    data.update(kw)
    return client.post("/subscribe", data=data)


def test_docs_hidden_and_headers():
    assert client.get("/docs").status_code == 404
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["Referrer-Policy"] == "no-referrer"


def test_subscribe_uniform_response_and_cooldown(mocks):
    vdelay, _ = mocks
    r1 = subscribe()
    assert r1.status_code == 200 and "Check Your Email" in r1.text
    assert vdelay.call_count == 1
    db = SessionLocal()
    sub = db.query(Subscription).filter_by(email="a@example.com").one()
    assert sub.region == "Oregon" and sub.place_id == 46
    # simulate the worker having issued a verify token just now
    db.add(Token.generate(sub.id, "verify")); db.commit()
    r2 = subscribe()
    assert "Check Your Email" in r2.text
    assert vdelay.call_count == 1  # cooldown suppressed resend
    # verify -> confirmation queued, manage link shown
    tok = db.query(Token).filter_by(subscription_id=sub.id, token_type="verify").first().token
    r3 = client.get(f"/verify/{tok}")
    assert r3.status_code == 200 and r3.headers["Cache-Control"] == "no-store"
    assert mocks[1].call_count == 1
    r4 = subscribe()  # already verified -> same generic response, no mail
    assert "Check Your Email" in r4.text and vdelay.call_count == 1
    db.close()


def test_taxon_text_without_id_rejected():
    r = subscribe(email="b@example.com", taxon_filter="Frogs")
    assert r.status_code == 400 and "Invalid Taxon Filter" in r.text


def test_admin_auth_and_csrf_and_xss():
    db = SessionLocal()
    create_admin_user(db, "admin", "correct-horse-battery")
    evil = "x'-alert`1`-'@example.com"
    s = Subscription(email=evil, region="R", frequency=Frequency.MONTHLY, is_verified=True, is_active=False)
    db.add(s); db.commit(); sid = s.id; db.close()
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:correct-horse-battery").decode()}
    bad = {"Authorization": "Basic " + base64.b64encode(b"nobody:x").decode()}
    assert client.get("/admin", headers=bad).status_code == 401
    dash = client.get("/admin", headers=auth)
    assert dash.status_code == 200
    assert "'x&#39;-alert" not in dash.text and "this.dataset.email" in dash.text
    assert client.post(f"/admin/activate-subscription/{sid}", headers=auth).status_code == 403
    ok = client.post(f"/admin/activate-subscription/{sid}", headers={**auth, "X-Requested-With": "invasives-admin"})
    assert ok.status_code == 200


def test_report_html_escaped_and_csp():
    html = generate_html({"query": {"region": "<script>alert(1)</script>"}, "new_species_count": 1, "new_species": [
        {"id": 1, "name": "<img src=x onerror=alert(1)>", "preferred_common_name": "<b>x</b>"}]})
    assert "<script>alert" not in html and "<img src=x" not in html and "&lt;script&gt;" in html
    r = client.get("/report/does-not-exist")
    assert r.status_code == 404 and "default-src 'none'" in r.headers["Content-Security-Policy"]


def test_scheduled_reports_idempotent_and_whole_days():
    db = SessionLocal()
    db.add(Subscription(email="w@example.com", region="R", place_id=1, frequency=Frequency.WEEKLY,
                        is_verified=True, is_active=True))
    db.commit(); db.close()
    with mock.patch.object(tasks.generate_report_task, "delay") as d:
        r1 = tasks.generate_scheduled_reports("weekly")
        r2 = tasks.generate_scheduled_reports("weekly")
    assert r1["reports_queued"] >= 1 and r2["reports_queued"] == 0 and r2["skipped_existing"] >= 1
    db = SessionLocal()
    rep = db.query(Report).first()
    assert rep.period_start.weekday() == 0 and rep.period_end.weekday() == 6
    assert rep.period_start.hour == 0 and (rep.period_end - rep.period_start).days == 6
    db.close()


def test_generate_report_uses_stored_period_and_email_not_resent():
    db = SessionLocal()
    rep = db.query(Report).first(); rid = rep.id; db.close()
    captured = {}
    def fake_find(self, **kw):
        captured.update(kw)
        return {"query": {"region": "R"}, "new_species": [], "new_species_count": 0, "total_species_in_period": 0}
    with mock.patch("inat_diff.SpeciesQuery.find_all_new_species_in_period", fake_find), \
         mock.patch.object(tasks, "generate_html", return_value="<p>r</p>"), \
         mock.patch.object(tasks.send_report_email_task, "delay"):
        assert tasks.generate_report_task.apply(args=[rid]).result["status"] == "completed"
        # second run (duplicate delivery) must not regenerate
        assert tasks.generate_report_task.apply(args=[rid]).result["status"] == "already_completed"
    assert " to " in captured["time_period"]
    with mock.patch.object(tasks.email_service, "send_report_email", return_value=True) as send:
        tasks.send_report_email_task.apply(args=[rid])
        tasks.send_report_email_task.apply(args=[rid])
    assert send.call_count == 1


def test_same_name_different_place_is_new_subscription(mocks):
    vdelay, _ = mocks
    subscribe(email="p@example.com", region="Portland", place_id="46")
    other = {"id": 99, "name": "Portland", "display_name": "Portland, ME"}
    with mock.patch.object(main.iNatClient, "get_place_by_id", return_value=other):
        subscribe(email="p@example.com", region="Portland", place_id="99")
    db = SessionLocal()
    assert {s.place_id for s in db.query(Subscription).filter_by(email="p@example.com")} == {46, 99}
    db.close()


def test_taxon_id_zero_rejected():
    r = subscribe(email="z@example.com", taxon_filter="Frogs", taxon_id="0")
    assert r.status_code == 400


def test_delivery_claim_and_stale_running():
    db = SessionLocal()
    sub = db.query(Subscription).filter_by(email="w@example.com").one()
    rep = Report(subscription_id=sub.id, report_uuid="u-claim", period_start=datetime(2020, 1, 1),
                 period_end=datetime(2020, 1, 7), status=ReportStatus.COMPLETED, html_path=__file__,
                 email_sent_at=datetime.utcnow())  # someone else is mid-send
    stale = Report(subscription_id=sub.id, report_uuid="u-stale", period_start=datetime(2020, 2, 1),
                   period_end=datetime(2020, 2, 7), status=ReportStatus.RUNNING,
                   started_at=datetime.utcnow() - timedelta(hours=3))
    db.add_all([rep, stale]); db.commit(); rid, sid = rep.id, stale.id; db.close()
    with mock.patch.object(tasks.email_service, "send_report_email", return_value=True) as send:
        assert tasks.send_report_email_task.apply(args=[rid]).result["status"] == "send_in_progress"
    assert send.call_count == 0
    def fake_find(self, **kw):
        return {"query": {}, "new_species": [], "new_species_count": 0, "total_species_in_period": 0}
    with mock.patch("inat_diff.SpeciesQuery.find_all_new_species_in_period", fake_find), \
         mock.patch.object(tasks, "generate_html", return_value="x"), \
         mock.patch.object(tasks.send_report_email_task, "delay"):
        assert tasks.generate_report_task.apply(args=[sid]).result["status"] == "completed"


def test_pagination_repeated_page_fails():
    from inat_diff import SpeciesQuery
    from inat_diff.exceptions import iNatAPIError
    q = SpeciesQuery()
    page = {"total_results": 1000, "results": [{"taxon": {"id": 1}, "count": 1}]}
    with mock.patch.object(q.client, "get_place_by_id", return_value={"id": 1, "name": "x", "display_name": "x"}), \
         mock.patch.object(q.client, "get_species_counts", return_value=page), \
         mock.patch("time.sleep"):
        with pytest.raises(iNatAPIError):
            q.find_all_new_species_in_period(time_period="2020-01-01 to 2020-01-07", region="x",
                                             place_id=1, rate_limit=0)


def test_pagination_legit_multi_page_succeeds():
    from inat_diff import SpeciesQuery
    q = SpeciesQuery()
    def pages(**kw):
        p = kw["page"]
        base = 0 if "2020-01" in kw["d1"] else 10000  # current vs historical ids
        if p == 1:
            return {"total_results": 700, "results": [{"taxon": {"id": base + i}, "count": 1} for i in range(1, 501)]}
        return {"total_results": 700, "results": [{"taxon": {"id": base + i}, "count": 1} for i in range(501, 701)]}
    with mock.patch.object(q.client, "get_place_by_id", return_value={"id": 1, "name": "x", "display_name": "x"}), \
         mock.patch.object(q.client, "get_species_counts", side_effect=pages), \
         mock.patch("time.sleep"):
        r = q.find_all_new_species_in_period(time_period="2020-01-01 to 2020-01-07", region="x",
                                             place_id=1, rate_limit=0)
    assert r["total_species_in_period"] == 700
