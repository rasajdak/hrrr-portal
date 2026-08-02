"""
hrrr_render.py — pull one HRRR field from raw GRIB2 and render a Web-Mercator PNG.

Data source : AWS public bucket noaa-hrrr-bdp-pds (no credentials).
Subsetting  : Herbie reads the GRIB .idx and HTTP byte-ranges only the field asked
              for, so each layer is ~1 MB off the wire instead of the 150 MB file.
Rendering   : cartopy reprojects the native 3 km Lambert-conformal grid to
              EPSG:3857 so the PNG lines up 1:1 with a Leaflet imageOverlay.

The rendered image spans exactly the lat/lon box in EXTENT below, so the frontend
overlays it with bounds [[S, W], [N, E]] and it registers correctly.
"""

import io
import math
import os
import functools
import hashlib
from datetime import datetime, timezone, timedelta

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap, BoundaryNorm, Normalize

import cartopy.crs as ccrs

# Herbie is imported lazily inside fetch() so the module still imports for
# introspection (e.g. /api/config) on a machine that hasn't installed it yet.

# ---------------------------------------------------------------- geometry ---
# Fixed render box covering the HRRR CONUS domain (with a little margin).
# W, E, S, N  in degrees.
EXTENT = (-134.0, -60.0, 21.0, 53.0)
# HRRR's native CONUS grid is ~1799 px wide (3 km). Render well above that so
# individual grid cells stay crisp when the single overlay image is zoomed in,
# instead of being upscaled from below-native resolution (which reads as blur).
IMG_WIDTH = 2800  # px; height derived from the mercator aspect of EXTENT

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
os.makedirs(CACHE_DIR, exist_ok=True)


def _mercy(lat_deg):
    """Web-Mercator y (in radians of latitude) for aspect math."""
    return math.log(math.tan(math.pi / 4 + math.radians(lat_deg) / 2))


def _img_size():
    w, e, s, n = EXTENT
    x_span = math.radians(e - w)           # mercator x is linear in longitude
    y_span = _mercy(n) - _mercy(s)
    h = int(round(IMG_WIDTH * (y_span / x_span)))
    return IMG_WIDTH, h


# ---------------------------------------------------------------- colormaps --
def _nws_reflectivity():
    """Classic NWS reflectivity ramp, 5..75 dBZ in 5 dBZ bins."""
    colors = [
        "#04e9e7", "#019ff4", "#0300f4", "#02fd02", "#01c501", "#008e00",
        "#fdf802", "#e5bc00", "#fd9500", "#fd0000", "#d40000", "#bc0000",
        "#f800fd", "#9854c6",
    ]
    levels = list(range(5, 75, 5)) + [75]
    cmap = ListedColormap(colors)
    cmap.set_under((0, 0, 0, 0))           # <5 dBZ transparent
    norm = BoundaryNorm(levels, cmap.N)
    return cmap, norm


# field key -> everything needed to fetch + draw it
#   group     : dropdown optgroup
#   search    : Herbie/wgrib2 idx regex identifying the GRIB message
#   product   : HRRR file family ("sfc" = wrfsfcf surface, "prs" = wrfprsf pressure)
#   conv      : callable applied to the raw array (unit conversion)
#   unit      : display unit
#   wind      : if set, magnitude of (search, search_v) U/V components
#   mask_under: values below vmin render transparent (clean background); else clamped
K2F = lambda a: (a - 273.15) * 9 / 5 + 32
K2C = lambda a: a - 273.15
MS2MPH = lambda a: a * 2.236936

FIELDS = {
    # ---- radar & storms -------------------------------------------------
    "refc": dict(group="Radar & storms", label="Composite reflectivity", unit="dBZ",
        product="sfc", search=":REFC:", conv=None, cmap="_nws", vmin=5, vmax=75),
    "refd1": dict(group="Radar & storms", label="Reflectivity (1 km AGL)", unit="dBZ",
        product="sfc", search=":REFD:1000 m above ground:", conv=None, cmap="_nws", vmin=5, vmax=75),
    "retop": dict(group="Radar & storms", label="Echo top height", unit="kft",
        product="sfc", search=":RETOP:", conv=lambda a: a * 0.00328084, cmap="plasma",
        vmin=1, vmax=50, mask_under=True),
    "uphl25": dict(group="Radar & storms", label="2–5 km updraft helicity", unit="m²/s²",
        product="sfc", search=":MXUPHL:5000-2000 m above ground:", conv=None, cmap="hot_r",
        vmin=10, vmax=150, mask_under=True, min_fxx=1),

    # ---- instability ----------------------------------------------------
    "cape": dict(group="Instability", label="Surface CAPE", unit="J/kg",
        product="sfc", search=":CAPE:surface:", conv=None, cmap="magma", vmin=0, vmax=5000),
    "mlcape": dict(group="Instability", label="Mixed-layer CAPE", unit="J/kg",
        product="sfc", search=":CAPE:90-0 mb above ground:", conv=None, cmap="magma", vmin=0, vmax=4000),
    "mucape": dict(group="Instability", label="Most-unstable CAPE", unit="J/kg",
        product="sfc", search=":CAPE:255-0 mb above ground:", conv=None, cmap="magma", vmin=0, vmax=5000),
    "cin": dict(group="Instability", label="Surface CIN", unit="J/kg",
        product="sfc", search=":CIN:surface:", conv=None, cmap="cool", vmin=-300, vmax=0),
    "lftx": dict(group="Instability", label="Best (4-layer) lifted index", unit="°C",
        product="sfc", search=":4LFTX:", conv=None, cmap="RdBu_r", vmin=-10, vmax=10),
    "pwat": dict(group="Instability", label="Precipitable water", unit="mm",
        product="sfc", search=":PWAT:", conv=None, cmap="YlGnBu", vmin=5, vmax=60),

    # ---- rotation -------------------------------------------------------
    "hlcy3": dict(group="Rotation", label="0–3 km storm-rel. helicity", unit="m²/s²",
        product="sfc", search=":HLCY:3000-0 m above ground:", conv=None, cmap="OrRd", vmin=0, vmax=600),
    "hlcy1": dict(group="Rotation", label="0–1 km storm-rel. helicity", unit="m²/s²",
        product="sfc", search=":HLCY:1000-0 m above ground:", conv=None, cmap="OrRd", vmin=0, vmax=400),

    # ---- surface weather ------------------------------------------------
    "t2m": dict(group="Surface weather", label="2 m temperature", unit="°F",
        product="sfc", search=":TMP:2 m above ground:", conv=K2F, cmap="turbo", vmin=-20, vmax=110),
    "dpt2m": dict(group="Surface weather", label="2 m dewpoint", unit="°F",
        product="sfc", search=":DPT:2 m above ground:", conv=K2F, cmap="BrBG", vmin=0, vmax=85),
    "rh2m": dict(group="Surface weather", label="2 m relative humidity", unit="%",
        product="sfc", search=":RH:2 m above ground:", conv=None, cmap="YlGnBu", vmin=0, vmax=100),
    "wind10m": dict(group="Surface weather", label="10 m wind speed", unit="mph",
        product="sfc", search=":WIND:10 m above ground:", conv=MS2MPH, cmap="viridis", vmin=0, vmax=70),
    "gust": dict(group="Surface weather", label="Surface wind gust", unit="mph",
        product="sfc", search=":GUST:surface:", conv=MS2MPH, cmap="plasma", vmin=0, vmax=90),
    "mslp": dict(group="Surface weather", label="Mean sea-level pressure", unit="hPa",
        product="sfc", search=":MSLMA:", conv=lambda a: a / 100.0, cmap="Spectral_r", vmin=980, vmax=1040),
    "vis": dict(group="Surface weather", label="Surface visibility", unit="mi",
        product="sfc", search=":VIS:surface:", conv=lambda a: a * 0.000621371, cmap="cividis", vmin=0, vmax=10),

    # ---- precipitation --------------------------------------------------
    "apcp": dict(group="Precipitation", label="1-hr precipitation", unit="in",
        product="sfc", search=":APCP:surface:", conv=lambda a: a / 25.4, cmap="YlGnBu",
        vmin=0.01, vmax=2.0, mask_under=True, min_fxx=1),
    "prate": dict(group="Precipitation", label="Precipitation rate", unit="mm/hr",
        product="sfc", search=":PRATE:surface:", conv=lambda a: a * 3600.0, cmap="YlGnBu",
        vmin=0.1, vmax=25, mask_under=True),
    "snod": dict(group="Precipitation", label="Snow depth", unit="in",
        product="sfc", search=":SNOD:surface:", conv=lambda a: a * 39.3701, cmap="Blues",
        vmin=0.1, vmax=12, mask_under=True),

    # ---- clouds & radiation --------------------------------------------
    "tcdc": dict(group="Clouds & radiation", label="Total cloud cover", unit="%",
        product="sfc", search=":TCDC:entire atmosphere:", conv=None, cmap="bone", vmin=0, vmax=100),
    "ceil": dict(group="Clouds & radiation", label="Cloud ceiling height", unit="kft",
        product="sfc", search=":HGT:cloud ceiling:", conv=lambda a: a * 0.00328084, cmap="cividis",
        vmin=0, vmax=12),
    "dswrf": dict(group="Clouds & radiation", label="Downward shortwave", unit="W/m²",
        product="sfc", search=":DSWRF:surface:", conv=None, cmap="inferno", vmin=0, vmax=1000),
    "hpbl": dict(group="Clouds & radiation", label="Boundary-layer height", unit="m",
        product="sfc", search=":HPBL:surface:", conv=None, cmap="viridis", vmin=0, vmax=3000),

    # ---- smoke (HRRR-Smoke) --------------------------------------------
    "smoke_sfc": dict(group="Smoke", label="Near-surface smoke", unit="µg/m³",
        product="sfc", search=":MASSDEN:8 m above ground:", conv=lambda a: a * 1e9, cmap="YlOrRd",
        vmin=1, vmax=100, mask_under=True),
    "colmd": dict(group="Smoke", label="Vertically-integrated smoke", unit="mg/m²",
        product="sfc", search=":COLMD:", conv=lambda a: a * 1e6, cmap="YlOrRd",
        vmin=1, vmax=200, mask_under=True),

    # ---- upper air (pressure levels) -----------------------------------
    "h500": dict(group="Upper air", label="500 mb geopotential height", unit="dam",
        product="prs", search=":HGT:500 mb:", conv=lambda a: a / 10.0, cmap="Spectral_r",
        vmin=492, vmax=600),
    "t850": dict(group="Upper air", label="850 mb temperature", unit="°C",
        product="prs", search=":TMP:850 mb:", conv=K2C, cmap="turbo", vmin=-10, vmax=35),
    "t500": dict(group="Upper air", label="500 mb temperature", unit="°C",
        product="prs", search=":TMP:500 mb:", conv=K2C, cmap="turbo", vmin=-30, vmax=5),
    "rh700": dict(group="Upper air", label="700 mb relative humidity", unit="%",
        product="prs", search=":RH:700 mb:", conv=None, cmap="YlGnBu", vmin=0, vmax=100),
    "w700": dict(group="Upper air", label="700 mb vertical velocity", unit="Pa/s",
        product="prs", search=":VVEL:700 mb:", conv=None, cmap="RdBu", vmin=-3, vmax=3),
    "wind250": dict(group="Upper air", label="250 mb wind speed (jet)", unit="mph",
        product="prs", search=":UGRD:250 mb:", search_v=":VGRD:250 mb:", wind=True,
        conv=MS2MPH, cmap="viridis", vmin=0, vmax=160),
    "wind850": dict(group="Upper air", label="850 mb wind speed", unit="mph",
        product="prs", search=":UGRD:850 mb:", search_v=":VGRD:850 mb:", wind=True,
        conv=MS2MPH, cmap="viridis", vmin=0, vmax=70),
}


def field_catalog():
    """Lightweight metadata for the frontend (no numpy/cartopy needed)."""
    out = []
    for k, f in FIELDS.items():
        out.append(dict(
            key=k, group=f.get("group", "Other"), label=f["label"], unit=f["unit"],
            vmin=f["vmin"], vmax=f["vmax"], cmap=f["cmap"],
            min_fxx=f.get("min_fxx", 0),
        ))
    return out


def extent_bounds():
    """[[S, W], [N, E]] for Leaflet imageOverlay."""
    w, e, s, n = EXTENT
    return [[s, w], [n, e]]


# ---------------------------------------------------------------- run helper -
def latest_run():
    """
    Best-effort latest HRRR init that is likely posted to AWS.
    HRRR runs hourly; allow ~1h latency. Returns a datetime (UTC, on the hour).
    """
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return now - timedelta(hours=1)


def latest_available_run(max_back=8):
    """
    Probe AWS for the newest run actually posted, walking back from ~1h ago.
    Falls back to the now-2h guess if nothing is found (offline / transient).
    """
    from herbie import Herbie
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for b in range(1, max_back + 1):
        r = base - timedelta(hours=b)
        try:
            H = Herbie(r.strftime("%Y-%m-%d %H:%M"), model="hrrr", product="sfc", fxx=0)
            if getattr(H, "grib", None):
                return r
        except Exception:
            pass
    return base - timedelta(hours=2)


# ---- point sampling (click-to-probe & meteograms) --------------------------
@functools.lru_cache(maxsize=24)
def _grid_values(field_key, run_dt, fxx):
    """Flattened, unit-converted values for one field/run/hour (in-process cache)."""
    f = FIELDS[field_key]
    data, _, _ = _fetch(f, run_dt.strftime("%Y-%m-%d %H:%M"), fxx)
    if f["conv"]:
        data = f["conv"](data)
    return np.asarray(data, dtype="float32").ravel()


def sample(field_key, run_dt, fxx, lat0, lon0):
    """
    Nearest-grid-point value of a field at (lat0, lon0). Returns a float, or None
    if the point is off-grid / missing. Reuses whatever the render path already
    downloaded, so it is fast once a field/hour has been viewed or animated.
    """
    if field_key not in FIELDS:
        raise ValueError("unknown field %r" % field_key)
    if fxx < FIELDS[field_key].get("min_fxx", 0):
        return None
    try:
        vals = _grid_values(field_key, run_dt, fxx)  # also populates _LATF/_LONF
    except Exception:
        return None                                  # one bad hour ≠ dead meteogram
    if _LATF is None:
        return None
    d = (_LATF - lat0) ** 2 + (_LONF - lon0) ** 2
    i = int(np.argmin(d))
    v = vals[i]
    return None if not math.isfinite(v) else round(float(v), 2)


def _cmap_norm(f):
    if f["cmap"] == "_nws":
        return _nws_reflectivity()
    cmap = plt.get_cmap(f["cmap"]).copy()
    if f.get("mask_under"):
        # clean background: values under vmin drop out (precip, smoke, echo top…)
        cmap.set_under((0, 0, 0, 0))
        norm = Normalize(vmin=f["vmin"], vmax=f["vmax"], clip=False)
    else:
        # continuous field: clamp extremes to the end colors, no hole punched
        cmap.set_under(cmap(0.0))
        cmap.set_over(cmap(1.0))
        norm = Normalize(vmin=f["vmin"], vmax=f["vmax"], clip=True)
    return cmap, norm


# ---------------------------------------------------------------- main render
def render(field_key, run_dt, fxx):
    """
    Return path to a cached PNG for (field, run, forecast-hour), rendering if
    it does not already exist. Raises ValueError / RuntimeError on bad input or
    fetch failure.
    """
    if field_key not in FIELDS:
        raise ValueError("unknown field %r" % field_key)
    f = FIELDS[field_key]
    if fxx < f.get("min_fxx", 0):
        raise ValueError("%s needs forecast hour >= %d" % (field_key, f["min_fxx"]))

    run_str = run_dt.strftime("%Y-%m-%d %H:%M")
    key = hashlib.md5(
        ("%s|%s|%02d" % (field_key, run_dt.strftime("%Y%m%d%H"), fxx)).encode()
    ).hexdigest()[:16]
    out_path = os.path.join(CACHE_DIR, "%s.png" % key)
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path

    data, lat, lon = _fetch(f, run_str, fxx)
    if f["conv"]:
        data = f["conv"](data)

    _draw(f, data, lat, lon, out_path)
    return out_path


# HRRR's horizontal grid is identical across every field and forecast hour, so we
# capture the flattened lat/lon once (from the first fetch) and reuse it for all
# point lookups instead of carrying coords around per query.
_LATF = None
_LONF = None


def _one(H, search):
    """Return (data, lat, lon) for a single GRIB message matched by `search`."""
    global _LATF, _LONF
    try:
        ds = H.xarray(search, remove_grib=False)
    except Exception:
        # A corrupt/interrupted subset can get cached and then reused forever.
        # Force one clean re-download of just this message, then retry.
        try:
            H.download(search, overwrite=True)
        except Exception:
            pass
        ds = H.xarray(search, remove_grib=False)
    if isinstance(ds, list):
        ds = ds[0]
    var = [v for v in ds.data_vars][0]
    data = np.asarray(ds[var].values, dtype="float32")
    lat = np.asarray(ds.latitude.values, dtype="float32")
    lon = np.asarray(ds.longitude.values, dtype="float32")
    lon = np.where(lon > 180, lon - 360, lon)  # 0..360 -> -180..180
    if _LATF is None:
        _LATF = lat.ravel()
        _LONF = lon.ravel()
    return data, lat, lon


def _fetch(f, run_str, fxx):
    from herbie import Herbie
    H = Herbie(run_str, model="hrrr", product=f["product"], fxx=fxx)
    if f.get("wind"):
        u, lat, lon = _one(H, f["search"])
        v, _, _ = _one(H, f["search_v"])
        data = np.hypot(u, v)
        return data, lat, lon
    return _one(H, f["search"])


def _draw(f, data, lat, lon, out_path):
    cmap, norm = _cmap_norm(f)
    w, e, s, n = EXTENT
    px_w, px_h = _img_size()
    dpi = 100

    fig = plt.figure(figsize=(px_w / dpi, px_h / dpi), dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1], projection=ccrs.epsg(3857))
    ax.set_extent([w, e, s, n], crs=ccrs.PlateCarree())
    ax.set_aspect("auto")           # fill the frame; aspect already matched
    ax.set_axis_off()

    # mask non-finite so ocean / off-grid stays transparent
    masked = np.ma.masked_invalid(data)
    ax.pcolormesh(
        lon, lat, masked,
        transform=ccrs.PlateCarree(),
        cmap=cmap, norm=norm, shading="auto",
    )
    fig.savefig(out_path, transparent=True, dpi=dpi,
                pad_inches=0, bbox_inches=None)
    plt.close(fig)


def render_legend(field_key):
    """Return PNG bytes of a horizontal colorbar for the given field."""
    if field_key not in FIELDS:
        raise ValueError("unknown field")
    f = FIELDS[field_key]
    cmap, norm = _cmap_norm(f)
    fig = plt.figure(figsize=(3.0, 0.55), dpi=110)
    ax = fig.add_axes([0.04, 0.45, 0.92, 0.5])
    cb = matplotlib.colorbar.ColorbarBase(
        ax, cmap=cmap, norm=norm, orientation="horizontal")
    cb.ax.tick_params(labelsize=7, colors="#cfe0f2", length=2)
    cb.outline.set_edgecolor("#33465e")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    return buf.getvalue()
