"""
Read side of the spread map: which counties are shown, and the per-species payload
rendered into /spread/{taxon} pages. Pure SQLAlchemy reads; no geometry libraries.

One eligibility rule is used everywhere (map payload and index counts): a source's rows
count only if the species is still linked to that source and the source has published at
least once; rows in territories or dated after today are left out.
"""
import hashlib
import logging
from datetime import datetime
from typing import Dict, Optional

from sqlalchemy import func

from webapp.config import BASE_DIR
from webapp.models import TrackedSpecies, CountyDetection, SpreadSource

logger = logging.getLogger(__name__)

# The map (d3.geoAlbersUsa) shows the 50 states and DC. Territories are left out of the
# geometry and of every count, so the map, chart, table and index always agree.
TERRITORY_STATE_FIPS = frozenset({"60", "66", "69", "72", "78"})

STATIC_DIR = BASE_DIR / "static"
_asset_urls: Dict[str, str] = {}


def shown(fips: str) -> bool:
    return fips[:2] not in TERRITORY_STATE_FIPS


def asset_url(path: str) -> str:
    """/static URL with a content hash, so nginx's 7-day caching never serves stale code.

    A missing file gets a plain URL (and is not cached) so the page still renders; the
    browser then shows its "map unavailable" fallback.
    """
    if path not in _asset_urls:
        try:
            digest = hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:10]
        except OSError:
            logger.error("Static asset missing: %s", path)
            return f"/static/{path}"
        _asset_urls[path] = f"/static/{path}?v={digest}"
    return _asset_urls[path]


def source_states(species: TrackedSpecies) -> Dict[str, dict]:
    """Public status of every source. Raw worker errors stay private (admin CLI only).

    ok: published, latest refresh succeeded. stale: published, but a later refresh failed
    or was postponed, so older data is shown. pending/unavailable: never published.
    """
    syncs = {s.source: s for s in species.syncs}
    out = {}
    for source in SpreadSource:
        sync = syncs.get(source)
        if species.source_id(source) is None:
            status, last = "not_linked", None
        elif sync is None or sync.last_success_at is None:
            status = "pending" if sync is None or sync.last_attempt_at is None else "unavailable"
            last = None
        else:
            status = "stale" if sync.error_message else "ok"
            last = sync.last_success_at.date().isoformat()
        out[source.value] = {"status": status, "last_success": last}
    return out


def _published(states: Dict[str, dict]) -> list:
    return [SpreadSource(k) for k, st in states.items() if st["status"] in ("ok", "stale")]


def _eligible_rows(db, species_id: int, sources: list):
    today = datetime.utcnow().date()
    return db.query(CountyDetection).filter(
        CountyDetection.species_id == species_id,
        CountyDetection.source.in_(sources),
        CountyDetection.first_date <= today,
        ~func.substr(CountyDetection.fips, 1, 2).in_(TERRITORY_STATE_FIPS),
    )


def species_payload(db, species: TrackedSpecies) -> dict:
    """Everything the map page needs for one species (a few hundred rows)."""
    states = source_states(species)
    counties: dict = {}
    for row in _eligible_rows(db, species.id, _published(states)):
        counties.setdefault(row.fips, {})[row.source.value] = [row.first_date.isoformat(), row.first_record_id]
    return {
        "species": {"inat_taxon_id": species.inat_taxon_id,
                    "scientific_name": species.scientific_name,
                    "common_name": species.common_name},
        "year": datetime.utcnow().year,
        "sources": states,
        "counties": counties,
    }


def species_list(db) -> list:
    """Active species with their county count (None if no source has published yet)."""
    out = []
    for sp in db.query(TrackedSpecies).filter(TrackedSpecies.is_active.is_(True)) \
                 .order_by(TrackedSpecies.common_name, TrackedSpecies.scientific_name):
        published = _published(source_states(sp))
        count = (_eligible_rows(db, sp.id, published).with_entities(CountyDetection.fips)
                 .distinct().count()) if published else None
        out.append({"inat_taxon_id": sp.inat_taxon_id, "scientific_name": sp.scientific_name,
                    "common_name": sp.common_name, "counties": count})
    return out


def active_species(db, inat_taxon_id: int) -> Optional[TrackedSpecies]:
    return db.query(TrackedSpecies).filter_by(inat_taxon_id=inat_taxon_id, is_active=True).one_or_none()
