"""
loop.py — the pieces of the animated futurecast, drawn by the browser.

A static map is a finished picture. The loop is the opposite: the server hands
over ingredients and the viewer's graphics card does the drawing, sixty times a
second, blending between forecast times so the weather moves instead of
stepping. Three kinds of ingredient, all in one region's map frame (the same
frame the static maps use, so everything registers):

  base   the land, water and shaded terrain, as a picture         (once a region)
  top    roads, borders, counties and place names, transparent    (once a region)
  frame  the weather at one forecast time, as data in a PNG:
         red = rain, green = wintry mix, blue = snow, each the echo strength
         (0..75 dBZ on 0..255) where that type owns the spot      (one per time)

  flow   how the weather moves between one time and the next, as a
         small field of displacements                            (one per pair)

Frames are small (about 960 x 500), smoothed here, and coloured by the shader,
so the palette and the blend can change without redrawing anything.

The flow is what makes it a loop rather than a slideshow. Fading from one time
to the next makes every band dim where it was and brighten where it will be,
which reads as twinkling. With the flow, the shader slides the earlier picture
forward and the later one back along the motion and blends those, so a band
travels.
"""

import io
import math
import os
from datetime import datetime, timezone, timedelta

import numpy as np

import hrrr_render as hr

REV = "L1"


def dims(region):
    """
    (base width, base height, frame width, frame height) in pixels. Landscape:
    the map area of a 1920 x 1080 video, under its title strip. A region asked
    for as 'name@tall' is framed upright, 1080 x 1920, the whole of a reel.
    Data frames are half the base size either way.
    """
    return (1080, 1920, 540, 960) if hr.region_parts(region)[1] else (1920, 1005, 960, 502)


# model -> minutes between frames, and how far the loop runs. HRRR posts
# 15-minute fields in its sub-hourly files, which is what makes its loop smooth;
# the others are blended across their own, longer steps.
LOOPS = {
    "hrrr":  dict(step=15,  to=18 * 60,  label="HRRR, next 18 hours"),
    "nam":   dict(step=60,  to=60 * 60,  label="NAM 3 km, next 60 hours"),
    "gfs":   dict(step=60,  to=72 * 60,  label="GFS, next 3 days"),
    "ecmwf": dict(step=180, to=120 * 60, label="ECMWF, next 5 days"),
}


def _path(*parts):
    return os.path.join(hr.CACHE_DIR, "loop_%s_%s.png" % (REV, "_".join(str(p) for p in parts)))


# ------------------------------------------------------------------- runs ---
def latest_run(model):
    """
    The newest run whose whole loop is posted. HRRR's ordinary "latest run"
    counts from hour 0, so for the loop it is walked back until the last
    sub-hourly file is there too.
    """
    run = hr.latest_available_run(model)
    if model != "hrrr":
        return run
    for back in range(0, 5):
        r = run - timedelta(hours=back)
        if hr._exists(hr._index_url(hr._grib_url("hrrr", "subh", r, 18))):
            return r
    return run - timedelta(hours=1)


def minutes(model):
    L = LOOPS[model]
    return list(range(0, L["to"] + 1, L["step"]))


# ----------------------------------------------------------------- weather ---
def _fields(model, run_dt, m):
    """(dbz, kind, t2m, lat, lon) on the model grid at m minutes into the run."""
    if model == "hrrr":
        url = hr._grib_url("hrrr", "subh", run_dt, int(math.ceil(m / 60.0)))
        when = "anl:" if m == 0 else "%d min fcst:" % m
        one = lambda var: hr._one(model, url, ":%s:%s" % (var, when))
        arrs, lat, lon = one("REFC:entire atmosphere")
        dbz = arrs[0]
        kind = np.zeros_like(dbz)
        for code, var in enumerate(("CRAIN", "CFRZR", "CICEP", "CSNOW"), start=1):
            kind = np.where(one(var + ":surface")[0][0] > 0.5, float(code), kind)
        t2m = one("TMP:2 m above ground")[0][0]
        return dbz, kind, t2m, lat, lon
    fxx = m // 60
    url = hr._grib_url(model, "sfc", run_dt, fxx)
    if model == "ecmwf":
        rate = hr._one(model, url, ":sfc:tprate:")[0][0] * 3600.0
        dbz = 10.0 * np.log10(200.0 * np.maximum(rate, 1e-4) ** 1.6)
    else:
        dbz = hr._one(model, url, ":REFC:")[0][0]
    kind, lat, lon = hr._fetch(hr.spec("ptype", model), model, run_dt, fxx)
    t2m = hr._one(model, url, hr.spec("t2m", model)["search"])[0][0]
    return dbz, kind, t2m, lat, lon


# ---------------------------------------------------------------- regrid ---
_INDEX = {}             # (model, region) -> (i, j) fractional grid indices per frame pixel


@hr._locked
def _index(model, region, lat, lon):
    """
    Where each pixel of the frame falls on the model's grid, as fractional row
    and column, so a field becomes a picture with one bilinear lookup. Worked
    out once per model and region: the grid is regular in its own projection
    (Lambert for HRRR and NAM, plain latitude and longitude for GFS and ECMWF),
    so its spacing is measured from the grid itself, not assumed.
    """
    key = (model, region)
    if key in _INDEX:
        return _INDEX[key]
    import cartopy.crs as ccrs
    proj, x0, x1, y0, y1 = hr.frame(region)
    _, _, FRAME_W, FRAME_H = dims(region)
    xs = x0 + (np.arange(FRAME_W) + 0.5) * (x1 - x0) / FRAME_W
    ys = y1 - (np.arange(FRAME_H) + 0.5) * (y1 - y0) / FRAME_H
    X, Y = np.meshgrid(xs, ys)
    pc = ccrs.PlateCarree()
    ll = pc.transform_points(proj, X, Y)
    plon, plat = ll[..., 0], ll[..., 1]
    if model in ("gfs", "ecmwf"):
        i = (plat - lat[0, 0]) / (lat[-1, 0] - lat[0, 0]) * (lat.shape[0] - 1)
        j = (plon - lon[0, 0]) / (lon[0, -1] - lon[0, 0]) * (lon.shape[1] - 1)
    else:
        grid = ccrs.LambertConformal(
            central_longitude=-97.5, central_latitude=38.5, standard_parallels=(38.5, 38.5),
            globe=ccrs.Globe(semimajor_axis=6371229, semiminor_axis=6371229))
        corner = grid.transform_points(pc, np.array([lon[0, 0], lon[0, -1], lon[-1, 0]], dtype="float64"),
                                       np.array([lat[0, 0], lat[0, -1], lat[-1, 0]], dtype="float64"))
        dx = (corner[1, 0] - corner[0, 0]) / (lon.shape[1] - 1)
        dy = (corner[2, 1] - corner[0, 1]) / (lat.shape[0] - 1)
        g = grid.transform_points(pc, plon, plat)
        i, j = (g[..., 1] - corner[0, 1]) / dy, (g[..., 0] - corner[0, 0]) / dx
    _INDEX[key] = (i.astype("float32"), j.astype("float32"))
    return _INDEX[key]


def _sample(a, i, j):
    """Bilinear lookup of grid `a` at fractional (i, j); zero off the grid."""
    n, m = a.shape
    ok = (i >= 0) & (i <= n - 1) & (j >= 0) & (j <= m - 1)
    i0 = np.clip(np.floor(i).astype(int), 0, n - 2)
    j0 = np.clip(np.floor(j).astype(int), 0, m - 2)
    fi, fj = np.clip(i - i0, 0, 1), np.clip(j - j0, 0, 1)
    out = (a[i0, j0] * (1 - fi) * (1 - fj) + a[i0 + 1, j0] * fi * (1 - fj)
           + a[i0, j0 + 1] * (1 - fi) * fj + a[i0 + 1, j0 + 1] * fi * fj)
    return np.where(ok, out, 0.0)


def frames(model, run_dt, m, regions, flows=True):
    """
    Draw the data frame at m minutes for each region, skipping any on disk.
    One call per time covers every region, because the six downloads behind a
    frame are the cost and they are the same for all of them.
    """
    todo = [r for r in regions if not os.path.exists(frame_path(model, run_dt, m, r))]
    if not todo:
        return _pair_up(model, run_dt, m, regions) if flows else None
    from PIL import Image
    dbz, kind, t2m, lat, lon = _fields(model, run_dt, m)
    dbz = np.where(np.isfinite(dbz), np.clip(dbz, 0, 75), 0.0)
    dbz = np.where(dbz >= 5, dbz, 0.0)
    group = np.where(kind == 4, 2, np.where((kind == 2) | (kind == 3), 1, 0))
    group = np.where((kind == 0) & (t2m < 273.65), 2, group)
    chans = [np.where(group == g, dbz, 0.0).astype("float32") for g in range(3)]
    for region in todo:
        i, j = _index(model, region, lat, lon)
        # Coarse grids are stretched a long way to fill a frame; smoothing after
        # the stretch rounds off the facets a bilinear lookup leaves behind.
        cell = abs(float(i[0, 0] - i[-1, 0])) / i.shape[0]        # grid cells per pixel
        passes = 2 if cell > 0.5 else int(min(10, round(1.5 / max(cell, 0.05))))
        rgb = np.stack([hr._smooth(_sample(c, i, j), passes) for c in chans], axis=-1)
        img = Image.fromarray(np.clip(rgb / 75.0 * 255.0, 0, 255).astype("uint8"), "RGB")
        path = frame_path(model, run_dt, m, region)
        tmp = "%s.%d.part" % (path, os.getpid())
        img.save(tmp, format="PNG", optimize=True)
        os.replace(tmp, path)
    if flows:
        _pair_up(model, run_dt, m, regions)


def _pair_up(model, run_dt, m, regions):
    """Work out the flow to and from a frame's neighbours wherever both ends exist."""
    step = LOOPS[model]["step"]
    for region in regions:
        for a in (m - step, m):
            if a >= 0 and a + step <= LOOPS[model]["to"] \
                    and os.path.exists(frame_path(model, run_dt, a, region)) \
                    and os.path.exists(frame_path(model, run_dt, a + step, region)) \
                    and not os.path.exists(flow_path(model, run_dt, a, region)):
                try:
                    flow_png(model, run_dt, a, region)
                except Exception:
                    pass


# ------------------------------------------------------------------- flow ---
FLOW_REV = "F1"
_DS, _WIN, _STRIDE = 4, 5, 4        # work at 1/4 size; 11 x 11 windows every 4th cell


@hr._locked
def flow_scale(model, region):
    """
    The largest displacement, in frame pixels, the flow of this model and
    region can express: weather moving 40 m/s for one step, capped where the
    search would stop being a search. It is also the scale the PNG is coded in.
    """
    _, x0, x1, _, _ = hr.frame(region)
    px = (x1 - x0) / dims(region)[2]                          # metres per frame pixel
    return float(min(96.0, max(8.0, 40.0 * LOOPS[model]["step"] * 60.0 / px)))


def _boxsum(a, r):
    """Sum over the (2r+1) square window round every cell, edges padded."""
    p = np.pad(a, ((r + 1, r), (r + 1, r)), mode="edge").cumsum(0).cumsum(1)
    k = 2 * r + 1
    return p[k:, k:] - p[:-k, k:] - p[k:, :-k] + p[:-k, :-k]


def _flow(a, b, maxd):
    """
    Displacement field carrying picture `a` onto picture `b`, by block matching:
    for each window, the shift of `b` that best matches `a`, with a small price
    on distance so featureless ground does not wander. Windows holding no
    weather take their motion from neighbours that do. Returns (rows, cols, 2)
    as (dx, dy) in full-frame pixels.
    """
    h, w = (a.shape[0] // _DS) * _DS, (a.shape[1] // _DS) * _DS
    A = a[:h, :w].reshape(h // _DS, _DS, w // _DS, _DS).mean((1, 3))
    B = b[:h, :w].reshape(h // _DS, _DS, w // _DS, _DS).mean((1, 3))
    R = int(math.ceil(maxd / _DS))
    Bp = np.pad(B, R, mode="constant")
    gy, gx = np.arange(_STRIDE // 2, A.shape[0], _STRIDE), np.arange(_STRIDE // 2, A.shape[1], _STRIDE)
    best = np.full((gy.size, gx.size), np.inf, dtype="float32")
    dxy = np.zeros((gy.size, gx.size, 2), dtype="float32")
    area = float((2 * _WIN + 1) ** 2)
    for sy in range(-R, R + 1):
        for sx in range(-R, R + 1):
            shifted = Bp[R + sy:R + sy + A.shape[0], R + sx:R + sx + A.shape[1]]
            cost = _boxsum(np.abs(A - shifted), _WIN)[np.ix_(gy, gx)] / area + 0.0015 * math.hypot(sx, sy)
            if sx == 0 and sy == 0:
                still = cost
            better = cost < best
            best = np.where(better, cost, best)
            dxy[better] = (sx, sy)
    # a shift has to earn its place: where moving the window barely beats
    # leaving it alone, the weather there is changing, not travelling
    dxy[best > 0.8 * still] = 0.0
    # Trust a window by how much weather is in it, then let motion spread into
    # the empty ones. Double precision: the running sums behind _boxsum leave
    # float32 residue where the true sum is zero, and dividing by that residue
    # once produced displacements of millions of pixels.
    weight = (_boxsum(A + B, _WIN)[np.ix_(gy, gx)] / area > 0.02).astype("float64")
    dxy = dxy.astype("float64")
    for _ in range(6):
        wsum = _boxsum(weight, 1)
        known = wsum > 1e-3
        dxy = np.stack([np.where(known, _boxsum(dxy[..., k] * weight, 1) / np.where(known, wsum, 1.0), 0.0)
                        for k in (0, 1)], axis=-1)
        weight = np.where(known, np.minimum(1.0, wsum / 9.0 + weight * 0.5), 0.0)
    return np.clip(dxy * _DS, -maxd, maxd).astype("float32")


def flow_path(model, run_dt, m, region):
    return os.path.join(hr.CACHE_DIR, "loop_%s_flow_%s_%s_%04d_%s.png" % (
        FLOW_REV, model, run_dt.strftime("%Y%m%d%H"), m, region))


def flow_png(model, run_dt, m, region):
    """The flow from the frame at m minutes to the next one. Makes both if need be."""
    step = LOOPS[model]["step"]
    if m not in minutes(model) or m + step not in minutes(model):
        raise ValueError("no pair of frames starts at %d minutes" % m)
    path = flow_path(model, run_dt, m, region)
    if os.path.exists(path):
        return path
    from PIL import Image
    frames(model, run_dt, m, [region], flows=False)
    frames(model, run_dt, m + step, [region], flows=False)
    load = lambda mm: np.asarray(Image.open(frame_path(model, run_dt, mm, region)), dtype="float32").max(-1) / 255.0
    maxd = flow_scale(model, region)
    d = np.clip(_flow(load(m), load(m + step), maxd) / maxd, -1, 1)
    rgb = np.zeros(d.shape[:2] + (3,), dtype="uint8")
    rgb[..., :2] = np.round(128 + d * 127).astype("uint8")
    tmp = "%s.%d.part" % (path, os.getpid())
    Image.fromarray(rgb, "RGB").save(tmp, format="PNG")
    os.replace(tmp, path)
    return path


def frame_path(model, run_dt, m, region):
    return _path(model, run_dt.strftime("%Y%m%d%H"), "%04d" % m, region)


def frame_png(model, run_dt, m, region):
    if m not in minutes(model):
        raise ValueError("%s frames are every %d minutes out to %d hours" % (
            hr.MODELS[model]["label"], LOOPS[model]["step"], LOOPS[model]["to"] // 60))
    frames(model, run_dt, m, [region])
    return frame_path(model, run_dt, m, region)


# ------------------------------------------------------------ base layers ---
@hr._locked
def base_png(region, layer):
    """
    'base': land, water and shaded terrain. 'top': roads, borders, counties and
    place names on a transparent sheet. The weather goes between the two.
    """
    if layer not in ("base", "top"):
        raise ValueError("layer is base or top")
    name, _ = hr.region_parts(region)
    BASE_W, BASE_H, _, _ = dims(region)
    path = os.path.join(hr.CACHE_DIR, "geo", "loop_%s_%s_%s_%s.png" % (
        layer, region, REV, hr.BASE_REV))
    if os.path.exists(path):
        return path
    import matplotlib.pyplot as plt
    import matplotlib.patheffects as pe
    import relief
    proj, x0, x1, y0, y1 = hr.frame(region)
    w, e, _, _ = hr.REGIONS[name]["box"]
    regional, close = (e - w) < 22, (e - w) < 10
    dpi = 100
    fig = plt.figure(figsize=(BASE_W / dpi, BASE_H / dpi), dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1], projection=proj)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_axis_off()
    base = hr._basemap(region, proj, x0, x1, y0, y1)

    def put(name, **kw):
        if base[name]:
            ax.add_geometries(base[name], crs=proj, **kw)

    if layer == "base":
        ax.set_facecolor(hr.WATER)
        ax.patch.set_visible(True)
        put("land", facecolor=hr.LAND, edgecolor="none", zorder=1)
        put("lakes", facecolor=hr.WATER, edgecolor="none", zorder=1)
        try:
            shade = relief.shade(region, proj, x0, x1, y0, y1, BASE_W, BASE_H, hr.CACHE_DIR)
            ax.imshow(relief.overlay(shade, 0.50, 0.75), extent=(x0, x1, y0, y1), origin="upper",
                      interpolation="nearest", zorder=1.5)
            ax.set_xlim(x0, x1)
            ax.set_ylim(y0, y1)
        except Exception:
            pass
    else:
        line = hr.INK
        put("roads", facecolor="none", edgecolor="#5a4632", linewidth=1.0 if close else 0.65,
            alpha=0.5 if close else 0.4, zorder=2.8)
        put("lakes", facecolor="none", edgecolor=line, linewidth=0.65, zorder=3)
        put("coast", facecolor="none", edgecolor=line, linewidth=0.8, zorder=3)
        put("states", facecolor="none", edgecolor=line, linewidth=0.75, zorder=3)
        put("borders", facecolor="none", edgecolor=line, linewidth=1.2, zorder=3)
        put("counties", facecolor="none", edgecolor=line, linewidth=0.4, alpha=0.32, zorder=3)
        halo = [pe.withStroke(linewidth=3.2, foreground="white")]
        cities = hr.city_labels(region, limit=18)
        for name, cx, cy, major in cities:
            ax.plot(cx, cy, "o", ms=5.6 if major else 4.0, color="#111", mec="white", mew=1.0, zorder=5)
            ax.text(cx, cy - (y1 - y0) * 0.013, name, ha="center", va="top",
                    fontsize=16 if major else 12.5, fontweight="semibold" if major else "medium",
                    color="#111" if major else "#2b2b2b", path_effects=halo, zorder=6)
        for name, cx, cy in hr.water_labels(region, cities):
            ax.text(cx, cy, name.upper(), ha="center", va="center", fontsize=12.5, fontweight="medium",
                    color="#4f7391", alpha=0.9, zorder=4,
                    path_effects=[pe.withStroke(linewidth=2.8, foreground="white", alpha=0.7)])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.part" % (path, os.getpid())
    fig.savefig(tmp, format="png", dpi=dpi, transparent=(layer == "top"),
                facecolor=hr.WATER if layer == "base" else "none")
    plt.close(fig)
    os.replace(tmp, path)
    return path


# ------------------------------------------------------------------- meta ---
def meta(model, region):
    """Everything the page needs to start: the run, the frame times, the layers."""
    if model not in LOOPS:
        raise ValueError("no loop for %r" % model)
    name, tall = hr.region_parts(region)
    BASE_W, BASE_H, FRAME_W, FRAME_H = dims(region)
    run = latest_run(model)
    stamp = run.strftime("%Y%m%d%H")
    mins = minutes(model)
    have = {n for n in os.listdir(hr.CACHE_DIR) if n.startswith("loop_")}
    return dict(
        model=model, name=hr.MODELS[model]["name"], credit=hr.credit(model),
        region=region, tall=tall, region_label=hr.REGIONS[name]["label"], tz=hr.REGIONS[name]["tz"],
        run=stamp, run_iso=run.strftime("%Y-%m-%dT%H:00:00Z"),
        step=LOOPS[model]["step"], minutes=mins,
        ready=[m for m in mins if os.path.basename(frame_path(model, run, m, region)) in have],
        width=BASE_W, height=BASE_H, frame_w=FRAME_W, frame_h=FRAME_H,
        flow_scale=flow_scale(model, region),
        models=[dict(key=k, label=v["label"]) for k, v in LOOPS.items()],
        regions=hr.region_catalog(),
        colors=hr.RP_COLORS, levels=hr.RP_LEVELS, groups=hr.RP_GROUPS,
    )
