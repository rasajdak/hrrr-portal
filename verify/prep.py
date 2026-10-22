"""Regrid score frames onto the NOHRSC 0.04 deg grid inside the box; cache per-day aggregates."""
import sys, os, glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, rasterio
from datetime import datetime, timedelta, timezone
from scipy.spatial import cKDTree

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
FR = os.environ.get("LE_FRAMES", "frames")
W, E, S, N = -93.5, -72.5, 40.5, 50.5
ds = rasterio.open("snow/20221119.tif")
c0, c1 = int(round((W + 126) / 0.04)), int(round((E + 126) / 0.04))
r0, r1 = int(round((55 - N) / 0.04)), int(round((55 - S) / 0.04))
LON = -126 + (np.arange(c0, c1) + 0.5) * 0.04
LAT = 55 - (np.arange(r0, r1) + 0.5) * 0.04
LON2, LAT2 = np.meshgrid(LON, LAT)

g = np.load(sorted(glob.glob("*/grid_hrrr.npz"))[0])       # the same grid whichever set
hlat, hlon = g["lat"], g["lon"]
k = np.cos(np.radians(45.5))
tree = cKDTree(np.c_[hlon.ravel() * k, hlat.ravel()])
_, IDX = tree.query(np.c_[LON2.ravel() * k, LAT2.ravel()])

if not os.path.exists("land.npy"):
    import hrrr_render as hr
    run = datetime(2022, 11, 18, 12, tzinfo=timezone.utc)
    arrs, lat, lon = hr._one("hrrr", hr._grib_url("hrrr", "sfc", run, 0), ":LAND:surface:")
    inbox = (lon >= W) & (lon <= E) & (lat >= S) & (lat <= N)
    rows, cols = np.where(inbox.any(axis=1))[0], np.where(inbox.any(axis=0))[0]
    land = arrs[0][rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
    assert land.shape == hlat.shape
    np.save("land.npy", land.ravel()[IDX].reshape(LAT2.shape))
LAND = np.load("land.npy") > 0.5


def frame(t):
    p = FR + "/hrrr_%s_f000.npz" % t.strftime("%Y%m%d%H")
    if not os.path.exists(p):
        return None
    z = np.load(p)
    if z["score"].shape != hlat.shape:
        return None
    s = z["score"].ravel()[IDX].reshape(LAT2.shape).astype("float32")
    gr = z["group"].ravel()[IDX].reshape(LAT2.shape)
    s[s == 255] = np.nan
    return s, gr


def snow(day):
    """24 h observed snowfall (inches) ending 12Z on `day`."""
    p = "snow/%s.tif" % day.strftime("%Y%m%d")
    if not os.path.exists(p):
        return None
    a = rasterio.open(p).read(1)[r0:r1, c0:c1].astype("float32")
    a[a < 0] = np.nan
    return a


def window(t0, t1, step):
    """Time mean of the score, of the snow-typed score, and the max, t0..t1 inclusive (trapezoid ends)."""
    ts, t = [], t0
    while t <= t1:
        ts.append(t); t += timedelta(hours=step)
    acc = accs = wsum = 0.0
    mx = None
    for i, t in enumerate(ts):
        f = frame(t)
        if f is None:
            continue
        s, gr = f
        w = 0.5 if i in (0, len(ts) - 1) else 1.0
        acc = acc + w * s; accs = accs + w * s * (gr == 2); wsum += w
        mx = s if mx is None else np.fmax(mx, s)
    if wsum == 0:
        return None
    return acc / wsum, accs / wsum, mx, wsum
