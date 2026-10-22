"""Compute and store lake effect score frames from archived model runs."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta, timezone
import numpy as np

# LE_FRAMES names the set, so a retuned score can sit beside the old one
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", os.environ.get("LE_FRAMES", "frames"))
os.makedirs(OUT, exist_ok=True)
BOX = (-93.5, -72.5, 40.5, 50.5)


def name(model, run, fxx):
    return os.path.join(OUT, "%s_%s_f%03d.npz" % (model, run.strftime("%Y%m%d%H"), fxx))


def one(job):
    model, run, fxx = job
    path = name(model, run, fxx)
    if os.path.exists(path):
        return "have", job
    import hrrr_render as hr
    for attempt in range(3):
        try:
            typed, lat, lon = hr._lake_effect.__wrapped__(model, run, fxx)
            break
        except Exception as e:
            err = repr(e)
            time.sleep(2 + 3 * attempt)
    else:
        return "FAIL " + err, job
    w, e, s, n = BOX
    inbox = (lon >= w) & (lon <= e) & (lat >= s) & (lat <= n)
    rows, cols = np.where(inbox.any(axis=1))[0], np.where(inbox.any(axis=0))[0]
    sl = (slice(rows[0], rows[-1] + 1), slice(cols[0], cols[-1] + 1))
    t = typed[sl]
    ok = np.isfinite(t)
    score = np.where(ok, t % 100, 255).astype("uint8")
    group = np.where(ok, t // 100, 255).astype("uint8")
    g = os.path.join(OUT, "grid_%s.npz" % model)
    if not os.path.exists(g):
        np.savez_compressed(g, lat=lat[sl], lon=lon[sl])
    np.savez_compressed(path, score=score, group=group)
    return "ok", job


def span(model, start, end, step_h, fxx=0):
    t, jobs = start, []
    while t <= end:
        jobs.append((model, t - timedelta(hours=fxx), fxx))
        t += timedelta(hours=step_h)
    return jobs


if __name__ == "__main__":
    from multiprocessing import Pool
    D = lambda s: datetime.strptime(s, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    jobs = []
    for line in open(sys.argv[1]):
        p = line.split()
        if not p or p[0].startswith("#"):
            continue
        jobs += span(p[0], D(p[1]), D(p[2]), int(p[3]), int(p[4]) if len(p) > 4 else 0)
    jobs = list(dict.fromkeys(jobs))
    print(len(jobs), "frames", flush=True)
    t0 = time.time()
    with Pool(int(sys.argv[2]) if len(sys.argv) > 2 else 6) as pool:
        for k, (st, job) in enumerate(pool.imap_unordered(one, jobs), 1):
            if st != "ok" and st != "have" or k % 25 == 0:
                print(k, st, job[0], job[1].strftime("%Y%m%d%H"), job[2], "%.0fs" % (time.time() - t0), flush=True)
    print("done %.0fs" % (time.time() - t0))
