"""
prewarm.py — draw the static maps people will ask for before they ask.

Run every few minutes by a systemd timer (deploy/hrrr-prewarm.timer). Each pass
asks every model for its newest posted run and draws whatever in PLAN is not in
the cache yet, so a new run's maps appear on their own and the page opens them
instantly. There is no state file: a map that exists is skipped, so a pass that
runs out of time, or meets an hour that is not posted yet, simply leaves the
rest for the next one.

Edit PLAN to change what is drawn. Every entry costs CPU on a one-core box:
models x fields x regions x frames, a few seconds each.
"""

import fcntl
import os
import sys
import time

import hrrr_render as hr

# model -> (fields, regions). Frames are all of the model's. Kept lean on
# purpose: this set is about 2,600 maps a day. HRRR is the expensive line,
# because it comes round every hour.
PLAN = [
    ("hrrr",  ["refc", "apcp_tot", "asnow"],   ["northeast"]),
    ("nam",   ["apcp_tot", "snow10"],          ["northeast", "wny"]),
    ("gfs",   ["apcp_tot", "snow10"],          ["northeast", "conus"]),
    ("ecmwf", ["apcp_tot", "snow10"],          ["northeast", "conus"]),
]

BUDGET = 8 * 60          # seconds per pass; the timer fires every 10 minutes
KEEP_PNG = 48 * 3600     # cached frames older than this are deleted


def jobs():
    """(model, run, field, fxx, region), newest need first: HRRR, then the rest."""
    for model, fields, regions in PLAN:
        m = hr.MODELS[model]
        run = hr.latest_available_run(model)
        for field in fields:
            first = hr.spec(field, model)["min_fxx"]
            for fxx in range(first, m["max_fxx"] + 1, m["step"]):
                # regions innermost: they share the one download of this frame
                for region in regions:
                    yield model, run, field, fxx, region


def prune():
    now = time.time()
    for name in os.listdir(hr.CACHE_DIR):
        if name.endswith(".png"):
            path = os.path.join(hr.CACHE_DIR, name)
            try:
                if now - os.path.getmtime(path) > KEEP_PNG:
                    os.remove(path)
            except OSError:
                pass


def main():
    lock = open(os.path.join(hr.CACHE_DIR, "prewarm.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return                                   # the last pass is still going
    start, drawn, missing = time.time(), 0, set()
    for model, run, field, fxx, region in jobs():
        if time.time() - start > BUDGET:
            break
        if (model, fxx) in missing:              # that hour is not posted yet
            continue
        path = hr._cache_path("map", model, field, run.strftime("%Y%m%d%H"),
                              "%03d" % fxx, region)
        if os.path.exists(path):
            continue
        try:
            hr.render_map(field, run, fxx, region, model)
            drawn += 1
        except RuntimeError as e:
            if "not posted" in str(e):
                missing.add((model, fxx))
            else:
                print("skip %s %s f%03d %s: %s" % (model, field, fxx, region, e))
        except Exception as e:
            print("skip %s %s f%03d %s: %s" % (model, field, fxx, region, e))
    prune()
    print("drew %d maps in %.0f s" % (drawn, time.time() - start))


if __name__ == "__main__":
    if "--list" in sys.argv:
        n = 0
        for j in jobs():
            n += 1
        print(n, "maps per full set of runs")
    else:
        main()
