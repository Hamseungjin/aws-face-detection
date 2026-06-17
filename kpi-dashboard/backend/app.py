"""KPI dashboard FastAPI app.

/api/kpis returns LIVE data aggregated from AWS (see kpis.collect). A TTL cache
(config.REFRESH_INTERVAL_SECONDS, default 60s) ensures browser polling every ~30s
does NOT trigger a CloudWatch Logs Insights query every time -- the cache + the
1h lookback (config.LOOKBACK_HOURS) are the Logs Insights COST guardrails.

Partial failure: kpis.collect never raises; a failing section comes back as
{"error": "..."} while the rest of the payload is still returned (never a 500)."""
import datetime
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import config
import kpis

app = FastAPI(title="Rekognition Video Analyzer - KPI Dashboard")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

_cache = {"payload": None, "at": 0.0}
_lock = threading.Lock()


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def _fresh(now):
    return _cache["payload"] is not None and (now - _cache["at"]) < config.REFRESH_INTERVAL_SECONDS


def _decorate(payload, cache_hit, stale=False):
    payload["cache_hit"] = cache_hit
    payload["stale"] = stale
    payload["cache_expires_at"] = _iso(_cache["at"] + config.REFRESH_INTERVAL_SECONDS)
    return payload


def _build_kpis():
    now = time.time()
    if _fresh(now):
        return _decorate(dict(_cache["payload"]), cache_hit=True)

    with _lock:
        now = time.time()
        if _fresh(now):  # another thread refreshed while we waited
            return _decorate(dict(_cache["payload"]), cache_hit=True)
        try:
            payload = kpis.collect()
        except Exception as e:  # collect() shouldn't raise, but never 500 the API
            if _cache["payload"] is not None:
                stale = dict(_cache["payload"])
                stale["collect_error"] = str(e)
                return _decorate(stale, cache_hit=True, stale=True)
            payload = {
                "generated_at": _iso(now), "source": "error", "error": str(e),
                "window_hours": config.LOOKBACK_HOURS,
                "refresh_interval_seconds": config.REFRESH_INTERVAL_SECONDS,
                "kpis": {}, "recent_errors": [],
            }
        _cache["payload"] = payload
        _cache["at"] = time.time()
        return _decorate(dict(payload), cache_hit=False)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/kpis")
def get_kpis():
    return _build_kpis()


# Static frontend mounted LAST so the API routes above take precedence.
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
