"""
relief.py — shaded terrain for the static maps, made once per region and kept.

The maps draw it twice: firmly on the bare land, and again faintly over the
weather, so a band of snow visibly lies on the Tug Hill or wraps the
Adirondacks instead of floating on a flat sheet.

Elevation comes from the keyless Terrarium tiles on AWS. Their coarse levels
carry void-fill garbage (needles thousands of metres tall), so every mosaic is
spike-filtered before anything is computed from it. The sea floor, which those
tiles also carry, is flattened: this is a weather map, not a chart.
"""

import io
import math
import os
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np

TILES = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
REV = "r1"               # bump to rebuild every region's relief


def _tile(zxy):
    from PIL import Image
    z, x, y = zxy
    req = urllib.request.Request(TILES.format(z=z, x=x, y=y), headers={"User-Agent": "hrrr-portal"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                a = np.asarray(Image.open(io.BytesIO(r.read())).convert("RGB"), dtype="float32")
            return a[..., 0] * 256.0 + a[..., 1] + a[..., 2] / 256.0 - 32768.0
        except Exception:
            if attempt == 2:
                return np.zeros((256, 256), dtype="float32")


def _despike(e, passes=4):
    """Replace a pixel with its 3x3 median where it stands 400 m off that median."""
    n, m = e.shape
    for _ in range(passes):
        p = np.pad(e, 1, mode="edge")
        med = np.median(np.stack([p[i:i + n, j:j + m] for i in range(3) for j in range(3)]), axis=0)
        bad = np.abs(e - med) > 400
        if not bad.any():
            break
        e = np.where(bad, med, e)
    return e


def _blur(a, passes=1):
    n, m = a.shape
    for _ in range(passes):
        p = np.pad(a, 1, mode="edge")
        a = sum(p[i:i + n, j:j + m] for i in range(3) for j in range(3)) / 9.0
    return a


def shade(key, proj, x0, x1, y0, y1, w, h, cache_dir):
    """
    (h, w) float array in [-1, 1] for a map frame: below zero is shadow, above
    is lit, zero is flat ground and water. `proj` is the map's cartopy
    projection and x0..y1 its limits. Built on first use, then read from disk.
    """
    import cartopy.crs as ccrs
    path = os.path.join(cache_dir, "geo", "relief_%s_%dx%d_%s.npy" % (key, w, h, REV))
    if os.path.exists(path):
        return np.load(path).astype("float32")

    # where every pixel of the finished frame sits on the globe
    xs = x0 + (np.arange(w) + 0.5) * (x1 - x0) / w
    ys = y1 - (np.arange(h) + 0.5) * (y1 - y0) / h
    X, Y = np.meshgrid(xs, ys)
    ll = ccrs.PlateCarree().transform_points(proj, X, Y)
    lon, lat = ll[..., 0], np.clip(ll[..., 1], -84, 84)

    # a tile level a little finer than the frame, so nothing is stretched
    res = (x1 - x0) / w                                  # metres per pixel
    z = int(min(10, max(4, math.ceil(math.log2(
        156543.03 * math.cos(math.radians(float(lat.mean()))) / res)))))
    n = 2 ** z
    fx = (lon + 180.0) / 360.0 * n
    fy = (1 - np.arcsinh(np.tan(np.radians(lat))) / math.pi) / 2 * n
    tx0, tx1, ty0, ty1 = int(fx.min()), int(fx.max()), int(fy.min()), int(fy.max())
    want = [(z, x, y) for y in range(ty0, ty1 + 1) for x in range(tx0, tx1 + 1)]
    with ThreadPoolExecutor(8) as ex:
        tiles = list(ex.map(_tile, want))
    cols = tx1 - tx0 + 1
    elev = np.vstack([np.hstack(tiles[r * cols:(r + 1) * cols]) for r in range(ty1 - ty0 + 1)])
    elev = np.maximum(_despike(elev), 0.0)               # heal voids, flatten the sea floor

    # bilinear sample of the mosaic at each frame pixel
    px, py = (fx - tx0) * 256 - 0.5, (fy - ty0) * 256 - 0.5
    i0 = np.clip(np.floor(py).astype(int), 0, elev.shape[0] - 2)
    j0 = np.clip(np.floor(px).astype(int), 0, elev.shape[1] - 2)
    a, b = np.clip(py - i0, 0, 1), np.clip(px - j0, 0, 1)
    g = (elev[i0, j0] * (1 - a) * (1 - b) + elev[i0 + 1, j0] * a * (1 - b)
         + elev[i0, j0 + 1] * (1 - a) * b + elev[i0 + 1, j0 + 1] * a * b)
    g = _blur(g.astype("float32"), 1)

    # Lit from the north-west, the convention that makes hills read as hills.
    # The exaggeration is set from the region's own slopes, so the Rockies do
    # not turn black and the Lake Erie plain is not left blank.
    dy, dx = np.gradient(g)                              # rows run north to south
    steep = np.percentile(np.hypot(dx, dy), 99.5)
    zf = float(np.clip(1.1 / max(steep, 1e-6), 0.0005, 0.6))
    dx, dy = dx * zf, dy * zf
    az, alt = math.radians(315.0), math.radians(45.0)
    lx, ly, lz = math.sin(az) * math.cos(alt), math.cos(az) * math.cos(alt), math.sin(alt)
    # surface normal is (-dz/dx, -dz/dy_north, 1); dy here is southward
    lit = (-dx * lx + dy * ly + lz) / np.sqrt(dx * dx + dy * dy + 1.0)
    s = np.clip((lit - lz) / lz, -1.0, 1.0)
    s[g <= 0.5] = 0.0                                    # sea level stays flat

    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.%d.part" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "wb") as fh:
        np.save(fh, s.astype("float16"))
    os.replace(tmp, path)
    return s.astype("float32")


def overlay(s, dark, light):
    """
    RGBA image for imshow: black where shaded, white where lit, by strength.
    Bytes, not floats: matplotlib spends most of an image's draw time converting
    a float array, and this one is drawn twice on every map.
    """
    rgba = np.zeros(s.shape + (4,), dtype="uint8")
    rgba[..., :3] = np.where(s > 0, 255, 0)[..., None]
    rgba[..., 3] = np.clip(np.where(s < 0, -s * dark, s * light) * 255.0, 0, 255)
    return rgba
