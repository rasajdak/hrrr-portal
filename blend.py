"""
The Ryan Sajdak blend: ECMWF IFS and NOAA GFS, averaged on one grid.

Both models post a global 0.25 degree lat/lon grid, so each is cut to the
portal's region, interpolated bilinearly onto a shared 0.25 degree grid, and
averaged with a weight that depends on forecast hour. ECMWF gets the larger
share throughout, because it has the better record at medium and long range;
GFS keeps its share and carries the rest of the run out to 16 days, once
ECMWF stops at 10.

The weights are a judgment, not a fitted result: 50 % ECMWF at hour 0 rising
to 70 % at hour 240, the rest GFS. Change `ECMWF_W0` / `ECMWF_W240` to tune.

Pure numpy on purpose: this module does not import cfgrib or cartopy, so it
can be tested on its own.
"""
import numpy as np

GRID_STEP = 0.25          # degrees, the shared grid's spacing (both models' native spacing)
ECMWF_MAX = 240           # ECMWF's open data runs out to 10 days
ECMWF_W0 = 0.50           # ECMWF's share of the blend at forecast hour 0
ECMWF_W240 = 0.70         # ECMWF's share at hour 240 (and GFS-only past it)


def ecmwf_weight(fxx):
    """ECMWF's share of the blend at forecast hour `fxx` (0 past its last hour)."""
    if fxx > ECMWF_MAX:
        return 0.0
    return ECMWF_W0 + (ECMWF_W240 - ECMWF_W0) * fxx / ECMWF_MAX


def target_grid(bounds):
    """(lat, lon) 1-D arrays of the shared grid covering bounds = (west, east, south, north)."""
    w, e, s, n = bounds
    lat = np.round(np.arange(s, n + GRID_STEP / 2, GRID_STEP), 4)
    lon = np.round(np.arange(w, e + GRID_STEP / 2, GRID_STEP), 4)
    return lat, lon


def _ascending(axis, values, dim):
    if axis[0] > axis[-1]:
        return axis[::-1], np.flip(values, axis=dim)
    return axis, values


def regrid(values, lat2d, lon2d, tlat, tlon):
    """
    Bilinear interpolation of a field on a regular lat/lon grid (2-D lat/lon,
    as _decode returns them) onto the 1-D target axes tlat, tlon. Points with
    any NaN corner come out NaN. Target points outside the source are clamped
    to its edge, so cut the source with a margin past the target.
    """
    lat = np.asarray(lat2d[:, 0], dtype=float)
    lon = np.asarray(lon2d[0, :], dtype=float)
    v = np.asarray(values, dtype=float)
    if v.shape != (lat.size, lon.size):
        raise ValueError("values %s do not match grid %s" % (v.shape, (lat.size, lon.size)))
    lat, v = _ascending(lat, v, 0)
    lon, v = _ascending(lon, v, 1)
    if lat.size < 2 or lon.size < 2:
        raise ValueError("grid too small to interpolate")
    fi = np.interp(tlat, lat, np.arange(lat.size))      # fractional row index of each target latitude
    fj = np.interp(tlon, lon, np.arange(lon.size))      # fractional column index of each target longitude
    i0 = np.clip(np.floor(fi).astype(int), 0, lat.size - 2)
    j0 = np.clip(np.floor(fj).astype(int), 0, lon.size - 2)
    ti = (fi - i0)[:, None]
    tj = (fj - j0)[None, :]
    I, J = i0[:, None], j0[None, :]
    return (v[I, J] * (1 - ti) * (1 - tj) + v[I, J + 1] * (1 - ti) * tj
            + v[I + 1, J] * ti * (1 - tj) + v[I + 1, J + 1] * ti * tj)


def combine(parts, fxx):
    """
    Weighted average of regridded members, {"ecmwf": array, "gfs": array}, at
    forecast hour `fxx`. Where one member is missing (NaN) the other carries
    the point alone, at its full weight. A point missing in every member is NaN.
    """
    e = ecmwf_weight(fxx)
    weights = {"ecmwf": e, "gfs": 1.0 - e}
    num = den = 0.0
    for name, arr in parts.items():
        ok = np.isfinite(arr)
        num = num + np.where(ok, arr * weights[name], 0.0)
        den = den + np.where(ok, weights[name], 0.0)
    safe = np.where(den > 0, den, 1.0)
    return np.where(den > 0, num / safe, np.nan)
