"""
Fetch eligible invasive-species sightings from iNaturalist, GBIF and EDDMapS and
place each one in a single US county (2020 Census boundaries).

Every fetcher yields (fips, "YYYY-MM-DD", record_id) for records that pass the
source's eligibility rules, and raises on an incomplete or malformed scan so the
caller never publishes partial data. See the design notes in spread_tasks.py.
"""
import email.utils
import gzip
import json
import logging
import math
import time
from datetime import date, datetime, timezone
from typing import Callable, Dict, Iterator, Optional, Tuple

import requests
import shapely
from shapely import affinity
from shapely.geometry import Point, shape
from shapely.strtree import STRtree

from webapp.config import BASE_DIR

logger = logging.getLogger(__name__)

COUNTIES_PATH = BASE_DIR / "data" / "us_counties_2020_500k.geojson.gz"

MIN_RADIUS_M = 500        # absorbs the 1:500k boundary generalization
UNKNOWN_RADIUS_M = 1000   # used when a source reports no coordinate uncertainty
MAX_RADIUS_M = 10_000     # larger uncertainty -> county cannot be trusted

GBIF_URL = "https://api.gbif.org/v1/occurrence/search"
GBIF_PAGE = 300
GBIF_OFFSET_LIMIT = 100_000
EDDMAPS_URL = "https://api.bugwoodcloud.org/v2/occurrence"
EDDMAPS_PAGE = 2000  # server caps pages at 2000 (docs say 3000); its nextpage links use 2000
INAT_PAGE = 200
INAT_MIN_COMPLETENESS = 0.99   # vs the first page's total; the end recount is checked too
MAX_RETRY_WAIT = 600           # s; a longer Retry-After ends the run (previous data is kept)
# Allowed drift between iNat's end-of-scan recount and the records seen (records can gain or
# lose research grade during a long scan). Far below one page.
def INAT_RECOUNT_SLACK(total: int) -> int:
    return min(max(10, total // 1000), INAT_PAGE // 2)


# EDDMapS reported totalrows=32594 for SLF but returned 32593 distinct records (one counted,
# never served). Accept exactly that one-record gap, nothing larger.
EDDMAPS_SLACK = 1
HTTP_TIMEOUT = (10, 60)
USER_AGENT = "inat-diff-invasives/1.0"

Record = Tuple[str, str, str]  # (fips, iso date, source record id)


class ScanIncomplete(Exception):
    """The source returned a page sequence that does not add up; do not publish."""


class ScanOversized(Exception):
    """The scan is too large to finish within one run's limits."""


# --------------------------------------------------------------------------- counties

class CountyIndex:
    """Point-with-uncertainty -> exactly one county FIPS, or None."""

    def __init__(self, path=COUNTIES_PATH):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            gj = json.load(fh)
        self.fips = [f["properties"]["fips"] for f in gj["features"]]
        self.geoms = [shape(f["geometry"]) for f in gj["features"]]
        for g in self.geoms:
            shapely.prepare(g)
        self.tree = STRtree(self.geoms)
        self.valid = set(self.fips)
        self.by_fips = dict(zip(self.fips, self.geoms))
        self.names = {f["properties"]["fips"]: f["properties"]["name"] for f in gj["features"]}

    @staticmethod
    def footprint(lat: float, lng: float, radius_m: float):
        """Ellipse (in degrees) enclosing a circle of radius_m metres around the point.

        The 32-gon is inscribed in its circle (apothem = cos(pi/32) = 0.995), so the
        radius is inflated 1% to keep the polygon outside the true circle.
        """
        radius_m *= 1.01
        circle = Point(lng, lat).buffer(1.0, quad_segs=8)
        return affinity.scale(
            circle,
            xfact=radius_m / (111_320 * max(math.cos(math.radians(lat)), 0.01)),
            yfact=radius_m / 110_540,
            origin=(lng, lat),
        )

    def _footprint_for(self, lat, lng, accuracy_m):
        """Uncertainty footprint for a reported point, or None if the point is unusable."""
        try:
            lat, lng = float(lat), float(lng)
        except (TypeError, ValueError):
            return None
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return None
        if accuracy_m is None:
            radius = UNKNOWN_RADIUS_M  # policy: unknown accuracy treated as 1 km
        else:
            try:
                radius = float(accuracy_m)
            except (TypeError, ValueError):
                return None
            if radius > MAX_RADIUS_M or radius < 0:
                return None
            radius = max(radius, MIN_RADIUS_M)
        return self.footprint(lat, lng, radius)

    def assign(self, lat, lng, accuracy_m) -> Optional[str]:
        fp = self._footprint_for(lat, lng, accuracy_m)
        if fp is None:
            return None
        covering = [i for i in self.tree.query(fp) if self.geoms[i].covers(fp)]
        return self.fips[covering[0]] if len(covering) == 1 else None

    def contradicts(self, fips: str, lat, lng, accuracy_m) -> bool:
        """True if usable coordinates place the record entirely outside county `fips`."""
        fp = self._footprint_for(lat, lng, accuracy_m)
        return fp is not None and not self.by_fips[fips].intersects(fp)


_county_index: Optional[CountyIndex] = None


def county_index() -> CountyIndex:
    """Load the boundaries once per worker process."""
    global _county_index
    if _county_index is None:
        _county_index = CountyIndex()
    return _county_index


# --------------------------------------------------------------------------- helpers

def _iso_day(value) -> Optional[str]:
    """Return YYYY-MM-DD if value starts with a real calendar date, else None."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _mentions_inat(*values) -> bool:
    return any(isinstance(v, str) and "inaturalist" in v.lower() for v in values)


def retry_delay(retry_after, attempt: int) -> Optional[float]:
    """Seconds to wait before retrying, from a Retry-After header (seconds or HTTP-date).

    Falls back to a short backoff when absent/unparseable. Returns None when the server
    asks for longer than MAX_RETRY_WAIT: the caller should give up this run.
    """
    delay = None
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(retry_after)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                delay = (when - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                delay = None
    if delay is None:
        delay = 10.0 * (attempt + 1)
    delay = max(delay, 0.0)
    return None if delay > MAX_RETRY_WAIT else delay


def _get_json(session: requests.Session, url: str, params, attempts: int = 3) -> dict:
    """GET with bounded retries; honors Retry-After on 429/5xx."""
    for attempt in range(attempts):
        try:
            resp = session.get(url, params=params, timeout=HTTP_TIMEOUT)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            retryable = status is None or status == 429 or status >= 500
            if not retryable or attempt == attempts - 1:
                raise
            retry_after = getattr(getattr(e, "response", None), "headers", {}).get("Retry-After")
            delay = retry_delay(retry_after, attempt)
            if delay is None:
                raise
            logger.warning("Retrying %s after %s (%.0fs)", url, e, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


# Longest first, so "city and borough" wins over "borough"; "co" is the label abbreviation.
_COUNTY_KINDS = ((" city and borough", "city and borough"), (" census area", "census area"),
                 (" municipality", "municipality"), (" borough", "borough"), (" parish", "parish"),
                 (" county", "county"), (" co", "county"), (" city", "city"))


def _norm_county(name: str) -> Tuple[str, Optional[str]]:
    """(base name, jurisdiction kind or None): "Berks Co." -> ("berks", "county")."""
    n = " ".join(name.lower().replace(".", "").split())
    kind = None
    for suffix, k in _COUNTY_KINDS:
        if n.endswith(suffix):
            n, kind = n[: -len(suffix)], k
            break
    return n.replace("saint ", "st "), kind


def _county_conflicts(stated, assigned_name: str) -> bool:
    """True if a record's own county label names a different county than its coordinates.

    A stated jurisdiction type must match too ("Baltimore County" is not "Baltimore city").
    """
    if not isinstance(stated, str) or not stated.strip():
        return False
    (s_base, s_kind), (a_base, a_kind) = _norm_county(stated), _norm_county(assigned_name)
    return s_base != a_base or (s_kind is not None and s_kind != a_kind)


# --------------------------------------------------------------------------- iNaturalist

def iter_inat(get_page: Callable[[dict], dict], taxon_id: int, counties: CountyIndex,
              max_requests: int, stats: Dict[str, int],
              ensure_budget: Callable[[int], None] = lambda n: None) -> Iterator[Record]:
    """Research-grade US observations of taxon_id (and descendants), id-ascending.

    get_page(params) performs one paced, retried request to /v2/observations.
    After page 1, a scan needing more than max_requests requests is ScanOversized, and
    ensure_budget(remaining_requests) may raise to defer a scan today's allowance can't cover.
    """
    last_id = 0
    total = None
    seen = 0
    while True:
        params = {
            "taxon_id": taxon_id, "place_id": 1, "quality_grade": "research",
            "order_by": "id", "order": "asc", "id_above": last_id, "per_page": INAT_PAGE,
            "fields": "id,observed_on,location,obscured,positional_accuracy",
        }
        page = get_page(params)
        results = page.get("results")
        if not isinstance(results, list):
            raise ScanIncomplete("iNat page without a results list")
        if total is None:
            total = page.get("total_results")
            if not isinstance(total, int) or total < 0:
                raise ScanIncomplete("iNat first page without total_results")
            # pages incl. the terminal short page, plus the end recount
            needed = total // INAT_PAGE + 2
            if needed > max_requests:
                raise ScanOversized(f"oversized: {needed} iNat requests (limit {max_requests})")
            ensure_budget(needed - 1)
        for obs in results:
            oid = obs.get("id") if isinstance(obs, dict) else None
            if not isinstance(oid, int) or oid <= last_id:
                raise ScanIncomplete(f"iNat ids not strictly increasing at {oid!r}")
            last_id = oid
            seen += 1
            if obs.get("obscured"):
                continue
            day = _iso_day(obs.get("observed_on"))
            loc = obs.get("location")
            if not day or not isinstance(loc, str) or "," not in loc:
                continue
            lat, lng = loc.split(",", 1)
            fips = counties.assign(lat, lng, obs.get("positional_accuracy"))
            if fips:
                yield fips, day, str(oid)
        if len(results) < INAT_PAGE:
            break
    stats["records_seen"] = seen
    if seen == 0 and total > 0:
        raise ScanIncomplete(f"iNat returned no records of {total}")
    if seen < INAT_MIN_COMPLETENESS * total:
        raise ScanIncomplete(f"iNat scan saw {seen} of {total} records")
    # Recount now: a truncated scan (e.g. a spurious short page) falls well short of it.
    recount = get_page({"taxon_id": taxon_id, "place_id": 1, "quality_grade": "research", "per_page": 0})
    end_total = recount.get("total_results")
    if not isinstance(end_total, int):
        raise ScanIncomplete("iNat recount without total_results")
    if (seen == 0 and end_total > 0) or seen < end_total - INAT_RECOUNT_SLACK(end_total):
        raise ScanIncomplete(f"iNat scan saw {seen} records but {end_total} exist now")


# --------------------------------------------------------------------------- GBIF

def iter_gbif(taxon_key: int, counties: CountyIndex, stats: Dict[str, int],
              session: Optional[requests.Session] = None) -> Iterator[Record]:
    """Vouchered US specimens (basisOfRecord PRESERVED_SPECIMEN) from GBIF.

    Specimen-only also excludes the iNaturalist dataset and observation aggregators.
    """
    session = session or _session()
    base = [
        ("taxonKey", taxon_key), ("country", "US"), ("hasCoordinate", "true"),
        ("hasGeospatialIssue", "false"), ("occurrenceStatus", "PRESENT"),
        ("basisOfRecord", "PRESERVED_SPECIMEN"),
        ("limit", GBIF_PAGE),
    ]
    offset, count, seen, ids = 0, None, 0, set()
    while True:
        page = _get_json(session, GBIF_URL, base + [("offset", offset)])
        results = page.get("results")
        if not isinstance(results, list) or not isinstance(page.get("count"), int):
            raise ScanIncomplete("GBIF page without results/count")
        if count is None:
            count = page["count"]
            if count > GBIF_OFFSET_LIMIT:
                raise ScanOversized(f"oversized: {count} GBIF records exceed the offset window")
        elif page["count"] != count:
            raise ScanIncomplete("GBIF count changed during the scan")
        for r in results:
            seen += 1
            if r.get("key") is None or r["key"] in ids:
                raise ScanIncomplete(f"GBIF record key missing or repeated: {r.get('key')!r}")
            ids.add(r["key"])
            if _mentions_inat(r.get("references"), r.get("occurrenceID")):
                continue
            y, m, d = r.get("year"), r.get("month"), r.get("day")
            event = r.get("eventDate")
            if not all(isinstance(v, int) for v in (y, m, d)) or (isinstance(event, str) and "/" in event):
                continue
            try:
                day = date(y, m, d).isoformat()
            except ValueError:
                continue
            fips = counties.assign(r.get("decimalLatitude"), r.get("decimalLongitude"),
                                   r.get("coordinateUncertaintyInMeters"))
            if fips and _county_conflicts(r.get("county"), counties.names[fips]):
                continue  # e.g. specimen labelled Berks Co. but georeferenced to another county
            if fips:
                yield fips, day, str(r["key"])
        if page.get("endOfRecords") is True:
            break
        if not results:
            raise ScanIncomplete("GBIF returned an empty page before endOfRecords")
        offset += len(results)
        time.sleep(0.2)
    stats["records_seen"] = seen
    if seen != count:
        raise ScanIncomplete(f"GBIF scan saw {seen} of {count} records")


# --------------------------------------------------------------------------- EDDMapS

def iter_eddmaps(subject_id: int, counties: CountyIndex, stats: Dict[str, int],
                 session: Optional[requests.Session] = None) -> Iterator[Record]:
    """Reviewed, positive EDDMapS reports with day-precision dates, minus copies of iNat."""
    session = session or _session()
    page_no, total, seen, ids = 1, None, 0, set()
    while True:
        page = _get_json(session, EDDMAPS_URL, {
            "subjectid": subject_id, "paging": "true", "pagesize": EDDMAPS_PAGE, "page": page_no})
        data = page.get("data") if isinstance(page, dict) else None
        # Live API sends "totalrows"; the published schema spells it "totalRows".
        rows = (page.get("totalrows", page.get("totalRows")) if isinstance(page, dict) else None)
        if not isinstance(data, list) or not isinstance(rows, int):
            raise ScanIncomplete("EDDMapS page without data/totalrows")
        if total is None:
            total = rows
        elif rows != total:
            raise ScanIncomplete("EDDMapS totalrows changed during the scan")
        for r in data:
            seen += 1
            oid = r.get("objectid")
            if oid is None or oid in ids:
                raise ScanIncomplete(f"EDDMapS objectid missing or repeated: {oid!r}")
            ids.add(oid)
            if _mentions_inat(r.get("url"), r.get("projectname"), r.get("reporter"),
                              r.get("OriginalRecordID")):
                continue
            if r.get("reviewed") is not True:
                continue
            statuses = (r.get("infestationstatus"), r.get("eradicationstatus"))
            if "Negative" in statuses or "Positive" not in statuses:
                continue  # need an explicit Positive and no Negative
            if r.get("yearaccuracy") is not None:
                continue
            unc_days = r.get("dateuncertaintyindays")
            if unc_days not in (None, 0):
                continue
            day = _iso_day(r.get("observationdate"))
            fips = str(r.get("fipscode") or "").strip().zfill(5)
            if not day or fips not in counties.valid:
                continue
            coords = r.get("coordinates")
            if isinstance(coords, str) and "," in coords:
                lat, lng = coords.split(",", 1)
                if counties.contradicts(fips, lat.strip(), lng.strip(),
                                        r.get("coordinateuncertaintyinmeters")):
                    continue
            yield fips, day, str(oid)
        nextpage = page.get("nextpage")
        if not isinstance(nextpage, str):
            raise ScanIncomplete("EDDMapS page without a nextpage marker")
        if nextpage == "":
            break  # terminal page
        if not data:
            raise ScanIncomplete("EDDMapS returned an empty page that is not the last one")
        page_no += 1
        time.sleep(0.5)
    stats["records_seen"] = seen
    if seen > total or seen < total - EDDMAPS_SLACK or (total > 0 and seen == 0):
        raise ScanIncomplete(f"EDDMapS scan saw {seen} of {total} records")


# --------------------------------------------------------------------------- id lookup (CLI)

def resolve_gbif_key(scientific_name: str, session: Optional[requests.Session] = None) -> Optional[int]:
    """GBIF usageKey for an exact, species-or-lower name match; None otherwise."""
    session = session or _session()
    m = _get_json(session, "https://api.gbif.org/v1/species/match",
                  {"name": scientific_name, "strict": "true"})
    if (m.get("matchType") == "EXACT" and m.get("rank") in ("SPECIES", "SUBSPECIES", "VARIETY", "FORM")
            and isinstance(m.get("usageKey"), int)):
        return m["usageKey"]
    return None


def resolve_eddmaps_subject(scientific_name: str, session: Optional[requests.Session] = None) -> Optional[int]:
    """EDDMapS subject id whose scientific name equals scientific_name exactly (unique); else None."""
    session = session or _session()
    rows = _get_json(session, "https://api.bugwoodcloud.org/v2/subject",
                     {"search": scientific_name, "pagesize": 50, "paging": "false"})
    if isinstance(rows, dict):
        rows = rows.get("data", [])
    target = " ".join(scientific_name.lower().split())
    hits = [r["subjectid"] for r in rows if isinstance(r, dict) and isinstance(r.get("subjectid"), int)
            and " ".join(filter(None, (r.get("namepart1"), r.get("namepart2"), r.get("namepart3")))).lower() == target]
    return hits[0] if len(hits) == 1 else None
