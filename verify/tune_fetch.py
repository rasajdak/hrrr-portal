"""
Store what the score is made from (heated fetch, metres climbed, shear and cap
trim) for a list of frames, so tune.py can try other terrain weightings without
downloading the runs again. Same job file as fetch.py.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone
import numpy as np
from fetch import span

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "parts")
os.makedirs(OUT, exist_ok=True)


def one(job):
    model, run, fxx = job
    path = os.path.join(OUT, "%s_%s_f%03d.npz" % (model, run.strftime("%Y%m%d%H"), fxx))
    if os.path.exists(path):
        return "have", job
    import hrrr_render as hr, lake_effect
    keep = {}
    real = lake_effect.parts
    def spy(*a, **k):
        keep["p"] = real(*a, **k)
        return keep["p"]
    lake_effect.parts = spy
    try:
        typed, lat, lon = hr._lake_effect.__wrapped__(model, run, fxx)
    except Exception as e:
        return "FAIL %r" % e, job
    sl, inbox, fetch, rise, trim = keep["p"]
    np.savez_compressed(path, fetch=fetch.astype("float16"), rise=rise.astype("int16"),
                        trim=np.rint(trim * 200).astype("uint8"), snow=(typed[sl] // 100 == 2))
    return "ok", job


if __name__ == "__main__":
    from multiprocessing import Pool
    D = lambda s: datetime.strptime(s, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    jobs = []
    for line in open(sys.argv[1]):
        p = line.split()
        if p and not p[0].startswith("#"):
            jobs += span(p[0], D(p[1]), D(p[2]), int(p[3]), int(p[4]) if len(p) > 4 else 0)
    jobs = list(dict.fromkeys(jobs))
    t0 = time.time()
    with Pool(8) as pool:
        for k, (st, job) in enumerate(pool.imap_unordered(one, jobs), 1):
            if st.startswith("FAIL") or k % 50 == 0:
                print(k, st, job[1].strftime("%Y%m%d%H"), "%.0fs" % (time.time() - t0), flush=True)
    print("done %.0fs" % (time.time() - t0))
