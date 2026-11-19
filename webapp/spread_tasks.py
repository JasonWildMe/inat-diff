"""
Celery tasks that rebuild county first-detection data for tracked species.

Each run is a complete, streaming rebuild of one (species, source):
  1. claim the source_sync row with a conditional UPDATE (committed before any I/O);
  2. stream the source, keeping only the earliest record per county in memory;
  3. publish in one short transaction that first re-checks ownership of the claim,
     then replaces that (species, source)'s county_detection rows.
A scan that fails, runs out of iNat budget, or loses its claim publishes nothing,
so the previous data stays in place. These tasks run on the dedicated `spread`
queue (one worker, concurrency 1) so they never delay subscriber reports.
"""
import logging
import time
from datetime import date, datetime, timedelta
from typing import Dict, Iterator, Optional, Tuple

from sqlalchemy import or_

from webapp.config import SPREAD_REFRESH_DAYS, SPREAD_MAX_INAT_PAGES, INAT_DAILY_REQUEST_CAP
from webapp.models import (SessionLocal, TrackedSpecies, SourceSync, CountyDetection,
                           SpreadSource, SyncStatus)
from webapp.tasks import celery_app, TASK_TIME_LIMIT, STALE_RUNNING_AFTER
from webapp.inat_pacer import BudgetExhausted, make_worker_pacer, requests_today
from webapp import spread_sources as src
from inat_diff.client import iNatClient
from inat_diff.exceptions import iNatAPIError

logger = logging.getLogger(__name__)

INAT_V2 = "https://api.inaturalist.org/v2"
SPREAD_SOFT_TIME_LIMIT = TASK_TIME_LIMIT - 600


def _record_key(day: str, record_id: str) -> Tuple[str, Tuple[int, int, str]]:
    """Deterministic order for same-day ties: numeric ids numerically, others as text."""
    return (day, (0, int(record_id), "") if record_id.isdigit() else (1, 0, record_id))


def _inat_get_page(client: iNatClient):
    """One /v2/observations request with bounded retries (each attempt is paced)."""
    def get_page(params: dict) -> dict:
        for attempt in range(3):
            try:
                return client.get_observations(**params)
            except iNatAPIError as e:
                status = e.status_code
                if attempt == 2 or (status is not None and status != 429 and status < 500):
                    raise
                delay = src.retry_delay(e.retry_after, attempt)
                if delay is None:
                    raise
                logger.warning("iNat request failed (%s); retrying in %.0fs", e, delay)
                time.sleep(delay)
        raise AssertionError("unreachable")
    return get_page


def _ensure_inat_budget(remaining_requests: int) -> None:
    """Defer now, before spending more, if today's allowance cannot finish this scan."""
    used = requests_today()
    if used + remaining_requests > INAT_DAILY_REQUEST_CAP:
        raise BudgetExhausted(f"scan needs {remaining_requests} more requests; "
                              f"{used}/{INAT_DAILY_REQUEST_CAP} used today")


def _records(source: SpreadSource, source_id: int, stats: Dict[str, int]) -> Iterator[src.Record]:
    counties = src.county_index()
    if source == SpreadSource.INAT:
        client = iNatClient(base_url=INAT_V2, before_request=make_worker_pacer(capped=True))
        return src.iter_inat(_inat_get_page(client), source_id, counties,
                             min(SPREAD_MAX_INAT_PAGES, INAT_DAILY_REQUEST_CAP), stats,
                             ensure_budget=_ensure_inat_budget)
    if source == SpreadSource.GBIF:
        return src.iter_gbif(source_id, counties, stats)
    return src.iter_eddmaps(source_id, counties, stats)


def _release(db, sync_id: int, ours: datetime, failed: bool, error: Optional[str]) -> None:
    """Drop our claim without publishing. No-op if the claim is no longer ours."""
    db.rollback()
    db.query(SourceSync).filter(
        SourceSync.id == sync_id,
        SourceSync.status == SyncStatus.RUNNING,
        SourceSync.claim_started_at == ours,
    ).update({
        SourceSync.status: SyncStatus.FAILED if failed else SyncStatus.IDLE,
        SourceSync.claim_started_at: None,
        SourceSync.error_message: error,
    }, synchronize_session=False)
    db.commit()


@celery_app.task(name="webapp.spread_tasks.sync_species_source",
                 soft_time_limit=SPREAD_SOFT_TIME_LIMIT, time_limit=TASK_TIME_LIMIT)
def sync_species_source(species_id: int, source: str, force: bool = False) -> dict:
    """Rebuild county first detections for one species from one source.

    Skips a source rebuilt within SPREAD_REFRESH_DAYS (e.g. a duplicate queued copy)
    unless force=True (manual `manage.py sync-species`).
    """
    source = SpreadSource(source)
    db = SessionLocal()
    sync_id = ours = None
    try:
        species = db.get(TrackedSpecies, species_id)
        row = db.query(SourceSync).filter_by(species_id=species_id, source=source).one_or_none()
        if species is None or row is None or not species.is_active:
            return {"status": "skipped", "reason": "unknown or inactive species"}
        source_id = species.source_id(source)
        if source_id is None:
            return {"status": "skipped", "reason": f"species not linked to {source.value}"}
        sync_id = row.id

        now = datetime.utcnow()
        fresh_after = now - timedelta(days=SPREAD_REFRESH_DAYS)
        claimed = db.query(SourceSync).filter(
            SourceSync.id == sync_id,
            or_(force, SourceSync.last_success_at.is_(None), SourceSync.last_success_at < fresh_after),
            or_(SourceSync.status != SyncStatus.RUNNING,
                SourceSync.claim_started_at.is_(None),
                SourceSync.claim_started_at < now - STALE_RUNNING_AFTER),
        ).update({SourceSync.status: SyncStatus.RUNNING, SourceSync.claim_started_at: now,
                  SourceSync.last_attempt_at: now}, synchronize_session=False)
        db.commit()
        if not claimed:
            return {"status": "already_running_or_fresh"}
        ours = now

        stats: Dict[str, int] = {}
        winners: Dict[str, tuple] = {}
        for fips, day, record_id in _records(source, source_id, stats):
            key = _record_key(day, record_id)
            if fips not in winners or key < winners[fips][0]:
                winners[fips] = (key, day, record_id)

        # Publish: ownership check and data replacement commit together or not at all.
        owned = db.query(SourceSync).filter(
            SourceSync.id == sync_id,
            SourceSync.status == SyncStatus.RUNNING,
            SourceSync.claim_started_at == ours,
        ).update({
            SourceSync.status: SyncStatus.IDLE, SourceSync.claim_started_at: None,
            SourceSync.last_success_at: datetime.utcnow(), SourceSync.error_message: None,
            SourceSync.records_seen: stats.get("records_seen"), SourceSync.counties: len(winners),
        }, synchronize_session=False)
        if owned != 1:
            db.rollback()
            ours = None
            logger.warning("Lost claim on sync %s before publishing; discarding results", sync_id)
            return {"status": "lost_claim"}
        db.query(CountyDetection).filter(CountyDetection.species_id == species_id,
                                         CountyDetection.source == source).delete(synchronize_session=False)
        db.add_all(CountyDetection(species_id=species_id, fips=fips, source=source,
                                   first_date=date.fromisoformat(day), first_record_id=record_id)
                   for fips, (_, day, record_id) in winners.items())
        db.commit()
        ours = None
        logger.info("Spread sync %s/%s: %s records, %s counties", species_id, source.value,
                    stats.get("records_seen"), len(winners))
        return {"status": "ok", "records_seen": stats.get("records_seen"), "counties": len(winners)}

    except BudgetExhausted as e:
        logger.info("Spread sync %s/%s deferred: %s", species_id, source.value, e)
        if ours is not None:
            _release(db, sync_id, ours, failed=False, error=f"deferred: {e}")
        return {"status": "deferred"}
    except Exception as e:  # includes ScanIncomplete, ScanOversized, SoftTimeLimitExceeded
        logger.exception("Spread sync %s/%s failed", species_id, source.value)
        if ours is not None:
            _release(db, sync_id, ours, failed=True, error=f"{type(e).__name__}: {e}"[:2000])
        return {"status": "failed", "error": str(e)[:500]}
    finally:
        db.close()


@celery_app.task(name="webapp.spread_tasks.dispatch_spread_syncs")
def dispatch_spread_syncs() -> dict:
    """Nightly: queue a rebuild for every (species, source) that is due.

    Due = never succeeded, or last success older than SPREAD_REFRESH_DAYS. An
    oversized scan is retried only once per SPREAD_REFRESH_DAYS. Never-synced rows go
    first. A duplicate queued copy is harmless: the task skips fresh or running rows.
    """
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        refresh_before = now - timedelta(days=SPREAD_REFRESH_DAYS)
        rows = (
            db.query(SourceSync).join(TrackedSpecies)
            .filter(
                TrackedSpecies.is_active.is_(True),
                or_(SourceSync.last_success_at.is_(None), SourceSync.last_success_at < refresh_before),
                or_(SourceSync.error_message.is_(None),
                    ~SourceSync.error_message.like("ScanOversized%"),
                    SourceSync.last_attempt_at.is_(None),
                    SourceSync.last_attempt_at < refresh_before),
            )
            .order_by(SourceSync.last_success_at.isnot(None), SourceSync.last_success_at)
            .all()
        )
        queued = 0
        for row in rows:
            if row.species.source_id(row.source) is None:
                continue
            sync_species_source.delay(row.species_id, row.source.value)
            queued += 1
        logger.info("Queued %s spread syncs", queued)
        return {"queued": queued}
    finally:
        db.close()
