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
import json
import multiprocessing
import os
import shutil
import socket
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
#         maps a day in the plan, but it has delivered 40 GB in 18 hours, more
#         than the droplet's disk: see CACHE_SHARE below.
#
# A pass run with four or more workers takes the wide plan (or pass --wide).
# In each plan:
#   totals : accumulations, drawn only at the hours a total gets quoted for
#   loops  : fields that move, drawn every N hours out to a limit
#   arrive : the hours an arrival-time map is drawn out to
#   lake   : the lake effect score, drawn only for the regions that have lakes
#   long   : for HRRR, the newest of its 48-hour runs (00, 06, 12, 18Z) past
#            hour 18: totals at `at`, moving fields every `every` hours, the
#            arrival map at `arrive`. Drawn while newer, shorter runs land.
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
        "hrrr":  dict(totals=["apcp_tot", "asnow"],  at=[6, 12, 18], loops=MOVING, every=3, to=18, arrive=[18], lake=3,
                      long=dict(at=[24, 36, 48], every=6, arrive=[48], lake=6)),
        "nam":   dict(totals=["apcp_tot", "snow10"], at=[24, 48, 60], loops=MOVING, every=3, to=60, arrive=[36, 60], lake=6),
        "gfs":   dict(totals=["apcp_tot", "snow10"], at=LONG, loops=MOVING_GLOBAL, every=6, to=120, arrive=[72, 120], lake=12),
        "ecmwf": dict(totals=["apcp_tot", "snow10"], at=LONG, loops=MOVING_GLOBAL, every=6, to=120, arrive=[72, 120], lake=12),
    })

WIDE = dict(
    areas=["wny", "newyork", "northeast", "greatlakes", "conus"],
    loop_areas=["wny", "newyork", "northeast", "greatlakes", "conus",
                "wny@tall", "newyork@tall", "northeast@tall", "greatlakes@tall"],
    # temperature and gust loops: one download a frame, not six
    loop_kinds=["radar", "temp", "gust"],
    plan={
        "hrrr":  dict(totals=["apcp_tot", "asnow"],  at=[6, 12, 18], loops=MOVING, every=1, to=18, arrive=[18], lake=1,
                      long=dict(at=[24, 30, 36, 42, 48], every=1, arrive=[36, 48], lake=3)),
        "nam":   dict(totals=["apcp_tot", "snow10"], at=[12, 24, 36, 48, 60], loops=MOVING, every=3, to=60, arrive=[36, 60], lake=3),
        "gfs":   dict(totals=["apcp_tot", "snow10"], at=LONG, loops=MOVING_GLOBAL, every=6, to=240, arrive=[72, 120], lake=6),
        "ecmwf": dict(totals=["apcp_tot", "snow10"], at=LONG, loops=MOVING_GLOBAL, every=6, to=240, arrive=[72, 120], lake=6),
    })

BUDGET = 8 * 60          # seconds per pass; the timer fires every 10 minutes
# Cached frames older than this are deleted. The set above writes roughly 20 GB
# a day (a temperature map is over 2 MB), and the longest any run stays current
# is ECMWF's 12 hours.
KEEP_PNG = 18 * 3600
# The frames may also fill no more than this share of the disk they sit on, and
# the oldest go first once they do. Age alone let the wide plan fill the
# droplet's 48 GB overnight, which stopped deliveries, drawing and this prune
# all at once. On the drawing Mac's disk the share is never reached.
CACHE_SHARE = 0.55
MIN_FREE_GB = 6          # below this, prune only and draw nothing
PUSH_BATCH = 2000        # maps in one delivery, newest first: a backlog goes over in pieces


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
        # the newest long run, past where the ordinary runs stop
        lp, lrun = p.get("long"), hr.long_run(model, run)
        if lp and lrun is not None:
            end, start = hr.run_max(model, lrun), p["to"] + 1
            hours = [(f, h) for f in p["totals"] for h in lp["at"]]
            hours += [(f, h) for f in p["loops"] for h in range(start, end + 1, lp["every"])]
            hours += [("arrival", h) for h in lp["arrive"]]
            for field, fxx in hours:
                todo = [r for r in AREAS if not os.path.exists(hr.map_path(model, field, lrun, fxx, r))]
                yield model, lrun, field, fxx, todo
            for fxx in range(start - start % lp["lake"] + lp["lake"], end + 1, lp["lake"]):
                todo = [r for r in LAKE_AREAS if not os.path.exists(hr.map_path(model, "lake_effect", lrun, fxx, r))]
                yield model, lrun, "lake_effect", fxx, todo
        # then that model's loop, one group per forecast time and kind
        lrun = loop.latest_run(model)
        for kind in want.get("loop_kinds", ["radar"]):
            for m in loop.minutes(model):
                todo = [r for r in LOOP_AREAS if not os.path.exists(loop.frame_path(model, lrun, m, r, kind))]
                yield model, lrun, "loop" if kind == "radar" else "loop:" + kind, m, todo


def draw(group):
    """
    Draw one group. Returns (maps drawn, the (model, hour) if not posted yet,
    [what was skipped and why], for the status page).
    """
    model, run, field, fxx, regions = group
    drawn, notes = 0, []

    def skip(region, e, label="f%03d" % fxx):
        print("skip %s %s %s %s: %s" % (model, field, label, region, e), flush=True)
        notes.append(dict(model=model, field=field, fxx=fxx, region=region, error=str(e)[:200]))

    if field.startswith("loop"):
        try:
            loop.frames(model, run, fxx, regions, kind=field.partition(":")[2] or "radar")
            return len(regions), None, notes
        except RuntimeError as e:
            return 0, ((model, "loop") if "not posted" in str(e) else None), notes
        except Exception as e:
            skip("", e, "%d min" % fxx)
            return 0, None, notes
    for region in regions:
        try:
            hr.render_map(field, run, fxx, region, model)
            drawn += 1
        except RuntimeError as e:
            if "not posted" in str(e):
                return drawn, (model, fxx), notes
            skip(region, e)
        except Exception as e:
            skip(region, e)
    return drawn, None, notes


# ------------------------------------------------------------------ status ---
# What the last pass did and what this one is doing, for /status. One small
# JSON file in the cache, rewritten every few seconds while a pass runs.
STATUS = os.path.join(hr.CACHE_DIR, "prewarm_status.json")
HISTORY = 72             # passes kept: half a day at one every ten minutes
NOTES = 60               # skips kept from the latest pass


def _status_read():
    try:
        with open(STATUS) as fh:
            return json.load(fh)
    except Exception:
        return {}


class Status(object):
    def __init__(self, want, workers):
        old = _status_read()
        self.s = dict(host=socket.gethostname(), plan="wide" if want is WIDE else "lean",
                      workers=workers, state="starting", started=time.time(), finished=None,
                      drawn=0, groups=0, done=0, current=None, notes=[], note_count=0,
                      history=old.get("history", [])[-HISTORY:])
        self.last = self.said = 0
        self.write(force=True)

    def write(self, force=False):
        if not force and time.time() - self.last < 5:
            return
        self.last = time.time()
        try:
            tmp = STATUS + ".part"
            with open(tmp, "w") as fh:
                json.dump(self.s, fh)
            os.replace(tmp, STATUS)
        except Exception:
            pass

    def set(self, **kw):
        self.s.update(kw)
        self.write(force=True)

    def step(self, group, n, notes):
        self.s["drawn"] += n
        self.s["done"] += 1
        self.s["current"] = dict(model=group[0], field=group[2], fxx=group[3],
                                 run=group[1].strftime("%Y%m%d%H"))
        self.s["note_count"] += len(notes)
        self.s["notes"] = (self.s["notes"] + notes)[-NOTES:]
        self.write()
        # a line in the log about once a minute, so `tail -f` shows a pass moving
        if time.time() - self.said > 60:
            self.said = time.time()
            c = self.s["current"]
            print("drawing: %d of %d jobs, %d maps so far, now %s %s %s" % (
                self.s["done"], self.s["groups"], self.s["drawn"], c["model"], c["field"],
                ("%d min" % c["fxx"]) if c["field"].startswith("loop") else "f%03d" % c["fxx"]), flush=True)

    def end(self, state):
        now = time.time()
        self.s.update(state=state, finished=now, current=None)
        self.s["history"] = (self.s["history"] + [dict(
            t=self.s["started"], secs=round(now - self.s["started"], 1), drawn=self.s["drawn"],
            groups=self.s["groups"], done=self.s["done"], skips=self.s["note_count"], state=state)])[-HISTORY:]
        self.write(force=True)


def prune():
    now, frames = time.time(), []
    for name in os.listdir(hr.CACHE_DIR):
        keep = KEEP_PNG if name.endswith(".png") else 3600 if name.endswith(".part") else None
        if keep:
            path = os.path.join(hr.CACHE_DIR, name)
            try:
                st = os.stat(path)
                if now - st.st_mtime > keep:
                    os.remove(path)
                elif name.endswith(".png"):
                    frames.append((st.st_mtime, st.st_size, path))
            except OSError:
                pass
    # then by size: oldest frames first until what is left fits its share
    over = sum(f[1] for f in frames) - CACHE_SHARE * shutil.disk_usage(hr.CACHE_DIR).total
    for _, size, path in sorted(frames):
        if over <= 0:
            break
        try:
            os.remove(path)
        except OSError:
            pass
        over -= size


def served_elsewhere(url="http://127.0.0.1:8001/"):
    """True when the app on the drawing machine answers down its ssh tunnel."""
    import urllib.request
    try:
        return urllib.request.urlopen(url, timeout=5).status == 200
    except Exception:
        return False


def push(dest, key):
    """
    Send the maps not yet sent to the web server, as a tar stream over ssh.

    The account at the other end can do exactly one thing: its key is tied to
    deploy/mapsync-receive, which unpacks .png files (and arrival-time grids)
    into a staging folder and moves them into the cache whole, so the site never serves half a file.
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
    # and each run's arrival times, the longest window worked out for it: with
    # that grid the server draws any region, shape or shorter window at once,
    # rather than fetching and decoding the run's frames again itself
    longest = {}
    for n in os.listdir(hr.CACHE_DIR):
        if n.startswith("acc_arrnow") and n.endswith(".npz"):
            have.add(n)
        elif n.startswith("acc_arr") and n.endswith(".npz"):
            stem = n.rsplit("_", 1)[0]
            longest[stem] = max(longest.get(stem, ""), n)
    have |= set(longest.values())
    sent = set(open(sent_path).read().split()) if os.path.exists(sent_path) else set()
    def drawn_at(n):
        try:
            return os.path.getmtime(os.path.join(hr.CACHE_DIR, n))
        except OSError:
            return 0
    # newest first and a batch at a time: after an outage the backlog is tens
    # of GB, and sent whole it would fill the server it is catching up
    todo = sorted(have - sent, key=drawn_at, reverse=True)[:PUSH_BATCH]
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
    st = Status(want, workers)
    print("pass started %s" % time.strftime("%H:%M"), flush=True)
    free = shutil.disk_usage(hr.CACHE_DIR).free / 1e9
    if free < MIN_FREE_GB:
        print("only %.1f GB free: drawing nothing this pass" % free)
        st.end("low disk")
        return
    # The web server is the fallback, not a second drawer. While another
    # machine has delivered maps in the last half hour (mapsync-receive touches
    # this file), a lean pass stands down and leaves the one core to serving.
    alive = os.path.join(hr.CACHE_DIR, ".drawn-elsewhere")
    stamps = [alive] + [os.path.join(hr.CACHE_DIR, n) for n in os.listdir(hr.CACHE_DIR)
                        if n.startswith(".in.")]          # a delivery still arriving
    if want is LEAN and any(os.path.exists(p) and time.time() - os.path.getmtime(p) < 1800 for p in stamps):
        print("another machine is drawing: pruned only")
        st.end("stood down: another machine is drawing")
        return
    # The same when the drawing machine serves the site itself (deploy/mac/
    # serve.sh): its maps never come here, but its app answers on this port.
    if want is LEAN and served_elsewhere():
        print("another machine is serving: pruned only")
        st.end("stood down: another machine is serving")
        return
    start, drawn, missing = time.time(), 0, set()
    st.set(state="building region layers")
    # A region's loop layers (land and relief, then roads and names) are slow to
    # build the first time, so they are made here, ahead of anyone asking.
    for region in want["loop_areas"]:
        for layer in ("base", "top"):
            try:
                loop.base_png(region, layer)
            except Exception as e:
                print("skip loop %s layer for %s: %s" % (layer, region, e), flush=True)
    work = [g for g in groups(want) if g[4]]
    st.set(state="drawing", groups=len(work))
    out_of_time = False
    if workers > 1 and work:
        # The first group is drawn here, alone: on a fresh machine it is what
        # fetches the border and county files, and several processes fetching
        # them at once would trip over each other.
        first = work.pop(0)
        n, miss, notes = draw(first)
        st.step(first, n, notes)
        drawn += n
        if miss:
            missing.add(miss)
        pool = multiprocessing.Pool(workers)
        try:
            # imap keeps the order, so each answer is matched to its group
            for g, (n, miss, notes) in zip(work, pool.imap(draw, work)):
                st.step(g, n, notes)
                drawn += n
                if miss:
                    missing.add(miss)
                if time.time() - start > budget:
                    out_of_time = True
                    break
        finally:
            pool.terminate()
            pool.join()
    else:
        for g in work:
            if time.time() - start > budget:
                out_of_time = True
                break
            if (g[0], g[3]) in missing:          # that hour is not posted yet
                st.step(g, 0, [])
                continue
            n, miss, notes = draw(g)
            st.step(g, n, notes)
            drawn += n
            if miss:
                missing.add(miss)
    print("drew %d maps in %.0f s" % (drawn, time.time() - start), flush=True)
    st.end("out of time, the rest next pass" if out_of_time else "done")
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
            lp = g[2].startswith("loop")
            n[lp] += len(want["loop_areas"] if lp else want["areas"])
        print(n[0], "maps and", n[1], "loop frames per full set of runs",
              "(wide plan)" if want is WIDE else "(lean plan)")
    else:
        main(workers=workers, dest=_arg("--push"), key=_arg("--key"),
             prune_only="--prune-only" in sys.argv, budget=int(_arg("--budget", BUDGET)), want=want)
