"""
One iNaturalist request gate shared by every process on this host (via Redis).

- Worker traffic (reports, spread syncs) waits for a shared slot, so all workers
  together stay at or below one request per INAT_WORKER_INTERVAL seconds.
- Every admitted request increments a per-UTC-day counter.
- Spread syncs also pass a daily cap; past it the request is refused with
  BudgetExhausted, so reports and the website keep the remaining allowance.
- Web requests (autocomplete, subscribe) are only counted, never delayed: making
  an interactive search wait behind a long background scan would break it. Their
  volume is bounded by the per-IP route limits, and the worker interval leaves
  headroom under iNaturalist's ~60 requests/minute guidance.
"""
import logging
import time
from typing import Callable, Optional

import redis

from inat_diff.exceptions import iNatAPIError
from webapp.config import REDIS_URL, INAT_WORKER_INTERVAL, INAT_DAILY_REQUEST_CAP

logger = logging.getLogger(__name__)

SLOT_KEY = "inat:next_slot_ms"
COUNT_KEY = "inat:requests:{day}"

# KEYS[1]=slot key, KEYS[2]=day counter. ARGV[1]=interval ms, ARGV[2]=cap (-1 = none).
# Returns {1, count} admitted; {0, wait_ms} slot busy; {-1, count} over cap.
_ADMIT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local nxt = tonumber(redis.call('GET', KEYS[1]) or '0')
if nxt > now then return {0, nxt - now} end
local count = tonumber(redis.call('GET', KEYS[2]) or '0')
local cap = tonumber(ARGV[2])
if cap >= 0 and count >= cap then return {-1, count} end
local interval = tonumber(ARGV[1])
redis.call('SET', KEYS[1], now + interval, 'PX', interval * 10)
count = redis.call('INCR', KEYS[2])
redis.call('EXPIRE', KEYS[2], 172800)
return {1, count}
"""


class BudgetExhausted(Exception):
    """Today's iNaturalist request allowance for background syncs is used up."""


_client: Optional[redis.Redis] = None


def _redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(REDIS_URL)
    return _client


def _day_key() -> str:
    return COUNT_KEY.format(day=time.strftime("%Y%m%d", time.gmtime()))


def make_worker_pacer(capped: bool = False, max_wait: float = 600.0) -> Callable[[], None]:
    """Return a before_request hook that blocks until this process may call iNat."""
    interval_ms = int(INAT_WORKER_INTERVAL * 1000)
    cap = INAT_DAILY_REQUEST_CAP if capped else -1
    script = _redis().register_script(_ADMIT)

    def pace() -> None:
        deadline = time.monotonic() + max_wait
        while True:
            try:
                status, value = script(keys=[SLOT_KEY, _day_key()], args=[interval_ms, cap])
            except redis.RedisError as e:
                # Surface as an API failure so callers' normal error handling applies.
                raise iNatAPIError(f"iNat request gate unavailable: {e}") from e
            if status == 1:
                return
            if status == -1:
                raise BudgetExhausted(f"daily iNat request cap reached ({value}/{cap})")
            if time.monotonic() > deadline:
                raise iNatAPIError("waited too long for the shared iNat request slot")
            time.sleep(min(int(value), interval_ms) / 1000)

    return pace


def count_web_request() -> None:
    """before_request hook for interactive requests: count, never block or fail."""
    try:
        r = _redis()
        key = _day_key()
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, 172800)
        pipe.execute()
    except redis.RedisError as e:
        logger.warning("Could not count iNat request: %s", e)


def requests_today() -> int:
    value = _redis().get(_day_key())
    return int(value) if value else 0
