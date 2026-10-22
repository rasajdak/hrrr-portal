"""
Lake effect potential, 0 to 100, from a model's ingredients rather than from
its precipitation. A global model cannot draw a snow band ten miles wide, but
it does know how cold the air is, how warm the lake is and which way the wind
blows down it, and those say where a band can form days before a 3 km model
reaches that far.

For every grid cell the air is followed back upwind along the low-level wind,
and the open water it crossed is added up, each kilometre weighted by how much
warmer the lake was than the air at 850 mb (nothing under 10 C of difference,
full weight from 22 C; 13 C is the textbook threshold). Water crossed long ago
counts for less: what a lake gives the air fades over the land in between,
faster in light wind. That "heated fetch" sets the score, trimmed where the
wind turns with height (bands shear apart) and where warm air aloft caps the
clouds low.

Ground that rises downwind of a lake wrings more out of the same air: the Tug
Hill takes the most snow east of Lake Ontario, not the beach. So the heated
fetch counts for more where the air has climbed since the low ground it last
crossed (lake_terrain.npz, built by verify/build_terrain.py).

numpy only, and no model knowledge: hrrr_render hands it arrays. verify/ holds
the check against observed snowfall that these numbers were set by.
"""

import os

import numpy as np

# Where lake effect is looked for: the Great Lakes and the smaller lakes round
# them (Champlain, Simcoe, Nipigon, the Finger Lakes). W, E, S, N.
BOX = (-93.5, -72.5, 40.5, 50.5)
# ...less the corner of it that is the Atlantic off New York
OCEAN = (-74.6, 41.7)             # east of this and south of this is salt water

KM_PER_DEG = 111.19
STEP_KM = 6.0                     # one stride back along the wind
REACH_KM = 500.0                  # how far upwind to look
DT_NONE, DT_FULL = 10.0, 22.0     # lake minus 850 mb, C: no weight .. full weight
FETCH_KM = 240.0                  # heated fetch at which the score is 63 (170 before the terrain gain)
INLAND_KM = 110.0                 # e-folding distance over land in a 10 m/s wind
RISE_KM = 60.0                    # how far upwind the low ground is looked for
RISE_FULL = 250.0                 # m climbed above it for the whole terrain gain
RISE_GAIN = 1.5                   # heated fetch counts (1 + this) times there

_TERRAIN = None


def terrain(lat, lon):
    """Ground height in m at each lat, lon (bilinear from lake_terrain.npz); 0 outside it."""
    global _TERRAIN
    if _TERRAIN is None:
        t = np.load(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lake_terrain.npz"))
        _TERRAIN = (float(t["lon0"]), float(t["lat0"]), float(t["step"]), t["z"].astype("float32"))
    lon0, lat0, step, z = _TERRAIN
    x, y = (lon - lon0) / step, (lat - lat0) / step
    off = (x < 0) | (y < 0) | (x > z.shape[1] - 1) | (y > z.shape[0] - 1)
    x, y = np.clip(x, 0, z.shape[1] - 1.001), np.clip(y, 0, z.shape[0] - 1.001)
    i, j = x.astype(int), y.astype(int)
    fx, fy = x - i, y - j
    out = (z[j, i] * (1 - fx) * (1 - fy) + z[j, i + 1] * fx * (1 - fy)
           + z[j + 1, i] * (1 - fx) * fy + z[j + 1, i + 1] * fx * fy)
    return np.where(off, 0.0, out)


def _ramp(x, x0, x1, y0, y1):
    return y0 + (y1 - y0) * np.clip((x - x0) / (x1 - x0), 0.0, 1.0)


def index(lat, lon, tsfc, land, ice, t850, t700, winds, grid_relative):
    """
    The score on the model's own grid; NaN outside BOX.

    lat, lon : 2-D degrees           tsfc : skin temperature, K (the lake's, over water)
    land     : 1 land .. 0 water     ice  : 0..1 ice cover, or None
    t850, t700 : K                   winds : {925: (u, v), 850: ..., 700: ...} in m/s
    grid_relative : True if u and v run along the grid's axes (Lambert grids)
                    rather than east and north
    """
    out = np.full(lat.shape, np.nan, dtype="float32")
    got = parts(lat, lon, tsfc, land, ice, t850, t700, winds, grid_relative)
    if got is None:
        return out
    sl, inbox, fetch, rise, trim = got
    fetch = fetch * (1.0 + RISE_GAIN * _ramp(rise, 0.0, RISE_FULL, 0.0, 1.0))
    score = 100.0 * (1.0 - np.exp(-fetch / FETCH_KM)) * trim
    out[sl] = np.where(inbox, score, np.nan)
    return out


def parts(lat, lon, tsfc, land, ice, t850, t700, winds, grid_relative):
    """
    What the score is put together from, on the part of the grid inside BOX:
    (slice of the grid, inside-BOX mask, heated fetch in km, m climbed, the
    shear and cap trim 0..1). None if the grid misses BOX. Apart from index()
    so verify/ can try other weightings without fetching a run again.
    """
    w, e, s, n = BOX
    inbox = (lon >= w) & (lon <= e) & (lat >= s) & (lat <= n)
    if not inbox.any():
        return None
    rows, cols = np.where(inbox.any(axis=1))[0], np.where(inbox.any(axis=0))[0]
    sl = (slice(rows[0], rows[-1] + 1), slice(cols[0], cols[-1] + 1))
    lat, lon = lat[sl].astype("float64"), lon[sl].astype("float64")
    cut = lambda a: np.nan_to_num(np.asarray(a[sl], dtype="float64"))
    tsfc, land, t850, t700 = cut(tsfc), cut(land), cut(t850), cut(t700)
    ice = cut(ice) if ice is not None else np.zeros_like(tsfc)
    ny, nx = lat.shape

    # What one step along each grid axis is on the ground, in km east and north.
    # Everything about the projection that matters is in these four numbers.
    coslat = np.cos(np.radians(lat))
    eix, eiy = np.gradient(lon, axis=1) * coslat * KM_PER_DEG, np.gradient(lat, axis=1) * KM_PER_DEG
    ejx, ejy = np.gradient(lon, axis=0) * coslat * KM_PER_DEG, np.gradient(lat, axis=0) * KM_PER_DEG

    def earth(uv):
        u, v = cut(uv[0]), cut(uv[1])
        if not grid_relative:
            return u, v
        # grid east and grid north, whichever way the rows and columns count
        li, lj = np.hypot(eix, eiy), np.hypot(ejx, ejy)
        si, sj = np.sign(eix), np.sign(ejy)
        return (u * si * eix / li + v * sj * ejx / lj,
                u * si * eiy / li + v * sj * ejy / lj)

    u9, v9 = earth(winds[925])
    u8, v8 = earth(winds[850])
    u7, v7 = earth(winds[700])
    us, vs = (u9 + u8) / 2.0, (v9 + v8) / 2.0          # what steers a band
    speed = np.hypot(us, vs)

    # one km upwind, as a move in grid index space
    dx, dy = -us / np.maximum(speed, 0.5), -vs / np.maximum(speed, 0.5)
    det = eix * ejy - ejx * eiy
    di, dj = (dx * ejy - dy * ejx) / det, (eix * dy - eiy * dx) / det

    water = (land < 0.5) & inbox[sl] & ~((lon > OCEAN[0]) & (lat < OCEAN[1]))
    heat = np.where(water, _ramp(tsfc - t850, DT_NONE, DT_FULL, 0.0, 1.0) * (1.0 - np.clip(ice, 0, 1)), 0.0)

    reach = INLAND_KM * np.clip(speed / 10.0, 0.5, 1.5)
    pj, pi = np.mgrid[0:ny, 0:nx].astype("float64")
    fetch = np.zeros((ny, nx))
    over_land = np.zeros((ny, nx))
    elev = terrain(lat, lon)
    low = elev.copy()                                  # the lowest ground within RISE_KM upwind
    alive = np.ones((ny, nx), dtype=bool)
    for step in range(int(REACH_KM / STEP_KM)):
        j, i = np.rint(pj).astype(int), np.rint(pi).astype(int)
        alive &= (j >= 0) & (j < ny) & (i >= 0) & (i < nx)
        if not alive.any():
            break
        j, i = np.clip(j, 0, ny - 1), np.clip(i, 0, nx - 1)
        fetch += np.where(alive, heat[j, i] * STEP_KM * np.exp(-over_land / reach), 0.0)
        over_land += np.where(water[j, i], 0.0, STEP_KM)
        if step * STEP_KM <= RISE_KM:
            low = np.where(alive, np.minimum(low, elev[j, i]), low)
        pi += di[j, i] * STEP_KM
        pj += dj[j, i] * STEP_KM

    # the wind turning with height between the lake and 700 mb, in degrees
    turn = np.degrees(np.arccos(np.clip(
        (u9 * u7 + v9 * v7) / np.maximum(np.hypot(u9, v9) * np.hypot(u7, v7), 1e-6), -1.0, 1.0)))
    shear = np.interp(turn, [30.0, 60.0, 90.0], [1.0, 0.55, 0.35])
    # 850 to 700 mb: about 10 C colder aloft is a deep, open layer; under 4 C is a lid
    cap = _ramp(t850 - t700, 4.0, 10.0, 0.6, 1.0)

    return sl, inbox[sl], fetch, elev - low, shear * cap
