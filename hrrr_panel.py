"""
hrrr_panel.py — one point, one small JSON object, for a device with a screen.

The portal already samples a point: /api/point answers one field at one forecast
hour. A panel on a desk wants a dozen fields for right now plus a few hours of
trend, which through /api/point is thirty-odd HTTP round trips, each of which
may sit there downloading a GRIB message. So this module answers the whole panel
in one response, and never makes the caller wait for a model download if it can
avoid it.

What it costs. A cold build samples NOW_FIELDS at the current forecast hour and
TREND_FIELDS across TREND_HOURS, which is about 31 GRIB messages at roughly a
megabyte each: a minute or two on a 1-vCPU droplet, once per HRRR run. Every
later request for the same run and point is a file read. Keep the field lists
short; they are the whole cost of this endpoint.

How it avoids blocking. The result is cached on disk per (run, point). When a
new run appears, the first request gets the previous run's answer immediately,
flagged as the older run, while a background thread builds the new one. Only a
genuinely cold cache (a point nobody has asked for) is built inline, and even
then the caller can pass wait=0 to get a 202 instead of waiting.

Herbie, numpy and hrrr_render are imported inside the functions that need them,
so this module still imports on a machine without the GRIB stack: the clock
arithmetic and the cache layout can then be tested on their own.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache", "panel")

# HRRR runs hourly and goes out 18 hours; the panel only ever wants the near end.
MAX_FXX = 18

# Everything the rotation shows as a current reading.
NOW_FIELDS = [
    "t2m", "dpt2m", "rh2m", "wind10m", "gust", "mslp", "vis",
    "tcdc", "ceil", "refc", "apcp", "cape", "smoke_sfc",
]

# Sampled across the next few hours as well, for a trend under the number.
# Three fields is a deliberate ceiling: each one costs TREND_HOURS downloads.
TREND_FIELDS = ["t2m", "apcp", "wind10m"]
TREND_HOURS = 6

# A built panel is good until the next run replaces it. This only bounds how
# long a run's answer may be reused if AWS stops posting.
MAX_AGE = 6 * 3600

_lock = threading.Lock()
_building = set()


# ------------------------------------------------------------------- clock ---
def now_fxx(run_dt, at=None):
    """
    Which forecast hour is 'now' for a given run.

    The run is an init time, not a valid time: the 16Z run's hour 0 is 16Z, so at
    17:40Z the current hour is 2, not 0. Rounding rather than flooring puts the
    panel on the nearest hour the model actually has, which is what a reading
    called 'now' should mean.
    """
    at = at or datetime.now(timezone.utc)
    lead = (at - run_dt).total_seconds() / 3600.0
    fxx = int(round(lead))
    return max(0, min(MAX_FXX, fxx))


def valid_iso(run_dt, fxx):
    return (run_dt + timedelta(hours=fxx)).strftime("%Y-%m-%dT%H:00:00Z")


# ------------------------------------------------------------------- cache ---
def cache_path(run_str, lat, lon):
    """
    One file per run and point, keyed to two decimal places. That is about a
    kilometre here, so it is finer than the 3 km grid rather than aligned to it:
    it stops a caller whose coordinates jitter in the fourth decimal from
    filling the directory, but two points a few hundred metres apart will still
    get two files holding the same numbers. Harmless, and much easier to reason
    about than snapping to the model's own Lambert grid.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, "%s_%.2f_%.2f.json" % (run_str, lat, lon))


def read_cache(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def write_cache(path, data):
    """Write through a temporary file: a reader must never see half a panel."""
    tmp = path + ".tmp.%d" % os.getpid()
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _recent_run_file(lat, lon):
    """The newest panel on disk for this point, whatever run built it."""
    try:
        names = os.listdir(CACHE_DIR)
    except OSError:
        return None
    tail = "_%.2f_%.2f.json" % (lat, lon)
    hits = [n for n in names if n.endswith(tail)]
    if not hits:
        return None
    # The run stamp is YYYYMMDDHH, so lexical order is chronological order.
    return os.path.join(CACHE_DIR, max(hits))


# -------------------------------------------------------------------- build --
def build(run_dt, lat, lon):
    """Sample every field this panel carries. Slow; call it off the request path."""
    import hrrr_render as hr

    run_str = run_dt.strftime("%Y%m%d%H")
    fxx = now_fxx(run_dt)

    now = {"fxx": fxx, "valid": valid_iso(run_dt, fxx)}
    for key in NOW_FIELDS:
        v = hr.sample(key, run_dt, fxx, lat, lon)
        if v is not None:
            now[key] = v

    trend = []
    for h in range(fxx, min(fxx + TREND_HOURS, MAX_FXX) + 1):
        point = {"fxx": h, "valid": valid_iso(run_dt, h)}
        for key in TREND_FIELDS:
            v = hr.sample(key, run_dt, h, lat, lon)
            if v is not None:
                point[key] = v
        # An hour that yielded nothing at all is an hour AWS has not posted yet.
        if len(point) > 2:
            trend.append(point)

    return {
        "run": run_str,
        "run_iso": run_dt.strftime("%Y-%m-%dT%H:00:00Z"),
        "lat": round(lat, 4),
        "lon": round(lon, 4),
        "units": {k: hr.FIELDS[k]["unit"] for k in NOW_FIELDS + TREND_FIELDS
                  if k in hr.FIELDS},
        "now": now,
        "trend": trend,
        "built": int(time.time()),
    }


def _build_into_cache(run_dt, lat, lon):
    path = cache_path(run_dt.strftime("%Y%m%d%H"), lat, lon)
    try:
        write_cache(path, build(run_dt, lat, lon))
    finally:
        with _lock:
            _building.discard(path)


def _start_background(run_dt, lat, lon):
    """
    Kick off one build, and only one. The service runs two gunicorn workers, so
    the in-process set is not the whole story: the marker file is what stops both
    workers downloading the same run at once, and it is allowed to go stale in
    case a worker dies mid-build.
    """
    path = cache_path(run_dt.strftime("%Y%m%d%H"), lat, lon)
    marker = path + ".building"
    with _lock:
        if path in _building:
            return False
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            age = time.time() - os.path.getmtime(marker)
            if age < 600:
                return False
            os.utime(marker, None)      # claim the abandoned build
        _building.add(path)

    def run():
        try:
            _build_into_cache(run_dt, lat, lon)
        finally:
            try:
                os.unlink(marker)
            except OSError:
                pass

    threading.Thread(target=run, daemon=True).start()
    return True


# --------------------------------------------------------------------- get ---
def get(lat, lon, wait=True):
    """
    The panel for a point.

    Returns (payload, http_status). The payload always carries 'run' and 'age'
    so a caller can decide for itself whether an older run is good enough; the
    panel firmware shows it and says which run it is.
    """
    import hrrr_render as hr

    run_dt = hr.latest_available_run()
    run_str = run_dt.strftime("%Y%m%d%H")
    path = cache_path(run_str, lat, lon)

    hit = read_cache(path)
    if hit is not None:
        hit["age"] = int(time.time()) - hit.get("built", 0)
        hit["current"] = True
        # A cached panel keeps the forecast hour it was built for, and "now"
        # moves on without it. Build the 19Z run at 20:05Z and it holds hour 1
        # forever, so by 21:05Z the headline reading is an hour old while the
        # run is still the newest one posted and nothing looks wrong. Normally a
        # new run lands every hour and rebuilds it, but when AWS is late that is
        # exactly when it is not happening. So rebuild on the hour drifting too,
        # not just on a new run, and say which hour these numbers are for.
        want = now_fxx(run_dt)
        have = (hit.get("now") or {}).get("fxx")
        if have is not None and have != want:
            hit["hour_drift"] = want - have
            _start_background(run_dt, lat, lon)
        return hit, 200

    # Nothing for this run. An older run for the same point is worth serving
    # while the new one builds: a two-hour-old forecast beats a spinner.
    older = _recent_run_file(lat, lon)
    prev = read_cache(older) if older else None
    if prev is not None and (time.time() - prev.get("built", 0)) < MAX_AGE:
        _start_background(run_dt, lat, lon)
        prev["age"] = int(time.time()) - prev.get("built", 0)
        prev["current"] = False
        prev["latest_run"] = run_str
        return prev, 200

    if not wait:
        _start_background(run_dt, lat, lon)
        return {"run": run_str, "building": True,
                "error": "no panel cached for this point yet"}, 202

    # Genuinely cold, with nothing older to fall back on: build it here, and
    # write it to the cache so this happens once per run and point rather than
    # once per request. No background build is started, because that would
    # download the same thirty messages a second time.
    data = build(run_dt, lat, lon)
    write_cache(path, data)
    data = dict(data)
    data["age"] = 0
    data["current"] = True
    return data, 200
