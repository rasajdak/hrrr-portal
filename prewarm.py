"""
prewarm.py — draw the static maps people will ask for before they ask.

Run every few minutes by a systemd timer (deploy/hrrr-prewarm.timer). Each pass
asks every model for its newest posted run and draws whatever in PLAN is not in
the cache yet, so a new run's maps appear on their own and the page opens them
instantly. There is no state file: a map that exists is skipped, so a pass that
runs out of time, or meets an hour that is not posted yet, simply leaves the
rest for the next one.

Edit PLAN to change what is drawn. Every entry costs CPU: models x fields x
regions x frames, a few seconds each.

    python prewarm.py                      one pass, one process (the droplet)
    python prewarm.py --workers 4          several drawing processes at once
    python prewarm.py --push hrrr@HOST     then copy new maps to the web server
    python prewarm.py --prune-only         delete old frames, draw nothing
    python prewarm.py --list               count the maps in the plan

A second machine can do the drawing (deploy/mac/): cache file names are a hash
of model, field, run, hour, region and the render revision, so a map drawn
anywhere from the same commit is the file the web server would have made.
"""

import fcntl
import multiprocessing
import os
import shutil
import subprocess
import sys
import time

import hrrr_render as hr
import loop

# What is drawn ahead. Two plans, because two very different machines run this:
#
#   LEAN  the droplet's one core, as a fallback: about 8,000 maps a day. Drawing
#         every frame there (17,900 a day at 3.4 s a map) was more than it has.
#   WIDE  the drawing Mac (six workers): every HRRR hour, GFS and ECMWF out the
#         full ten days, more total hours, and the Great Lakes. About 19,700
#         maps a day, which is also about what the droplet's disk can hold for
#         the 18 hours frames are kept.
#
# A pass run with four or more workers takes the wide plan (or pass --wide).
# In each plan:
#   totals : accumulations, drawn only at the hours a total gets quoted for
#   loops  : fields that move, drawn every N hours out to a limit
#   arrive : the hours an arrival-time map is drawn out to
#   lake   : the lake effect score, drawn only for the regions that have lakes
# Anything not listed still draws on request.
LONG = [24, 48, 72, 120, 168, 240]
MOVING = ["refc_ptype", "refc", "ptype", "t2m", "gust"]
MOVING_GLOBAL = ["refc_ptype", "ptype", "t2m", "gust"]          # no radar field worth a map
LAKE_AREAS = ["wny", "greatlakes"]

LEAN = dict(
    areas=["wny", "newyork", "northeast", "conus"],
    # the animated loop's data frames. A frame's cost is its six downloads,
    # shared by every region, so regions are cheap here. @tall: upright, for reels
    loop_areas=["wny", "newyork", "northeast", "wny@tall", "northeast@tall"],
    plan={
        "hrrr":  dict(totals=["apcp_tot", "asnow"],  at=[6, 12, 18], loops=MOVING, every=3, to=18, arrive=[18], lake=3),
        "nam":   dict(totals=["apcp_tot", "snow10"], at=[24, 48, 60], loops=MOVING, every=3, to=60, arrive=[36, 60], lake=6),
        "gfs":   dict(totals=["apcp_tot", "snow10"], at=LONG, loops=MOVING_GLOBAL, every=6, to=120, arrive=[72, 120], lake=12),
        "ecmwf": dict(totals=["apcp_tot", "snow10"], at=LONG, loops=MOVING_GLOBAL, every=6, to=120, arrive=[72, 120], lake=12),
    })

WIDE = dict(
    areas=["wny", "newyork", "northeast", "greatlakes", "conus"],
    loop_areas=["wny", "newyork", "northeast", "greatlakes", "conus",
                "wny@tall", "newyork@tall", "northeast@tall", "greatlakes@tall"],
    plan={
        "hrrr":  dict(totals=["apcp_tot", "asnow"],  at=[6, 12, 18], loops=MOVING, every=1, to=18, arrive=[18], lake=1),
        "nam":   dict(totals=["apcp_tot", "snow10"], at=[12, 24, 36, 48, 60], loops=MOVING, every=3, to=60, arrive=[36, 60], lake=3),
        "gfs":   dict(totals=["apcp_tot", "snow10"], at=LONG, loops=MOVING_GLOBAL, every=6, to=240, arrive=[72, 120], lake=6),
        "ecmwf": dict(totals=["apcp_tot", "snow10"], at=LONG, loops=MOVING_GLOBAL, every=6, to=240, arrive=[72, 120], lake=6),
    })

BUDGET = 8 * 60          # seconds per pass; the timer fires every 10 minutes
# Cached frames older than this are deleted. The set above writes roughly 20 GB
# a day (a temperature map is over 2 MB), and the longest any run stays current
# is ECMWF's 12 hours.
KEEP_PNG = 18 * 3600
MIN_FREE_GB = 6          # below this, prune only and draw nothing


def groups(want=LEAN):
    """
    (model, run, field, fxx, [regions]) for every planned frame with a map still
    to draw, newest need first: HRRR, then the rest. One group is one download,
    shared by its regions, so a group is the unit handed to a worker.
    """
    AREAS, LOOP_AREAS = want["areas"], want["loop_areas"]
    for model, p in want["plan"].items():
        run = hr.latest_available_run(model)
        frames = [(f, h) for f in p["totals"] for h in p["at"]]
        frames += [(f, h) for f in p["loops"] for h in range(0, p["to"] + 1, p["every"])]
        for field, fxx in frames:
            if fxx < hr.spec(field, model)["min_fxx"]:
                continue
            todo = [r for r in AREAS if not os.path.exists(hr.map_path(model, field, run, fxx, r))]
            yield model, run, field, fxx, todo
        for fxx in p["arrive"]:
            todo = [r for r in AREAS if not os.path.exists(hr.map_path(model, "arrival", run, fxx, r))]
            yield model, run, "arrival", fxx, todo
        for fxx in range(0, p["to"] + 1, p["lake"]):
            todo = [r for r in LAKE_AREAS if not os.path.exists(hr.map_path(model, "lake_effect", run, fxx, r))]
            yield model, run, "lake_effect", fxx, todo
        # then that model's loop, one group per forecast time
        lrun = loop.latest_run(model)
        for m in loop.minutes(model):
            todo = [r for r in LOOP_AREAS if not os.path.exists(loop.frame_path(model, lrun, m, r))]
            yield model, lrun, "loop", m, todo


def draw(group):
    """Draw one group. Returns (maps drawn, the (model, hour) if not posted yet)."""
    model, run, field, fxx, regions = group
    drawn = 0
    if field == "loop":
        try:
            loop.frames(model, run, fxx, regions)
            return len(regions), None
        except RuntimeError as e:
            return 0, ((model, "loop") if "not posted" in str(e) else None)
        except Exception as e:
            print("skip %s loop %d min: %s" % (model, fxx, e), flush=True)
            return 0, None
    for region in regions:
        try:
            hr.render_map(field, run, fxx, region, model)
            drawn += 1
        except RuntimeError as e:
            if "not posted" in str(e):
                return drawn, (model, fxx)
            print("skip %s %s f%03d %s: %s" % (model, field, fxx, region, e), flush=True)
        except Exception as e:
            print("skip %s %s f%03d %s: %s" % (model, field, fxx, region, e), flush=True)
    return drawn, None


def prune():
    now = time.time()
    for name in os.listdir(hr.CACHE_DIR):
        keep = KEEP_PNG if name.endswith(".png") else 3600 if name.endswith(".part") else None
        if keep:
            path = os.path.join(hr.CACHE_DIR, name)
            try:
                if now - os.path.getmtime(path) > keep:
                    os.remove(path)
            except OSError:
                pass


def push(dest, key):
    """
    Send the maps not yet sent to the web server, as a tar stream over ssh.

    The account at the other end can do exactly one thing: its key is tied to
    deploy/mapsync-receive, which unpacks .png files into a staging folder and
    moves them into the cache whole, so the site never serves half a file.
    (rsync would be the obvious tool, but macOS ships openrsync and the
    restricted-rsync wrapper on the server refuses its handshake.)
    """
    import tarfile
    sent_path = os.path.join(hr.CACHE_DIR, ".pushed")
    have = {n for n in os.listdir(hr.CACHE_DIR) if n.endswith(".png")}
    # and the loop's base and label layers, which live one folder down
    geo = os.path.join(hr.CACHE_DIR, "geo")
    if os.path.isdir(geo):
        have |= {"geo/" + n for n in os.listdir(geo) if n.startswith("loop_") and n.endswith(".png")}
    sent = set(open(sent_path).read().split()) if os.path.exists(sent_path) else set()
    todo = sorted(have - sent)
    if not todo:
        return 0
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "IdentitiesOnly=yes"]
    cmd += (["-i", key] if key else []) + [dest, "receive"]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=p.stdin, mode="w|") as tar:
            for n in todo:
                try:
                    tar.add(os.path.join(hr.CACHE_DIR, n), arcname=n)
                except OSError:
                    pass                          # pruned a moment ago
        p.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    err = p.stderr.read().decode("utf-8", "replace").strip()
    if p.wait():
        print("push failed: %s" % (err.splitlines() or ["ssh exited %d" % p.returncode])[-1], flush=True)
        return -1
    with open(sent_path, "w") as fh:               # forget names that are gone
        fh.write("\n".join(sorted((sent | set(todo)) & have)))
    return len(todo)


def main(workers=1, dest=None, key=None, prune_only=False, budget=BUDGET, want=LEAN):
    lock = open(os.path.join(hr.CACHE_DIR, "prewarm.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return                                   # the last pass is still going
    prune()
    if prune_only:
        return
    free = shutil.disk_usage(hr.CACHE_DIR).free / 1e9
    if free < MIN_FREE_GB:
        print("only %.1f GB free: drawing nothing this pass" % free)
        return
    # The web server is the fallback, not a second drawer. While another
    # machine has delivered maps in the last half hour (mapsync-receive touches
    # this file), a lean pass stands down and leaves the one core to serving.
    alive = os.path.join(hr.CACHE_DIR, ".drawn-elsewhere")
    if want is LEAN and os.path.exists(alive) and time.time() - os.path.getmtime(alive) < 1800:
        print("another machine is drawing: pruned only")
        return
    start, drawn, missing = time.time(), 0, set()
    # A region's loop layers (land and relief, then roads and names) are slow to
    # build the first time, so they are made here, ahead of anyone asking.
    for region in want["loop_areas"]:
        for layer in ("base", "top"):
            try:
                loop.base_png(region, layer)
            except Exception as e:
                print("skip loop %s layer for %s: %s" % (layer, region, e), flush=True)
    work = [g for g in groups(want) if g[4]]
    if workers > 1 and work:
        # The first group is drawn here, alone: on a fresh machine it is what
        # fetches the border and county files, and several processes fetching
        # them at once would trip over each other.
        n, miss = draw(work.pop(0))
        drawn += n
        if miss:
            missing.add(miss)
        pool = multiprocessing.Pool(workers)
        try:
            for n, miss in pool.imap_unordered(draw, work):
                drawn += n
                if miss:
                    missing.add(miss)
                if time.time() - start > budget:
                    break
        finally:
            pool.terminate()
            pool.join()
    else:
        for g in work:
            if time.time() - start > budget:
                break
            if (g[0], g[3]) in missing:          # that hour is not posted yet
                continue
            n, miss = draw(g)
            drawn += n
            if miss:
                missing.add(miss)
    print("drew %d maps in %.0f s" % (drawn, time.time() - start), flush=True)
    if dest:
        n = push(dest, key)
        if n > 0:
            print("sent %d maps to %s" % (n, dest), flush=True)


def _arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


if __name__ == "__main__":
    workers = int(_arg("--workers", "1"))
    want = WIDE if (workers >= 4 or "--wide" in sys.argv) else LEAN
    if "--list" in sys.argv:
        n = [0, 0]
        for g in groups(want):
            n[g[2] == "loop"] += len(want["loop_areas"] if g[2] == "loop" else want["areas"])
        print(n[0], "maps and", n[1], "loop frames per full set of runs",
              "(wide plan)" if want is WIDE else "(lean plan)")
    else:
        main(workers=workers, dest=_arg("--push"), key=_arg("--key"),
             prune_only="--prune-only" in sys.argv, budget=int(_arg("--budget", BUDGET)), want=want)
