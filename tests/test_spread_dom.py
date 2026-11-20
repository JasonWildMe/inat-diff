"""Browser test of the spread page's client logic (ties, toggles, slider, hostile ids, CSP).

Runs the real page in headless Chromium; every request is answered by FastAPI's TestClient,
so no server or network is needed. Skipped unless playwright and a Chromium are available
(set PLAYWRIGHT_CHROMIUM to a chrome binary if the bundled one is not installed).
"""
import os, sys
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
pytest.importorskip("fastapi")
sync_api = pytest.importorskip("playwright.sync_api")
from fastapi.testclient import TestClient
from webapp import main
from webapp.models import SessionLocal, init_db, TrackedSpecies, SourceSync, CountyDetection, SpreadSource

init_db()
client = TestClient(main.app)
HOSTILE = '"><img src=x onerror=window.pwned=1>'
ORIGIN = "http://spread.test"


@pytest.fixture
def page():
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM") or None,
                                        args=["--no-sandbox"])
        except Exception as e:  # browser not installed
            pytest.skip(f"no Chromium for playwright: {e}")
        pg = browser.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and errors.append(m.text))

        def handle(route):
            r = client.get(route.request.url[len(ORIGIN):])
            route.fulfill(status=r.status_code, body=r.content,
                          headers={k: v for k, v in r.headers.items() if k.lower() != "content-length"})
        pg.route(ORIGIN + "/**", handle)
        yield pg, errors
        browser.close()


@pytest.fixture
def species():
    db = SessionLocal()
    t = datetime(2026, 11, 1)
    sp = TrackedSpecies(inat_taxon_id=990101, gbif_taxon_key=1, eddmaps_subject_id=2,
                        scientific_name="Domus testus", common_name="DOM bug")
    sp.syncs = [SourceSync(source=s, last_success_at=t, last_attempt_at=t) for s in SpreadSource]
    db.add(sp); db.commit()
    db.add_all([
        CountyDetection(species_id=sp.id, fips="42011", source=SpreadSource.INAT, first_date=date(2015, 11, 7), first_record_id="10912226"),
        CountyDetection(species_id=sp.id, fips="42011", source=SpreadSource.EDDMAPS, first_date=date(2014, 9, 22), first_record_id=HOSTILE),
        CountyDetection(species_id=sp.id, fips="42011", source=SpreadSource.GBIF, first_date=date(2014, 9, 22), first_record_id="1"),
        CountyDetection(species_id=sp.id, fips="42071", source=SpreadSource.INAT, first_date=date(2017, 6, 1), first_record_id="5"),
    ])
    db.commit()
    yield sp.inat_taxon_id
    db.query(CountyDetection).filter_by(species_id=sp.id).delete()
    db.delete(db.get(TrackedSpecies, sp.id)); db.commit(); db.close()


@pytest.fixture
def empty_species():
    """990102: never published. 990103: published, but no eligible records."""
    db = SessionLocal()
    t = datetime(2026, 11, 1)
    a = TrackedSpecies(inat_taxon_id=990102, scientific_name="Never built")
    a.syncs = [SourceSync(source=SpreadSource.INAT)]
    b = TrackedSpecies(inat_taxon_id=990103, scientific_name="Built empty")
    b.syncs = [SourceSync(source=SpreadSource.INAT, last_success_at=t, last_attempt_at=t)]
    db.add_all([a, b]); db.commit()
    yield
    db.delete(a); db.delete(b); db.commit(); db.close()


def click_county(pg, fips):
    pg.evaluate("""f => [...document.querySelectorAll('path.county')].find(p => p.__data__.id === f)
                         .dispatchEvent(new MouseEvent('click', {bubbles: true}))""", fips)


def detail_items(pg):
    return pg.eval_on_selector_all("#detail li", "els => els.map(e => e.textContent)")


def test_page_logic(page, species):
    pg, errors = page
    pg.goto(f"{ORIGIN}/spread/{species}")
    pg.wait_for_selector("path.county")
    assert pg.inner_text("#stCum") == "2"

    click_county(pg, "42011")
    assert "2014-09-22 (EDDMapS, GBIF specimens)" in pg.inner_text("#detail")    # tie credits both
    items = detail_items(pg)
    assert "(later record)" in items[0] and items[0].startswith("iNaturalist")
    assert "(earliest)" in items[1] and "(earliest)" in items[2]

    href = pg.eval_on_selector_all("#detail a", "as => as.map(a => a.href)")[1]
    assert href.startswith("https://www.eddmaps.org/distribution/point.cfm?id=%22%3E%3Cimg")
    assert pg.evaluate("window.pwned") is None and pg.query_selector("img[src=x]") is None

    pg.locator("#sources input").nth(1).uncheck()                                # EDDMapS off
    items = detail_items(pg)
    assert "(source not selected)" in items[1] and "(earliest)" in items[2]

    pg.eval_on_selector("#year", "e => { e.value = 2014; e.dispatchEvent(new Event('input')); }")
    assert "after 2014" in detail_items(pg)[0]
    assert pg.inner_text("#stCum") == "1" and pg.inner_text("#stNew") == "1"

    for i in range(3):
        pg.locator("#sources input").nth(i).uncheck()
    assert pg.inner_text("#notice") == "Select at least one source."
    assert pg.inner_text("#stCum") == "–" and pg.is_disabled("#play")
    assert errors == []    # includes CSP violations reported to the console


def hover_text(pg, fips):
    pg.evaluate("""f => [...document.querySelectorAll('path.county')].find(p => p.__data__.id === f)
                         .dispatchEvent(new PointerEvent('pointermove', {bubbles: true, clientX: 5, clientY: 5}))""", fips)
    return pg.inner_text("#tip")


def test_empty_states(page, empty_species):
    pg, errors = page
    pg.goto(f"{ORIGIN}/spread/990102"); pg.wait_for_selector("path.county")
    assert pg.inner_text("#notice") == "No data has been published for this species yet."
    assert pg.inner_text("#stCum") == "–" and pg.inner_text("#yrlabel") == "–"
    assert hover_text(pg, "42011") == "Berks County, PA"          # no bogus "no record by 50"
    assert pg.is_disabled("#play") and pg.is_disabled("#year")

    pg.goto(f"{ORIGIN}/spread/990103"); pg.wait_for_selector("path.county")
    assert pg.inner_text("#notice") == "The selected sources have no eligible records in US counties."
    assert pg.inner_text("#stCum") == "0" and pg.inner_text("#stSt") == "0"
    assert errors == []
