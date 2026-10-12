"""
hrrr_render.py — pull one model field from raw GRIB2 and render it, either as a
Web-Mercator PNG overlay for the Leaflet map or as a finished static map.

Models      : HRRR (3 km), NAM CONUS nest (3 km), RAP (13 km), GFS (0.25 deg),
              all from NOAA's public AWS buckets (no credentials).
Subsetting  : the GRIB .idx sidecar lists every message and its byte offset, so
              one HTTP Range request pulls only the field asked for, about 1 MB
              off the wire instead of the 150 MB file.
Rendering   : cartopy reprojects the native grid to EPSG:3857 so the overlay PNG
              lines up 1:1 with a Leaflet imageOverlay. Static maps are drawn in
              a Lambert conformal fitted to the region, with borders, a title,
              city values and a colorbar, sized to post.

The overlay image spans exactly the lat/lon box in EXTENT below, so the frontend
overlays it with bounds [[S, W], [N, E]] and it registers correctly.
"""

import io
import math
import os
import re
import functools
import hashlib
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patheffects as pe
from matplotlib.colors import ListedColormap, BoundaryNorm, Normalize

import cartopy.crs as ccrs

# The static maps are set in Barlow (labels, numbers) and Barlow Condensed (the
# title): type drawn from highway signs and timetables, which is what a weather
# chart is closer to than matplotlib's default DejaVu. Shipped in fonts/ so a
# server needs nothing installed; DejaVu remains the fallback (and has "°").
import logging
from matplotlib import font_manager as _fm
# the DejaVu fallback has no medium or semibold; findfont says so on every draw
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
_FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
if os.path.isdir(_FONT_DIR):
    for _n in sorted(os.listdir(_FONT_DIR)):
        if _n.endswith(".ttf"):
            _fm.fontManager.addfont(os.path.join(_FONT_DIR, _n))
    matplotlib.rcParams["font.family"] = ["Barlow", "DejaVu Sans"]
TITLE_FONT = ["Barlow Condensed", "Barlow", "DejaVu Sans"]

# cfgrib is imported lazily inside _message() so the module still imports for
# introspection (e.g. /api/config) on a machine that hasn't installed it yet.

# ---------------------------------------------------------------- geometry ---
# Fixed render box covering the HRRR CONUS domain (with a little margin).
# W, E, S, N  in degrees.
EXTENT = (-134.0, -60.0, 21.0, 53.0)
# Gouraud shading interpolates smoothly, so the output doesn't need to exceed the
# native grid — a moderate width keeps renders fast on a 1-vCPU box and upscales
# smoothly. Each model's stride downsamples its grid before shading (fewer
# triangles to rasterize); gouraud fills the gaps smoothly, so the look holds.
IMG_WIDTH = 2400  # px; height derived from the mercator aspect of EXTENT

# Part of every cache key. Bump it when a colormap or the drawing changes, or a
# PNG rendered the old way is served forever.
RENDER_REV = "m10"

# One drawing at a time in each process. The web server answers requests on
# several threads, and neither matplotlib's figure state nor the projection
# transforms under cartopy are safe to share between them: two requests once
# built the same basemap at the same moment and pickled a tangle of lines
# hundreds of kilometres long. Separate worker processes still draw in parallel.
import threading
GEO_LOCK = threading.RLock()


def part_name(path):
    """
    A scratch name for writing `path`, renamed over it when complete. Unique to
    the process and the thread: the process id alone let two requests in one
    server process write the same scratch file, and the slower one then found
    it already renamed away.
    """
    return "%s.%d.%d.part" % (path, os.getpid(), threading.get_ident())


def _locked(fn):
    @functools.wraps(fn)
    def inner(*a, **k):
        with GEO_LOCK:
            return fn(*a, **k)
    return inner


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


# ------------------------------------------------------------------ models ---
# key -> where the files live and how far the run goes
#   cycle   : hours between runs
#   max_fxx : last forecast hour the portal shows
#   step    : hours between frames on the timeline
#   probe   : the forecast hour whose .idx must exist before a run counts as
#             posted. HRRR keeps 0 (the desk panel wants the newest run the
#             moment it starts landing); the rest wait for their last frame, so
#             a loop never runs off the end of a half-uploaded run.
#   stride  : grid downsample for the overlay
#   url     : format string; {ymd} {hh} {fxx} {product}
ECMWF_HOST = "https://storage.googleapis.com/ecmwf-open-data"
MODELS = {
    "hrrr": dict(label="HRRR", name="HRRR 3 km", desc="High-Resolution Rapid Refresh · 3 km · hourly",
        cycle=1, max_fxx=18, step=1, probe=0, back=8, stride=2,
        url="https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{ymd}/conus/"
            "hrrr.t{hh}z.wrf{product}f{fxx:02d}.grib2"),
    "nam": dict(label="NAM 3 km", name="NAM 3 km", desc="NAM CONUS nest · 3 km · every 6 h, out 60 h",
        cycle=6, max_fxx=60, step=3, probe=60, back=5, stride=2,
        url="https://noaa-nam-pds.s3.amazonaws.com/nam.{ymd}/"
            "nam.t{hh}z.conusnest.hiresf{fxx:02d}.tm00.grib2"),
    "rap": dict(label="RAP", name="RAP 13 km", desc="Rapid Refresh · 13 km · hourly, out 21 h",
        cycle=1, max_fxx=21, step=1, probe=21, back=8, stride=1,
        url="https://noaa-rap-pds.s3.amazonaws.com/rap.{ymd}/"
            "rap.t{hh}z.awp130pgrbf{fxx:02d}.grib2"),
    "gfs": dict(label="GFS", name="GFS 0.25°", desc="Global Forecast System · 0.25° · every 6 h, out 10 days",
        cycle=6, max_fxx=240, step=6, probe=240, back=5, stride=1,
        url="https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{ymd}/{hh}/atmos/"
            "gfs.t{hh}z.pgrb2.0p25.f{fxx:03d}"),
    # ECMWF's open data. Its own index format (JSON lines, see _idx) and its own
    # short names, so its fields are listed apart, in ECMWF below. Only the 00Z
    # and 12Z runs go the full ten days. Read from Google's mirror: the AWS
    # bucket answers 503 Slow Down to about one request in three.
    "ecmwf": dict(label="ECMWF", name="ECMWF IFS 0.25°", desc="European model (IFS open data) · 0.25° · every 12 h, out 10 days",
        cycle=12, max_fxx=240, step=6, probe=240, back=4, stride=1,
        url=ECMWF_HOST + "/{ymd}/{hh}z/ifs/0p25/oper/"
            "{ymd}{hh}0000-{fxx}h-oper-fc.grib2"),
}
ALL = ("hrrr", "nam", "rap", "gfs")
MESO = ("hrrr", "nam", "rap")


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


# Banded scales, the way forecast offices draw them. One colour per interval
# between levels, plus a last colour for everything past the top level.
SNOW_LEVELS = [0.1, 1, 2, 3, 4, 6, 8, 12, 18, 24, 30, 36, 48]
SNOW_COLORS = ["#d4e6f5", "#a6cdea", "#6baed6", "#2f7fc1", "#0a4a9c", "#6a3fa6",
               "#b266c9", "#f1e04b", "#f5a623", "#e8591a", "#c41414", "#7a0c0c",
               "#f2c4dc"]
QPF_LEVELS = [0.01, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8]
QPF_COLORS = ["#c7f0b8", "#7fd67a", "#33a84a", "#1a7a3a", "#2b6fc4", "#58a8f0",
              "#f5e84a", "#f5a623", "#e8491a", "#b81414", "#8a2be2", "#f0b8f0"]
ICE_LEVELS = [0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0]
ICE_COLORS = ["#f9d5e5", "#f4a6c6", "#ec6fa6", "#d6398a", "#a61e7a", "#6d1466",
              "#3d0c4a"]
# Radar by precipitation type: one intensity scale, three colour families.
# Six steps of reflectivity each for rain, wintry mix and snow, light to heavy.
RP_LEVELS = [5, 15, 25, 35, 45, 55]
RP_GROUPS = ["Rain", "Mix", "Snow"]
RP_COLORS = ["#a8e6a1", "#5cc35a", "#1f9a36", "#f2e03a", "#f08c1e", "#d7261e",
             "#f9cdd8", "#f3a0b8", "#e86a95", "#d13d78", "#a81f66", "#7a124f",
             "#dfe4fb", "#b9c3f5", "#8e9cec", "#5f6fdc", "#3a45c0", "#262a8f"]
PTYPE_CATS = ["Rain", "Freezing rain", "Sleet or mix", "Snow"]
PTYPE_COLORS = ["#3fae49", "#f06292", "#9c5fd1", "#3d8bfd"]


# field key -> everything needed to fetch + draw it
#   group     : dropdown optgroup
#   search    : regex identifying the GRIB message in the .idx. {f} is the
#               forecast hour and {p} the previous frame's, for accumulations.
#   product   : HRRR file family ("sfc" = wrfsfcf surface, "prs" = wrfprsf
#               pressure); the other models keep everything in one file
#   models    : which models carry it (default: all four)
#   per       : per-model overrides of anything here (a different search, mostly)
#   conv      : callable applied to the raw array (unit conversion)
#   unit      : display unit
#   wind      : if set, magnitude of (search, search_v) U/V components
#   ptype     : if set, the four categorical precip-type flags folded into one
#   levels    : banded scale (with colors) instead of a continuous cmap
#   mask_under: values below vmin render transparent (clean background); else clamped
#   diff      : the model posts only running totals; subtract the last frame's
#   accum     : the model posts only per-frame buckets; sum them into a run total
#   weight    : a second message to multiply the first by (GFS snow, see below)
#   min_fxx   : 1 means "not at hour 0" (an accumulation or an hourly maximum)
K2F = lambda a: (a - 273.15) * 9 / 5 + 32
K2C = lambda a: a - 273.15
MS2MPH = lambda a: a * 2.236936
MS2KT = lambda a: a * 1.943844
MM2IN = lambda a: a / 25.4
M2IN = lambda a: a * 39.3701
M2KFT = lambda a: a * 0.00328084
SWE2SNOW = lambda a: a / 25.4 * 10.0       # kg/m² of water -> inches of snow at 10:1

# a total since the run began. wgrib2 writes "0-5 day acc" once the hours divide
# by 24, so the hour cannot simply be templated in.
TOTAL = r"0-\d+ (hour|day) acc"
BUCKET = "{p}-{f} hour acc"
# GFS snow: precipitation in the bucket, weighted by how much of it fell as snow
GFS_SNOW = dict(search=":APCP:surface:" + BUCKET, weight=":CSNOW:surface:{p}-{f} hour ave")
# an instantaneous message, as opposed to an accumulation or average of the
# same variable on the same level
INST = r"(anl|\d+ hour fcst):"
UV10 = dict(search=":UGRD:10 m above ground:", search_v=":VGRD:10 m above ground:", wind=True)

FIELDS = {
    # ---- radar & storms -------------------------------------------------
    "refc": dict(group="Radar & storms", label="Composite reflectivity", unit="dBZ",
        product="sfc", search=":REFC:", conv=None, cmap="_nws", vmin=5, vmax=75),
    # composite reflectivity for how hard, the type flags for what: the picture
    # a TV "futurecast" shows. Fetched as group * 100 + dBZ (see _radar_ptype).
    "refc_ptype": dict(group="Radar & storms", label="Radar by precipitation type", unit="dBZ",
        product="sfc", radar_ptype=True, search=":REFC:", conv=None, vmin=5, vmax=75,
        mask_under=True),
    "refd1": dict(group="Radar & storms", label="Reflectivity (1 km AGL)", unit="dBZ",
        product="sfc", search=":REFD:1000 m above ground:", conv=None, cmap="_nws", vmin=5, vmax=75),
    "maxref": dict(group="Radar & storms", label="Max 1 km reflectivity, past {step} h", unit="dBZ",
        models=("hrrr", "nam"), product="sfc", search=":MAXREF:1000 m above ground:", conv=None,
        cmap="_nws", vmin=5, vmax=75, min_fxx=1),
    "retop": dict(group="Radar & storms", label="Echo top height", unit="kft",
        models=MESO, product="sfc", search=":RETOP:", conv=M2KFT, cmap="plasma",
        vmin=1, vmax=50, mask_under=True),
    "vil": dict(group="Radar & storms", label="Vertically integrated liquid", unit="kg/m²",
        models=("hrrr",), product="sfc", search=":VIL:", conv=None, cmap="turbo",
        vmin=1, vmax=70, mask_under=True),
    "uphl25": dict(group="Radar & storms", label="2–5 km updraft helicity (max)", unit="m²/s²",
        models=("hrrr", "nam"), product="sfc", search=":MXUPHL:5000-2000 m above ground:", conv=None,
        cmap="hot_r", vmin=10, vmax=150, mask_under=True, min_fxx=1),
    "uphl03": dict(group="Radar & storms", label="0–3 km updraft helicity (max)", unit="m²/s²",
        models=("hrrr", "nam"), product="sfc", search=":MXUPHL:3000-0 m above ground:", conv=None,
        cmap="hot_r", vmin=10, vmax=120, mask_under=True, min_fxx=1),
    "uphl02": dict(group="Radar & storms", label="0–2 km updraft helicity (max)", unit="m²/s²",
        models=("hrrr",), product="sfc", search=":MXUPHL:2000-0 m above ground:", conv=None,
        cmap="hot_r", vmin=5, vmax=80, mask_under=True, min_fxx=1),
    "maxuvv": dict(group="Radar & storms", label="Max updraft speed", unit="m/s",
        models=("hrrr", "nam"), product="sfc", search=":MAXUVV:", conv=None, cmap="YlOrRd",
        vmin=2, vmax=40, mask_under=True, min_fxx=1),
    "maxdvv": dict(group="Radar & storms", label="Max downdraft speed", unit="m/s",
        models=("hrrr", "nam"), product="sfc", search=":MAXDVV:", conv=lambda a: -a, cmap="PuBu",
        vmin=2, vmax=25, mask_under=True, min_fxx=1),
    "hail": dict(group="Radar & storms", label="Max hail size", unit="in",
        models=("hrrr",), product="sfc", search=":HAIL:entire atmosphere:", conv=M2IN, cmap="RdPu",
        vmin=0.25, vmax=3, mask_under=True, min_fxx=1),
    "ltng": dict(group="Radar & storms", label="Lightning flash density", unit="fl/km²/5min",
        models=("hrrr",), product="sfc", search=":LTNG:", conv=None, cmap="YlOrRd",
        vmin=0.05, vmax=6, mask_under=True),

    # ---- instability ----------------------------------------------------
    "cape": dict(group="Instability", label="Surface CAPE", unit="J/kg",
        product="sfc", search=":CAPE:surface:", conv=None, cmap="magma", vmin=0, vmax=5000),
    "mlcape": dict(group="Instability", label="Mixed-layer CAPE", unit="J/kg",
        product="sfc", search=":CAPE:90-0 mb above ground:", conv=None, cmap="magma", vmin=0, vmax=4000),
    "mucape": dict(group="Instability", label="Most-unstable CAPE", unit="J/kg",
        product="sfc", search=":CAPE:255-0 mb above ground:", conv=None, cmap="magma", vmin=0, vmax=5000),
    "cape03": dict(group="Instability", label="0–3 km CAPE", unit="J/kg",
        models=("hrrr", "rap"), product="sfc", search=":CAPE:0-3000 m above ground:", conv=None,
        cmap="magma", vmin=0, vmax=300),
    "cin": dict(group="Instability", label="Surface CIN", unit="J/kg",
        product="sfc", search=":CIN:surface:", conv=None, cmap="cool", vmin=-300, vmax=0),
    "lftx": dict(group="Instability", label="Best (4-layer) lifted index", unit="°C",
        product="sfc", search=":4LFTX:", conv=None, cmap="RdBu_r", vmin=-10, vmax=10),
    "pwat": dict(group="Instability", label="Precipitable water", unit="mm",
        product="sfc", search=":PWAT:entire atmosphere", conv=None, cmap="YlGnBu", vmin=5, vmax=60),

    # ---- rotation & shear -----------------------------------------------
    "hlcy3": dict(group="Rotation & shear", label="0–3 km storm-rel. helicity", unit="m²/s²",
        product="sfc", search=":HLCY:3000-0 m above ground:", conv=None, cmap="OrRd", vmin=0, vmax=600),
    "hlcy1": dict(group="Rotation & shear", label="0–1 km storm-rel. helicity", unit="m²/s²",
        models=MESO, product="sfc", search=":HLCY:1000-0 m above ground:", conv=None, cmap="OrRd",
        vmin=0, vmax=400),
    "shear06": dict(group="Rotation & shear", label="0–6 km bulk shear", unit="kt",
        models=MESO, product="sfc", search=":VUCSH:0-6000 m above ground:",
        search_v=":VVCSH:0-6000 m above ground:", wind=True, conv=MS2KT, cmap="viridis",
        vmin=0, vmax=90),
    "shear01": dict(group="Rotation & shear", label="0–1 km bulk shear", unit="kt",
        models=("hrrr",), product="sfc", search=":VUCSH:0-1000 m above ground:",
        search_v=":VVCSH:0-1000 m above ground:", wind=True, conv=MS2KT, cmap="viridis",
        vmin=0, vmax=50),

    # ---- surface weather ------------------------------------------------
    "t2m": dict(group="Surface weather", label="2 m temperature", unit="°F",
        product="sfc", search=":TMP:2 m above ground:", conv=K2F, cmap="turbo", vmin=-20, vmax=110),
    "dpt2m": dict(group="Surface weather", label="2 m dewpoint", unit="°F",
        product="sfc", search=":DPT:2 m above ground:", conv=K2F, cmap="BrBG", vmin=0, vmax=85),
    "rh2m": dict(group="Surface weather", label="2 m relative humidity", unit="%",
        product="sfc", search=":RH:2 m above ground:", conv=None, cmap="YlGnBu", vmin=0, vmax=100),
    "wind10m": dict(group="Surface weather", label="10 m wind speed", unit="mph",
        product="sfc", search=":WIND:10 m above ground:", conv=MS2MPH, cmap="viridis", vmin=0, vmax=70,
        per=dict(nam=UV10, rap=UV10, gfs=UV10)),
    "gust": dict(group="Surface weather", label="Surface wind gust", unit="mph",
        product="sfc", search=":GUST:surface:", conv=MS2MPH, cmap="plasma", vmin=0, vmax=90),
    "mslp": dict(group="Surface weather", label="Mean sea-level pressure", unit="hPa",
        product="sfc", search=":MSLMA:", conv=lambda a: a / 100.0, cmap="Spectral_r", vmin=980, vmax=1040,
        per=dict(nam=dict(search=":PRMSL:"), gfs=dict(search=":PRMSL:"))),
    "vis": dict(group="Surface weather", label="Surface visibility", unit="mi",
        product="sfc", search=":VIS:surface:", conv=lambda a: a * 0.000621371, cmap="cividis", vmin=0, vmax=10),

    # ---- precipitation --------------------------------------------------
    "apcp": dict(group="Precipitation", label="{step}-hr precipitation", unit="in",
        product="sfc", search=":APCP:surface:" + BUCKET, conv=MM2IN,
        levels=QPF_LEVELS, colors=QPF_COLORS, vmin=0.01, vmax=8, mask_under=True, min_fxx=1),
    "apcp_tot": dict(group="Precipitation", label="Total precipitation since run start", unit="in",
        product="sfc", search=":APCP:surface:" + TOTAL,
        conv=MM2IN, levels=QPF_LEVELS, colors=QPF_COLORS, vmin=0.01, vmax=8,
        mask_under=True, min_fxx=1,
        per=dict(nam=dict(search=":APCP:surface:" + BUCKET, accum=True))),
    "prate": dict(group="Precipitation", label="Precipitation rate", unit="mm/hr",
        product="sfc", search=":PRATE:surface:" + INST, conv=lambda a: a * 3600.0, cmap="YlGnBu",
        vmin=0.1, vmax=25, mask_under=True),

    # ---- snow & ice -----------------------------------------------------
    "asnow": dict(group="Snow & ice", label="Total snowfall since run start", unit="in",
        models=("hrrr", "rap"), product="sfc", search=":ASNOW:surface:" + TOTAL, conv=M2IN,
        levels=SNOW_LEVELS, colors=SNOW_COLORS, vmin=0.1, vmax=48, mask_under=True, min_fxx=1),
    "snow10": dict(group="Snow & ice", label="Total snowfall at 10:1 since run start", unit="in",
        product="sfc", search=":WEASD:surface:" + TOTAL, conv=SWE2SNOW,
        levels=SNOW_LEVELS, colors=SNOW_COLORS, vmin=0.1, vmax=48, mask_under=True, min_fxx=1,
        per=dict(nam=dict(search=":WEASD:surface:" + BUCKET, accum=True),
                 gfs=dict(GFS_SNOW, accum=True))),
    "snow_step": dict(group="Snow & ice", label="{step}-hr snowfall at 10:1", unit="in",
        product="sfc", search=":WEASD:surface:" + BUCKET, conv=SWE2SNOW,
        levels=SNOW_LEVELS, colors=SNOW_COLORS, vmin=0.1, vmax=48, mask_under=True, min_fxx=1,
        per=dict(gfs=GFS_SNOW)),
    "ptype": dict(group="Snow & ice", label="Precipitation type", unit="type",
        product="sfc", ptype=True, search=":CRAIN:surface:" + INST, conv=None,
        levels=[0.5, 1.5, 2.5, 3.5, 4.5], colors=PTYPE_COLORS, cats=PTYPE_CATS,
        vmin=0.5, vmax=4.5, mask_under=True),
    "frzr": dict(group="Snow & ice", label="Freezing rain since run start", unit="in",
        models=("hrrr", "rap"), product="sfc", search=":FRZR:surface:" + TOTAL, conv=MM2IN,
        levels=ICE_LEVELS, colors=ICE_COLORS, vmin=0.01, vmax=1, mask_under=True, min_fxx=1),
    "cpofp": dict(group="Snow & ice", label="Share of precipitation frozen", unit="%",
        models=("hrrr", "nam", "gfs"), product="sfc", search=":CPOFP:surface:", conv=None,
        cmap="cool_r", vmin=1, vmax=100, mask_under=True),
    "snod": dict(group="Snow & ice", label="Snow depth on the ground", unit="in",
        product="sfc", search=":SNOD:surface:", conv=M2IN,
        levels=SNOW_LEVELS, colors=SNOW_COLORS, vmin=0.1, vmax=48, mask_under=True),
    "swe": dict(group="Snow & ice", label="Water in the snowpack", unit="in",
        product="sfc", search=":WEASD:surface:" + INST, conv=MM2IN, cmap="PuBu",
        vmin=0.05, vmax=10, mask_under=True),
    "snowc": dict(group="Snow & ice", label="Snow cover", unit="%",
        models=("hrrr", "nam"), product="sfc", search=":SNOWC:surface:", conv=None, cmap="Blues",
        vmin=5, vmax=100, mask_under=True),
    "frzlvl": dict(group="Snow & ice", label="Freezing level height", unit="kft",
        models=("hrrr",), product="sfc", search=":HGT:0C isotherm:", conv=M2KFT, cmap="Spectral",
        vmin=0, vmax=16),

    # ---- clouds & radiation --------------------------------------------
    "tcdc": dict(group="Clouds & radiation", label="Total cloud cover", unit="%",
        product="sfc", search=":TCDC:entire atmosphere:" + INST, conv=None, cmap="bone", vmin=0, vmax=100,
        per=dict(nam=dict(search=":TCDC:entire atmosphere"))),
    "ceil": dict(group="Clouds & radiation", label="Cloud ceiling height", unit="kft",
        product="sfc", search=":HGT:cloud ceiling:", conv=M2KFT, cmap="cividis",
        vmin=0, vmax=12),
    "dswrf": dict(group="Clouds & radiation", label="Downward shortwave", unit="W/m²",
        models=("hrrr", "nam"), product="sfc", search=":DSWRF:surface:" + INST, conv=None,
        cmap="inferno", vmin=0, vmax=1000),
    "hpbl": dict(group="Clouds & radiation", label="Boundary-layer height", unit="m",
        product="sfc", search=":HPBL:surface:", conv=None, cmap="viridis", vmin=0, vmax=3000),

    # ---- smoke (HRRR-Smoke) --------------------------------------------
    "smoke_sfc": dict(group="Smoke", label="Near-surface smoke", unit="µg/m³",
        models=("hrrr", "rap"), product="sfc", search=":MASSDEN:8 m above ground:",
        conv=lambda a: a * 1e9, cmap="YlOrRd", vmin=1, vmax=100, mask_under=True),
    "colmd": dict(group="Smoke", label="Vertically-integrated smoke", unit="mg/m²",
        models=("hrrr",), product="sfc", search=":COLMD:", conv=lambda a: a * 1e6, cmap="YlOrRd",
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


# What ECMWF calls each field, in its index: ":levtype:param:level:". It keeps
# running totals only (no buckets), so a per-frame amount is this frame minus
# the last (`diff`). Lengths are metres of water, not kg/m².
M_SWE2SNOW = lambda a: a * 39.3701 * 10.0
ECMWF = {
    "t2m": dict(search=":sfc:2t:"),
    "dpt2m": dict(search=":sfc:2d:"),
    "wind10m": dict(search=":sfc:10u:", search_v=":sfc:10v:", wind=True),
    "gust": dict(search=":sfc:10fg:"),
    "mslp": dict(search=":sfc:msl:"),
    "mucape": dict(search=":sfc:mucape:"),
    "pwat": dict(search=":sfc:tcwv:"),
    "tcdc": dict(search=":sfc:tcc:", conv=lambda a: a * 100.0),      # a 0-1 fraction
    "apcp": dict(search=":sfc:tp:", diff=True, conv=M2IN),
    "apcp_tot": dict(search=":sfc:tp:", conv=M2IN),
    "prate": dict(search=":sfc:tprate:"),
    "snow10": dict(search=":sfc:sf:", conv=M_SWE2SNOW),
    "snow_step": dict(search=":sfc:sf:", diff=True, conv=M_SWE2SNOW),
    "swe": dict(search=":sfc:sd:", conv=M2IN),
    "ptype": dict(search=":sfc:ptype:", ptype="wmo"),
    # no radar field: intensity comes from the precipitation rate instead
    "refc_ptype": dict(search=":sfc:tprate:"),
    "h500": dict(search=":pl:gh:500:"),
    "t850": dict(search=":pl:t:850:"),
    "t500": dict(search=":pl:t:500:"),
    "rh700": dict(search=":pl:r:700:"),
    "w700": dict(search=":pl:w:700:"),
    "wind250": dict(search=":pl:u:250:", search_v=":pl:v:250:"),
    "wind850": dict(search=":pl:u:850:", search_v=":pl:v:850:"),
}
for _k, _o in ECMWF.items():
    FIELDS[_k]["models"] = FIELDS[_k].get("models", ALL) + ("ecmwf",)
    FIELDS[_k].setdefault("per", {})["ecmwf"] = _o
# WMO precipitation-type codes -> rain 1, freezing rain 2, sleet or mix 3, snow 4
WMO_PTYPE = {1: 1, 3: 2, 12: 2, 7: 3, 8: 3, 5: 4, 6: 4}


def spec(field_key, model="hrrr"):
    """
    The field as one model carries it: per-model overrides applied and the
    label filled in. Raises ValueError if the field or model is unknown, or if
    that model does not carry the field.
    """
    if model not in MODELS:
        raise ValueError("unknown model %r" % model)
    if field_key not in FIELDS:
        raise ValueError("unknown field %r" % field_key)
    f = FIELDS[field_key]
    if model not in f.get("models", ALL):
        raise ValueError("%s has no %s" % (MODELS[model]["label"], f["label"].format(step="n")))
    step = MODELS[model]["step"]
    out = dict(f)
    out.update(f.get("per", {}).get(model, {}))
    out["label"] = f["label"].format(step=step)
    out["min_fxx"] = step if f.get("min_fxx") else 0
    return out


def field_catalog(model="hrrr"):
    """Lightweight metadata for the frontend (no numpy/cartopy needed)."""
    out = []
    for k, f in FIELDS.items():
        if model not in f.get("models", ALL):
            continue
        s = spec(k, model)
        out.append(dict(
            key=k, group=s.get("group", "Other"), label=s["label"], unit=s["unit"],
            vmin=s["vmin"], vmax=s["vmax"], cmap=s.get("cmap", "banded"),
            min_fxx=s["min_fxx"], cats=s.get("cats"),
        ))
    return out


def credit(model):
    # ECMWF's open data is CC BY 4.0: the attribution is a condition of use
    return "ECMWF IFS open data (CC BY 4.0)" if model == "ecmwf" else "NOAA " + MODELS[model]["label"].split()[0]


def model_catalog():
    return [dict(key=k, label=m["label"], name=m["name"], desc=m["desc"], credit=credit(k),
                 max_fxx=m["max_fxx"], step=m["step"]) for k, m in MODELS.items()]


def extent_bounds():
    """[[S, W], [N, E]] for Leaflet imageOverlay."""
    w, e, s, n = EXTENT
    return [[s, w], [n, e]]


# ---------------------------------------------------------------- GRIB access
def _grib_url(model, product, run_dt, fxx):
    return MODELS[model]["url"].format(
        ymd=run_dt.strftime("%Y%m%d"), hh=run_dt.strftime("%H"), fxx=fxx, product=product)


def _http(url, rng=None, timeout=60, method="GET"):
    headers = {"User-Agent": "hrrr-portal"}
    if rng:
        headers["Range"] = "bytes=%d-%s" % (rng[0], "" if rng[1] is None else rng[1])
    req = urllib.request.Request(url, headers=headers, method=method)
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            # a 503 Slow Down from a public bucket means "ask again"
            if e.code not in (500, 503) or attempt == 4:
                raise
            time.sleep(0.8 * (attempt + 1))


def _index_url(url):
    if url.startswith(ECMWF_HOST):
        return url[:-len(".grib2")] + ".index"
    return url + ".idx"


@functools.lru_cache(maxsize=64)
def _idx(url):
    """
    [(start_byte, end_byte, line)] for every message in a GRIB file, from its
    sidecar. NCEP's is wgrib2 text ("n:start:d=...:VAR:level:..."), where a
    message ends where the next begins; ECMWF's is a JSON object per message
    with its offset and length, turned here into ":levtype:param:level:" so
    one regex search serves both.
    """
    try:
        text = _http(_index_url(url), timeout=30).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            raise RuntimeError("that forecast hour is not posted yet")
        raise
    if url.startswith(ECMWF_HOST):
        import json
        rows = []
        for line in text.splitlines():
            if line.strip():
                j = json.loads(line)
                rows.append((j["_offset"], j["_offset"] + j["_length"] - 1,
                             ":%s:%s:%s:" % (j.get("levtype"), j.get("param"), j.get("levelist", ""))))
        return rows
    raw = []
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[1].isdigit():
            raw.append((int(parts[1]), line))
    starts = sorted({st for st, _ in raw})
    # NAM and RAP pack U and V into one message (lines n.1 and n.2 share a
    # start byte), so the end is the next start that lies further on.
    nxt = {st: (starts[i + 1] - 1 if i + 1 < len(starts) else None) for i, st in enumerate(starts)}
    return [(st, nxt[st], line) for st, line in raw]


def _exists(url):
    try:
        _http(url, timeout=15, method="HEAD")
        return True
    except Exception:
        return False


def _locate(url, search):
    """Byte range of the first message whose index line matches `search`."""
    pat = re.compile(search)
    for start, end, line in _idx(url):
        if pat.search(line):
            return start, end
    raise RuntimeError("field not in this file (%s)" % search.strip(":"))


@functools.lru_cache(maxsize=4)
def _message(model, url, start, end):
    """
    Download and decode one GRIB message: ([arrays], lat2d, lon2d). Usually one
    array; two when the message carries a U/V pair.
    """
    return _decode(model, _http(url, rng=(start, end)))


@_locked
def _decode(model, raw):
    import cfgrib
    fd, path = tempfile.mkstemp(suffix=".grib2", dir=CACHE_DIR)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        arrays, lat, lon = [], None, None
        for ds in cfgrib.open_datasets(path, backend_kwargs={"indexpath": ""}):
            for name in ds.data_vars:
                arrays.append(np.asarray(ds[name].values, dtype="float32"))
            if lat is None:
                lat = np.asarray(ds.latitude.values, dtype="float32")
                lon = np.asarray(ds.longitude.values, dtype="float32")
            ds.close()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if not arrays:
        raise RuntimeError("could not decode the GRIB message")
    lon = np.where(lon > 180, lon - 360, lon)  # 0..360 -> -180..180

    if lat.ndim == 1:
        # A regular lat/lon grid (GFS) covers the planet. Keep the part the
        # portal draws, with a margin, and give it the 2-D coordinates the
        # Lambert grids already have.
        w, e, s, n = EXTENT
        jj = np.where((lat >= s - 3) & (lat <= n + 3))[0]
        ii = np.where((lon >= w - 3) & (lon <= e + 3))[0]
        ii = ii[np.argsort(lon[ii])]
        arrays = [a[np.ix_(jj, ii)] for a in arrays]
        lon, lat = np.meshgrid(lon[ii], lat[jj])
    if model not in _GRID:
        _GRID[model] = (lat.ravel(), lon.ravel())
    return arrays, lat, lon


# Each model's horizontal grid is identical across every field and forecast
# hour, so the flattened lat/lon is captured once (from the first fetch) and
# reused for all point lookups instead of carrying coords around per query.
_GRID = {}


def _one(model, url, search):
    start, end = _locate(url, search)
    return _message(model, url, start, end)


def _accumulate(f, model, run_dt, fxx):
    """
    A run total for a model that only posts per-frame buckets: NAM resets its
    accumulations every 3 hours, and GFS has no snowfall field at all, so its
    snow is each 6-hour precipitation bucket times the share of that window it
    was snowing. Every frame's running total is kept on disk, so stepping
    through a loop costs one frame of downloads, not the whole sum again.
    """
    from concurrent.futures import ThreadPoolExecutor
    step = MODELS[model]["step"]
    tag = hashlib.md5(("%s|%s|%s" % (f["search"], f.get("weight"), RENDER_REV)).encode()).hexdigest()[:8]

    def memo(h):
        return os.path.join(CACHE_DIR, "acc_%s_%s_%s_%03d.npz" % (
            model, tag, run_dt.strftime("%Y%m%d%H"), h))

    have, total = 0, None
    for h in range(fxx, 0, -step):
        if os.path.exists(memo(h)):
            try:
                total, have = np.load(memo(h))["a"].astype("float32"), h
                break
            except Exception:
                pass                                   # half-written: rebuild it
    todo = list(range(have + step, fxx + 1, step))

    def pull(h):
        # downloads only; eccodes decodes one at a time, back on this thread
        url = _grib_url(model, f.get("product", "sfc"), run_dt, h)
        fmt = dict(f=h, p=h - step)
        return [_http(url, rng=_locate(url, f[k].format(**fmt)))
                for k in ("search", "weight") if f.get(k)]

    lat = lon = None
    with ThreadPoolExecutor(6) as ex:
        for h, raws in zip(todo, ex.map(pull, todo)):
            arrs, lat, lon = _decode(model, raws[0])
            part = np.nan_to_num(arrs[0])
            if len(raws) > 1:
                part = part * np.nan_to_num(_decode(model, raws[1])[0][0])
            total = part if total is None else total + part
            tmp = part_name(memo(h))   # two drawers may share a total
            with open(tmp, "wb") as fh:
                np.savez_compressed(fh, a=total.astype("float16"))
            os.replace(tmp, memo(h))
    if lat is None:                                    # everything came off disk
        _, lat, lon = _one(model, _grib_url(model, f.get("product", "sfc"), run_dt, fxx),
                           f["search"].format(f=fxx, p=fxx - step))
    _prune()
    return total, lat, lon


_pruned = [0.0]


def _prune(max_age=36 * 3600):
    """Running totals belong to one run; drop them once it is two runs stale."""
    now = time.time()
    if now - _pruned[0] < 3600:
        return
    _pruned[0] = now
    for name in os.listdir(CACHE_DIR):
        if name.startswith("acc_") or name.endswith((".part", ".grib2")):
            path = os.path.join(CACHE_DIR, name)
            try:
                if now - os.path.getmtime(path) > max_age:
                    os.remove(path)
            except OSError:
                pass


def _radar_ptype(model, run_dt, fxx):
    """
    Reflectivity and precipitation type in one array: group * 100 + dBZ, where
    group is 0 rain, 1 wintry mix, 2 snow, and cells under 5 dBZ are missing.
    Echo with no type flagged (it is not reaching the ground, or the model
    simply did not say) is called snow where it is freezing at 2 m, else rain.
    """
    url = _grib_url(model, "sfc", run_dt, fxx)
    if model == "ecmwf":
        # Marshall-Palmer, Z = 200 R^1.6, turns a rain rate into a radar echo
        rate = _one(model, url, ":sfc:tprate:")[0][0] * 3600.0
        dbz = 10.0 * np.log10(200.0 * np.maximum(rate, 1e-4) ** 1.6)
    else:
        dbz = _one(model, url, ":REFC:")[0][0]
    kind, lat, lon = _fetch(spec("ptype", model), model, run_dt, fxx)
    t2m = _one(model, url, spec("t2m", model)["search"])[0][0]
    group = np.where(kind == 4, 2.0, np.where((kind == 2) | (kind == 3), 1.0, 0.0))
    group = np.where((kind == 0) & (t2m < 273.65), 2.0, group)
    code = group * 100.0 + np.clip(dbz, 0, 99)
    return np.where(np.isfinite(dbz) & (dbz >= RP_LEVELS[0]), code, np.nan).astype("float32"), lat, lon


def _rp_index(dbz, group):
    """Colour index into RP_COLORS (group * 6 + intensity step); NaN under 5 dBZ."""
    step = np.digitize(np.nan_to_num(dbz), RP_LEVELS) - 1
    out = np.asarray(group, dtype="float32") * len(RP_LEVELS) + step
    out[(step < 0) | ~np.isfinite(dbz)] = np.nan
    return out


def _fetch(f, model, run_dt, fxx):
    """(data, lat, lon) for a field spec, raw units, on the model's grid."""
    if f.get("radar_ptype"):
        return _radar_ptype(model, run_dt, fxx)
    if f.get("accum"):
        return _accumulate(f, model, run_dt, fxx)
    url = _grib_url(model, f.get("product", "sfc"), run_dt, fxx)
    fmt = dict(f=fxx, p=fxx - MODELS[model]["step"])
    if f.get("ptype") == "wmo":
        arrs, lat, lon = _one(model, url, f["search"])
        out = np.zeros_like(arrs[0])
        for code, ours in WMO_PTYPE.items():
            out = np.where(np.rint(arrs[0]) == code, float(ours), out)
        return out, lat, lon
    if f.get("ptype"):
        # rain, freezing rain, sleet, snow: later flags win where two are set
        out = lat = lon = None
        for code, var in enumerate(("CRAIN", "CFRZR", "CICEP", "CSNOW"), start=1):
            arrs, lat, lon = _one(model, url, ":%s:surface:%s" % (var, INST))
            if out is None:
                out = np.zeros_like(arrs[0])
            out = np.where(arrs[0] > 0.5, float(code), out)
        return out, lat, lon
    arrs, lat, lon = _one(model, url, f["search"].format(**fmt))
    if f.get("wind"):
        if len(arrs) >= 2:
            u, v = arrs[0], arrs[1]
        else:
            u = arrs[0]
            v = _one(model, url, f["search_v"].format(**fmt))[0][0]
        return np.hypot(u, v), lat, lon
    if f.get("diff") and fmt["p"] > 0:
        prev = _grib_url(model, f.get("product", "sfc"), run_dt, fmt["p"])
        return np.maximum(arrs[0] - _one(model, prev, f["search"])[0][0], 0), lat, lon
    if f.get("weight"):
        return arrs[0] * _one(model, url, f["weight"].format(**fmt))[0][0], lat, lon
    return arrs[0], lat, lon


def _field(field_key, model, run_dt, fxx):
    """Fetch + unit-convert, with the forecast-hour check both renderers need."""
    f = spec(field_key, model)
    m = MODELS[model]
    if fxx < f["min_fxx"]:
        raise ValueError("%s needs forecast hour >= %d" % (field_key, f["min_fxx"]))
    if fxx > m["max_fxx"] or fxx % m["step"]:
        raise ValueError("%s frames are every %d h out to %d" % (m["label"], m["step"], m["max_fxx"]))
    data, lat, lon = _fetch(f, model, run_dt, fxx)
    if f["conv"]:
        data = f["conv"](data)
    return f, data, lat, lon


# ---------------------------------------------------------------- run helper -
_RUNS = {}          # model -> (checked_at, run_dt)


def latest_available_run(model="hrrr", max_age=240):
    """
    Probe AWS for the newest run actually posted, walking back cycle by cycle.
    Falls back to a guess if nothing is found (offline / transient). The answer
    is kept for a few minutes: /api/config asks on every page load.
    """
    hit = _RUNS.get(model)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    m = MODELS[model]
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    base -= timedelta(hours=base.hour % m["cycle"])
    found = None
    for b in range(0 if m["cycle"] > 1 else 1, m["back"] + 1):
        r = base - timedelta(hours=b * m["cycle"])
        if _exists(_index_url(_grib_url(model, "sfc", r, m["probe"]))):
            found = r
            break
    if found is None:
        return base - timedelta(hours=2 * m["cycle"])      # not cached: ask again
    _RUNS[model] = (time.time(), found)
    return found


# ---- point sampling (click-to-probe & meteograms) --------------------------
@functools.lru_cache(maxsize=24)
def _grid_values(field_key, run_dt, fxx, model="hrrr"):
    """Flattened, unit-converted values for one field/run/hour (in-process cache)."""
    _, data, _, _ = _field(field_key, model, run_dt, fxx)
    return np.asarray(data, dtype="float32").ravel()


def sample(field_key, run_dt, fxx, lat0, lon0, model="hrrr"):
    """
    Nearest-grid-point value of a field at (lat0, lon0). Returns a float, or None
    if the point is off-grid / missing. Reuses whatever the render path already
    downloaded, so it is fast once a field/hour has been viewed or animated.
    """
    f = spec(field_key, model)
    if fxx < f["min_fxx"]:
        return None
    try:
        vals = _grid_values(field_key, run_dt, fxx, model)  # also populates _GRID
    except Exception:
        return None                                  # one bad hour ≠ dead meteogram
    if model not in _GRID:
        return None
    latf, lonf = _GRID[model]
    d = (latf - lat0) ** 2 + (lonf - lon0) ** 2
    i = int(np.argmin(d))
    v = vals[i]
    if f.get("radar_ptype") and math.isfinite(v):
        v = v % 100                                  # the echo, without the type
    return None if not math.isfinite(v) else round(float(v), 2)


def _clear(color):
    """
    The same colour at zero alpha. Gouraud blends towards whatever "under" is,
    so transparent black leaves a grey fringe round every patch of snow or rain.
    """
    from matplotlib.colors import to_rgba
    r, g, b, _ = to_rgba(color)
    return (r, g, b, 0.0)


def _cmap_norm(f):
    if f.get("cmap") == "_nws":
        return _nws_reflectivity()
    if f.get("radar_ptype"):
        cmap = ListedColormap(RP_COLORS)
        cmap.set_bad((0, 0, 0, 0))
        return cmap, BoundaryNorm(range(len(RP_COLORS) + 1), cmap.N)
    if f.get("levels"):
        levels, colors = f["levels"], f["colors"]
        if len(colors) == len(levels):               # last colour = past the top
            cmap = ListedColormap(colors[:-1])
            cmap.set_over(colors[-1])
        else:
            cmap = ListedColormap(colors)
            cmap.set_over(colors[-1])
        cmap.set_under(_clear(colors[0]))
        return cmap, BoundaryNorm(levels, cmap.N)
    cmap = plt.get_cmap(f["cmap"]).copy()
    if f.get("mask_under"):
        # clean background: values under vmin drop out (precip, smoke, echo top…)
        cmap.set_under(_clear(cmap(0.0)))
        norm = Normalize(vmin=f["vmin"], vmax=f["vmax"], clip=False)
    else:
        # continuous field: clamp extremes to the end colors, no hole punched
        cmap.set_under(cmap(0.0))
        cmap.set_over(cmap(1.0))
        norm = Normalize(vmin=f["vmin"], vmax=f["vmax"], clip=True)
    return cmap, norm


def _shading(f):
    # Blending between categories would invent sleet along every rain/snow line.
    return "nearest" if f.get("cats") or f.get("radar_ptype") else "gouraud"


def _cache_path(*parts):
    key = hashlib.md5(("|".join(str(p) for p in parts) + "|" + RENDER_REV).encode()).hexdigest()[:16]
    return os.path.join(CACHE_DIR, "%s.png" % key)


# ---------------------------------------------------------------- main render
def render(field_key, run_dt, fxx, model="hrrr"):
    """
    Return path to a cached PNG for (model, field, run, forecast-hour), rendering
    if it does not already exist. Raises ValueError / RuntimeError on bad input
    or fetch failure.
    """
    spec(field_key, model)                             # validate before touching disk
    out_path = _cache_path(model, field_key, run_dt.strftime("%Y%m%d%H"), "%03d" % fxx)
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path
    f, data, lat, lon = _field(field_key, model, run_dt, fxx)
    _draw(f, data, lat, lon, out_path, MODELS[model]["stride"])
    return out_path


@_locked
def _draw(f, data, lat, lon, out_path, stride):
    cmap, norm = _cmap_norm(f)
    w, e, s, n = EXTENT
    px_w, px_h = _img_size()
    dpi = 100

    fig = plt.figure(figsize=(px_w / dpi, px_h / dpi), dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1], projection=ccrs.epsg(3857))
    ax.set_extent([w, e, s, n], crs=ccrs.PlateCarree())
    ax.set_aspect("auto")           # fill the frame; aspect already matched
    ax.set_axis_off()

    # downsample the grid for rendering — far fewer triangles for gouraud to
    # rasterize (the expensive part), smoothed back over the gaps by the shading.
    if f.get("radar_ptype"):
        data = _rp_index(data % 100, data // 100)
    d = data[::stride, ::stride]
    la = lat[::stride, ::stride]
    lo = lon[::stride, ::stride]
    field = np.ma.masked_invalid(d)     # ocean / off-grid stays transparent
    # Gouraud shading interpolates color smoothly between grid points — the smooth
    # "pro" look (like Pivotal), without pcolormesh's hard cell squares.
    ax.pcolormesh(lo, la, field, transform=ccrs.PlateCarree(),
                  cmap=cmap, norm=norm, shading=_shading(f))

    tmp = part_name(out_path)            # two workers may render the same frame
    fig.savefig(tmp, format="png", transparent=True, dpi=dpi,
                pad_inches=0, bbox_inches=None)
    plt.close(fig)
    os.replace(tmp, out_path)


def _colorbar(f, ax, tick_color, edge_color, size):
    cmap, norm = _cmap_norm(f)
    banded = bool(f.get("levels")) and not f.get("cats")
    if f.get("radar_ptype"):
        cb = matplotlib.colorbar.ColorbarBase(ax, cmap=cmap, norm=norm, orientation="horizontal")
        n = len(RP_LEVELS)
        cb.set_ticks([n * g + n / 2.0 for g in range(len(RP_GROUPS))])
        cb.set_ticklabels(["%s, light to heavy" % g for g in RP_GROUPS])
        cb.ax.tick_params(labelsize=size, colors=tick_color, length=0)
        cb.outline.set_edgecolor(edge_color)
        return cb
    cb = matplotlib.colorbar.ColorbarBase(
        ax, cmap=cmap, norm=norm, orientation="horizontal",
        extend="max" if banded else "neither")
    if f.get("cats"):
        cb.set_ticks(range(1, len(f["cats"]) + 1))
        cb.set_ticklabels(f["cats"])
    elif banded:
        cb.set_ticks(f["levels"])
        cb.set_ticklabels(["%g" % v for v in f["levels"]])
    cb.ax.tick_params(labelsize=size, colors=tick_color, length=2)
    cb.outline.set_edgecolor(edge_color)
    return cb


@_locked
def render_legend(field_key, model="hrrr"):
    """Return PNG bytes of a horizontal colorbar for the given field."""
    f = spec(field_key, model)
    fig = plt.figure(figsize=(3.0, 0.55), dpi=110)
    ax = fig.add_axes([0.04, 0.45, 0.92, 0.5])
    many = len(f.get("levels") or []) > 9
    _colorbar(f, ax, "#cfe0f2", "#33465e", 5.5 if many else 7)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    return buf.getvalue()


# ---------------------------------------------------------------- static maps
# A finished picture: basemap, borders, title, city values, colorbar. Meant to
# be saved and posted, so it carries everything needed to read it on its own.
#   box : W, E, S, N    tz : the zone the valid time is also written in
REGIONS = {
    "conus":       dict(label="United States",    box=(-125.0, -66.5, 23.0, 50.0), tz="America/New_York"),
    "northeast":   dict(label="Northeast",        box=(-83.0, -66.5, 38.5, 48.0),  tz="America/New_York"),
    "newyork":     dict(label="New York State",   box=(-80.3, -71.4, 40.3, 45.3),  tz="America/New_York"),
    "wny":         dict(label="Western New York", box=(-81.6, -75.4, 41.4, 44.5),  tz="America/New_York"),
    "greatlakes":  dict(label="Great Lakes",      box=(-93.5, -74.0, 40.0, 50.0),  tz="America/New_York"),
    "midatlantic": dict(label="Mid-Atlantic",     box=(-84.5, -72.0, 35.0, 42.5),  tz="America/New_York"),
    "southeast":   dict(label="Southeast",        box=(-92.5, -75.0, 24.0, 37.5),  tz="America/New_York"),
    "midwest":     dict(label="Midwest",          box=(-98.0, -80.0, 36.0, 49.5),  tz="America/Chicago"),
    "plains":      dict(label="Northern Plains",  box=(-106.0, -90.0, 39.0, 49.5), tz="America/Chicago"),
    "southplains": dict(label="Southern Plains",  box=(-107.0, -88.5, 25.5, 40.0), tz="America/Chicago"),
    "rockies":     dict(label="Rockies",          box=(-117.5, -101.0, 35.0, 49.5), tz="America/Denver"),
    "northwest":   dict(label="Northwest",        box=(-125.0, -108.5, 40.5, 49.5), tz="America/Los_Angeles"),
    "southwest":   dict(label="Southwest",        box=(-125.0, -105.0, 31.0, 42.5), tz="America/Los_Angeles"),
}

# Cities that get their value printed, in priority order: when two would
# collide on a given map the earlier one stays.
CITIES = [
    ("Buffalo", 42.89, -78.88), ("New York", 40.71, -74.01), ("Chicago", 41.88, -87.63),
    ("Los Angeles", 34.05, -118.24), ("Dallas", 32.78, -96.80), ("Denver", 39.74, -104.99),
    ("Atlanta", 33.75, -84.39), ("Seattle", 47.61, -122.33), ("Miami", 25.76, -80.19),
    ("Minneapolis", 44.98, -93.27), ("Boston", 42.36, -71.06), ("Washington", 38.91, -77.04),
    ("Houston", 29.76, -95.37), ("Phoenix", 33.45, -112.07), ("Kansas City", 39.10, -94.58),
    ("Salt Lake City", 40.76, -111.89), ("Rochester", 43.16, -77.61), ("Syracuse", 43.05, -76.15),
    ("Albany", 42.65, -73.76), ("Pittsburgh", 40.44, -80.00), ("Cleveland", 41.50, -81.69),
    ("Detroit", 42.33, -83.05), ("Toronto", 43.65, -79.38), ("Philadelphia", 39.95, -75.17),
    ("Erie", 42.13, -80.09), ("Watertown", 43.97, -75.91), ("Binghamton", 42.10, -75.91),
    ("Jamestown", 42.10, -79.24), ("Niagara Falls", 43.09, -79.06), ("Ithaca", 42.44, -76.50),
    ("Old Forge", 43.71, -74.97), ("Lake Placid", 44.28, -73.98), ("Plattsburgh", 44.70, -73.45),
    ("Burlington", 44.48, -73.21), ("Portland ME", 43.66, -70.26), ("Hartford", 41.76, -72.67),
    ("Scranton", 41.41, -75.66), ("Montreal", 45.50, -73.57), ("Ottawa", 45.42, -75.70),
    ("Columbus", 39.96, -83.00), ("Cincinnati", 39.10, -84.51), ("Indianapolis", 39.77, -86.16),
    ("Milwaukee", 43.04, -87.91), ("Green Bay", 44.51, -88.01), ("Marquette", 46.54, -87.40),
    ("Duluth", 46.79, -92.10), ("Grand Rapids", 42.96, -85.67), ("St. Louis", 38.63, -90.20),
    ("Des Moines", 41.59, -93.62), ("Omaha", 41.26, -95.93), ("Fargo", 46.88, -96.79),
    ("Bismarck", 46.81, -100.78), ("Rapid City", 44.08, -103.23), ("Oklahoma City", 35.47, -97.52),
    ("Wichita", 37.69, -97.34), ("Amarillo", 35.22, -101.83), ("San Antonio", 29.42, -98.49),
    ("New Orleans", 29.95, -90.07), ("Memphis", 35.15, -90.05), ("Nashville", 36.16, -86.78),
    ("Little Rock", 34.75, -92.29), ("Birmingham", 33.52, -86.80), ("Charlotte", 35.23, -80.84),
    ("Raleigh", 35.78, -78.64), ("Richmond", 37.54, -77.44), ("Norfolk", 36.85, -76.29),
    ("Charleston", 32.78, -79.93), ("Jacksonville", 30.33, -81.66), ("Tampa", 27.95, -82.46),
    ("Orlando", 28.54, -81.38), ("Albuquerque", 35.08, -106.65), ("El Paso", 31.76, -106.49),
    ("Las Vegas", 36.17, -115.14), ("San Francisco", 37.77, -122.42), ("Sacramento", 38.58, -121.49),
    ("San Diego", 32.72, -117.16), ("Portland", 45.52, -122.68), ("Spokane", 47.66, -117.43),
    ("Boise", 43.62, -116.20), ("Billings", 45.78, -108.50), ("Missoula", 46.87, -113.99),
    ("Cheyenne", 41.14, -104.82), ("Reno", 39.53, -119.81), ("Flagstaff", 35.20, -111.65),
]

# Laid out as a 16 x 10 inch sheet and saved at 150 dpi: 2400 x 1500 pixels,
# enough that the type and county lines stay sharp when a phone zooms in.
MAP_W, MAP_H, MAP_DPI = 2400, 1500, 150
# The look of the sheet. Bump when it changes; every static map is then redrawn
# (overlay PNGs for the live map are keyed without it and stay).
MAP_REV = "s4"
WATER, LAND, INK = "#c9d8e4", "#efece5", "#2a2f36"

# Named on regional maps, where a lake is half the picture.
WATERS = [("Lake Erie", 42.15, -81.25), ("Lake Ontario", 43.63, -77.80), ("Lake Huron", 44.75, -82.45),
          ("Georgian Bay", 45.30, -80.85), ("Lake Michigan", 43.90, -87.05), ("Lake Superior", 47.60, -87.60),
          ("Atlantic Ocean", 40.2, -70.2), ("Gulf of Maine", 43.2, -68.6)]
_roads = {}             # major -> [(bounds, geometry)]


def _road_shapes(w, e, s, n, major_only):
    """Highways touching a lat/lon box, from Natural Earth. No file, no roads."""
    if not _roads:
        try:
            from cartopy.io import shapereader
            path = shapereader.natural_earth(resolution="10m", category="cultural", name="roads")
            big, rest = [], []
            for r in shapereader.Reader(path).records():
                kind = r.attributes.get("type") or ""
                if r.geometry is None or kind not in ("Major Highway", "Secondary Highway"):
                    continue
                (big if kind == "Major Highway" else rest).append(r.geometry.bounds + (r.geometry,))
            _roads[True], _roads[False] = big, big + rest
        except Exception:
            return []
    return [g for x0, y0, x1, y1, g in _roads[bool(major_only)]
            if x1 > w and x0 < e and y1 > s and y0 < n]
_MAP_AX = [0.0, 0.072, 1.0, 0.838]          # the map itself, as a figure fraction

# The same map in other shapes, for where it is going to be posted. Each is a
# sheet size in pixels (at MAP_DPI) and where the map sits on it. A shape other
# than wide frames the region differently, so it is a region key of its own
# inside ('wny@sq', 'wny@pt'), with its own basemap and relief.
#   square : 1:1, for a feed. Laid out like the wide sheet.
#   tall   : 9:16, for a story or reel. Dark bands top and bottom, like the
#            animated loop's, with the text set from the left.
SHAPES = {
    "wide":   dict(w=MAP_W, h=MAP_H, ax=_MAP_AX, key=""),
    "square": dict(w=2160, h=2160, ax=[0.0, 0.064, 1.0, 0.868], key="@sq"),
    "tall":   dict(w=1620, h=2880, ax=[0.0, 0.144, 1.0, 0.712], key="@pt"),
}


def region_catalog():
    return [dict(key=k, label=r["label"]) for k, r in REGIONS.items()]


def _fmt_value(v, f):
    if abs(v) >= 100:
        return "%d" % round(v)
    if abs(v) >= 10 or f["unit"] in ("°F", "°C", "dBZ", "%", "mph", "kt"):
        return "%d" % round(v) if f["unit"] != "in" else "%.1f" % v
    return ("%.2f" % v) if abs(v) < 1 and f["unit"] == "in" else "%.1f" % v


def _clock(dt, tz):
    """'4 PM EDT Mon Oct 12' in the region's own zone."""
    from zoneinfo import ZoneInfo
    local = dt.astimezone(ZoneInfo(tz))
    return "%s %s" % (local.strftime("%I %p %Z").lstrip("0"), local.strftime("%a %b %-d"))


def _when(run_dt, fxx, tz):
    """'Valid 4 PM EDT Mon Oct 12 · Hour 6 of the 10 AM EDT Mon Oct 12 run'"""
    return "Valid %s  ·  Hour %d of the %s run" % (
        _clock(run_dt + timedelta(hours=fxx), tz), fxx, _clock(run_dt, tz))


COUNTY_ZIP = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_county_5m.zip"
_counties = []          # [(minx, miny, maxx, maxy, geometry)], loaded once


def _county_shapes(w, e, s, n):
    """
    County outlines touching a lat/lon box, from the Census cartographic
    boundary file (fetched once into cache/geo). Counties are what make a
    regional map readable to the people who live in it. No file, no counties:
    the map still draws.
    """
    if not _counties:
        try:
            import zipfile
            from cartopy.io import shapereader
            geo = os.path.join(CACHE_DIR, "geo")
            shp = os.path.join(geo, "cb_2023_us_county_5m.shp")
            if not os.path.exists(shp):
                os.makedirs(geo, exist_ok=True)
                zipfile.ZipFile(io.BytesIO(_http(COUNTY_ZIP, timeout=60))).extractall(geo)
            for g in shapereader.Reader(shp).geometries():
                _counties.append(g.bounds + (g,))
        except Exception:
            return []
    return [g for x0, y0, x1, y1, g in _counties
            if x1 > w and x0 < e and y1 > s and y0 < n]


def _bands(f):
    """(levels, colors, extend) if the field is drawn in bands, else None."""
    if f.get("cats"):
        return None
    if f.get("cmap") == "_nws":
        cmap, norm = _nws_reflectivity()
        return list(norm.boundaries), list(cmap.colors), "neither"
    if f.get("levels"):
        return f["levels"], f["colors"], "max"
    return None


def _smooth(a, passes=1):
    """
    Repeated 3x3 means, which add up to a Gaussian of sigma = sqrt(2/3 * passes)
    cells. Missing cells are ignored and stay missing; the shape is unchanged.
    """
    ok = np.isfinite(a)
    out = np.where(ok, a, 0.0).astype("float32")
    wt = ok.astype("float32")
    n, m = a.shape
    for _ in range(passes):
        v, c = np.pad(out * wt, 1, mode="edge"), np.pad(wt, 1, mode="edge")
        tot = sum(v[i:i + n, j:j + m] for i in range(3) for j in range(3))
        cnt = sum(c[i:i + n, j:j + m] for i in range(3) for j in range(3))
        out = tot / np.maximum(cnt, 1e-6)
    out[~ok] = np.nan
    return out


def _upsample(a, k):
    """Bilinear resample to k times the cells along each axis (numpy only)."""
    if k <= 1:
        return a
    n, m = a.shape
    yi, xi = np.linspace(0, n - 1, (n - 1) * k + 1), np.linspace(0, m - 1, (m - 1) * k + 1)
    y0 = np.clip(np.floor(yi).astype(int), 0, n - 2)
    x0 = np.clip(np.floor(xi).astype(int), 0, m - 2)
    fy, fx = (yi - y0)[:, None], (xi - x0)[None, :]
    top, bot = a[y0], a[y0 + 1]
    return (top[:, x0] * (1 - fy) * (1 - fx) + bot[:, x0] * fy * (1 - fx)
            + top[:, x0 + 1] * (1 - fy) * fx + bot[:, x0 + 1] * fy * fx)


def map_path(model, field_key, run_dt, fxx, region, shape="wide"):
    """Where a static map lives in the cache, drawn or not."""
    return _cache_path("map", MAP_REV, model, field_key, run_dt.strftime("%Y%m%d%H"),
                       "%03d" % fxx, region + SHAPES[shape]["key"])


def render_map(field_key, run_dt, fxx, region="conus", model="hrrr", shape="wide"):
    """Path to a cached static map PNG, drawing it if need be."""
    if region not in REGIONS:
        raise ValueError("unknown region %r" % region)
    if shape not in SHAPES:
        raise ValueError("shape is one of %s" % ", ".join(SHAPES))
    spec(field_key, model)
    out_path = map_path(model, field_key, run_dt, fxx, region, shape)
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path
    f, data, lat, lon = _field(field_key, model, run_dt, fxx)
    _draw_map(f, data, lat, lon, out_path, model, run_dt, fxx, region, shape)
    return out_path


def recent_runs(model, count=None):
    """
    The newest posted run and the ones before it, newest first, each with how
    many of its static maps are already drawn: what a run picker lists. Earlier
    runs are not probed; NOAA and ECMWF keep them far longer than this reaches.
    """
    m = MODELS[model]
    count = count or {1: 12, 6: 6, 12: 4}.get(m["cycle"], 6)
    latest = latest_available_run(model)
    have = set(os.listdir(CACHE_DIR))
    out = []
    for k in range(count):
        r = latest - timedelta(hours=k * m["cycle"])
        ready = ready_maps(model, r, have)
        out.append(dict(run=r.strftime("%Y%m%d%H"), iso=r.strftime("%Y-%m-%dT%H:00:00Z"),
                        ready=sum(len(h) for f in ready.values() for h in f.values())))
    return out


def ready_maps(model, run_dt, have=None):
    """
    {field: {region: [hours]}} for the static maps of one run that are already
    drawn, so the page can show what opens at once. One directory listing and a
    hash per candidate; nothing is fetched or rendered.
    """
    if have is None:
        have = set(os.listdir(CACHE_DIR))
    m = MODELS[model]
    out = {}
    for f in field_catalog(model):
        for region in REGIONS:
            hours = [h for h in range(f["min_fxx"], m["max_fxx"] + 1, m["step"])
                     if os.path.basename(map_path(model, f["key"], run_dt, h, region)) in have]
            if hours:
                out.setdefault(f["key"], {})[region] = hours
    return out


BASE_REV = "b2"
_BASE = {}              # region -> {layer: [geometries in map coordinates]}


@_locked
def _basemap(region, proj, x0, x1, y0, y1):
    """
    Land, lakes, borders, roads and counties for one region, already projected
    into the map's coordinates and clipped to its frame.

    Projecting a continent's coastline is by far the slowest step of a map, and
    a region's frame never changes, so it is done once and pickled. Every map
    after that, in every process, just draws the stored shapes.
    """
    if region in _BASE:
        return _BASE[region]
    import pickle
    path = os.path.join(CACHE_DIR, "geo", "base_%s_%s%s.pkl" % (
        region, BASE_REV, _framing(region)[0]))
    if os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                _BASE[region] = pickle.load(fh)
            return _BASE[region]
        except Exception:
            pass                                          # half-written: rebuild
    import cartopy.feature as cfeature
    import shapely.geometry as sg
    w, e, s, n = REGIONS[region_parts(region)[0]]["box"]
    pc = ccrs.PlateCarree()
    res = "10m" if (e - w) < 14 else "50m"
    regional, close = (e - w) < 22, (e - w) < 10
    view = sg.box(x0 - (x1 - x0) * 0.02, y0 - (y1 - y0) * 0.02,
                  x1 + (x1 - x0) * 0.02, y1 + (y1 - y0) * 0.02)
    lw, le, ls, ln = w - 9, e + 9, s - 6, n + 6           # the frame is wider than the box

    def layer(geoms):
        out = []
        for g in geoms:
            if g is None:
                continue
            b = g.bounds
            if b[2] < lw or b[0] > le or b[3] < ls or b[1] > ln:
                continue
            try:
                p = proj.project_geometry(g, pc).intersection(view)
            except Exception:
                continue
            if not p.is_empty:
                out.extend(q for q in getattr(p, "geoms", [p])
                           if not q.is_empty and q.geom_type != "Point")
        return out

    ne = lambda cat, name: cfeature.NaturalEarthFeature(cat, name, res).geometries()
    base = dict(
        land=layer(ne("physical", "land")),
        lakes=layer(ne("physical", "lakes")),
        coast=layer(ne("physical", "coastline")),
        states=layer(ne("cultural", "admin_1_states_provinces_lines")),
        borders=layer(ne("cultural", "admin_0_boundary_lines_land")),
        roads=layer(_road_shapes(lw, le, ls, ln, major_only=not close)),
        counties=layer(_county_shapes(lw, le, ls, ln)) if regional else [],
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = part_name(path)
    with open(tmp, "wb") as fh:
        pickle.dump(base, fh, protocol=4)
    os.replace(tmp, path)
    _BASE[region] = base
    return base


# The upright (reel) frame: the map fills the whole 1080 x 1920 video. (A
# version that kept the map to the clear middle, between solid title and footer
# bands, wasted a third of the screen and squashed the map; it was thrown out.)
TALL_W, TALL_H = 1080, 1920
TALL = float(TALL_W) / TALL_H
TALL_TAG = "_v3"           # in cache names of upright layers; bump when this frame changes


# how a region key may be framed, and what marks its cached layers (bump a mark
# when that frame changes)
FRAMINGS = {"": "", "tall": TALL_TAG, "sq": "_s1", "pt": "_p1"}


def region_parts(region):
    """
    ('wny', False) for 'wny', ('wny', True) for 'wny@tall': the same region
    framed upright for a phone, which the animated loop offers for reels.
    'wny@sq' and 'wny@pt' are the static maps' square and upright framings.
    Raises ValueError for a region that does not exist.
    """
    base, _, how = region.partition("@")
    if base not in REGIONS or how not in FRAMINGS:
        raise ValueError("unknown region %r" % region)
    return base, how == "tall"


def _framing(region):
    """(mark for cache names, map aspect or None for the wide frame)."""
    how = region.partition("@")[2]
    if how == "tall":
        return FRAMINGS[how], TALL
    for sh in SHAPES.values():
        if sh["key"] == "@" + how and how:
            return FRAMINGS[how], (sh["w"] * sh["ax"][2]) / (sh["h"] * sh["ax"][3])
    return "", None


@_locked
def frame(region):
    """
    (proj, x0, x1, y0, y1): the Lambert conformal fitted to a region and the
    limits of its map frame. The box is grown on one axis to the frame's shape,
    so the map fills it edge to edge instead of floating in a margin. The
    animated loop draws into this same frame, which is why its layers line up
    with the static maps'.
    """
    base, _ = region_parts(region)
    aspect = _framing(region)[1]
    w, e, s, n = REGIONS[base]["box"]
    pc = ccrs.PlateCarree()
    proj = ccrs.LambertConformal(
        central_longitude=(w + e) / 2, central_latitude=(s + n) / 2,
        standard_parallels=(s + (n - s) / 6, n - (n - s) / 6))
    t = np.linspace(0, 1, 40)
    edge_lon = np.concatenate([w + (e - w) * t, w + (e - w) * t, np.full(40, w), np.full(40, e)])
    edge_lat = np.concatenate([np.full(40, s), np.full(40, n), s + (n - s) * t, s + (n - s) * t])
    pts = proj.transform_points(pc, edge_lon, edge_lat)
    x0, x1, y0, y1 = pts[:, 0].min(), pts[:, 0].max(), pts[:, 1].min(), pts[:, 1].max()
    if aspect:
        # A frame squarer or taller than the region cannot hold all of it. Keep
        # most of the width, or about twice the height, whichever asks for
        # less, and centre it: the sides are trimmed and the map runs further
        # north and south than the landscape one.
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        h = min(0.85 * (x1 - x0) / aspect, 1.9 * (y1 - y0))
        return proj, cx - h * aspect / 2, cx + h * aspect / 2, cy - h / 2, cy + h / 2
    want = (MAP_W * _MAP_AX[2]) / (MAP_H * _MAP_AX[3])
    if (x1 - x0) / (y1 - y0) < want:
        grow = (y1 - y0) * want - (x1 - x0)
        x0, x1 = x0 - grow / 2, x1 + grow / 2
    else:
        grow = (x1 - x0) / want - (y1 - y0)
        y0, y1 = y0 - grow / 2, y1 + grow / 2
    return proj, x0, x1, y0, y1


@_locked
def city_labels(region, limit=22):
    """
    [(name, x, y, major)] for the cities a region's map has room for, in map
    coordinates: taken in priority order, dropping any that would collide.
    """
    proj, x0, x1, y0, y1 = frame(region)
    cxy = proj.transform_points(ccrs.PlateCarree(), np.array([c[2] for c in CITIES]),
                                np.array([c[1] for c in CITIES]))
    kept = []
    # a label block is about this wide (an upright frame is narrow, so there the
    # room is reckoned from its height)
    gap = 0.085 * max(x1 - x0, 0.9 * (y1 - y0))
    mx, my = (x1 - x0) * 0.03, (y1 - y0) * 0.04
    for rank, ((name, _, _), (cx, cy, _)) in enumerate(zip(CITIES, cxy)):
        if not (x0 + mx < cx < x1 - mx and y0 + my < cy < y1 - my):
            continue
        if any(abs(cx - k[1]) < gap and abs(cy - k[2]) < gap * 0.75 for k in kept):
            continue
        if len(kept) >= limit:
            break
        kept.append((name, cx, cy, rank < 16))    # the first cities listed lead
    return kept


@_locked
def water_labels(region, cities):
    """[(name, x, y)] for the big waters in a regional frame, clear of the cities."""
    w, e, _, _ = REGIONS[region_parts(region)[0]]["box"]
    if (e - w) >= 22:
        return []
    proj, x0, x1, y0, y1 = frame(region)
    gap = 0.085 * max(x1 - x0, 0.9 * (y1 - y0))
    wxy = proj.transform_points(ccrs.PlateCarree(), np.array([c[2] for c in WATERS]),
                                np.array([c[1] for c in WATERS]))
    out = []
    for (name, _, _), (cx, cy, _) in zip(WATERS, wxy):
        clear = not any(abs(cx - k[1]) < gap * 1.3 and abs(cy - k[2]) < gap * 0.6 for k in cities)
        if clear and x0 + (x1 - x0) * 0.06 < cx < x1 - (x1 - x0) * 0.06 \
                and y0 + (y1 - y0) * 0.05 < cy < y1 - (y1 - y0) * 0.05:
            out.append((name, cx, cy))
    return out


@_locked
def _draw_map(f, data, lat, lon, out_path, model, run_dt, fxx, region, shape="wide"):

    reg = REGIONS[region]
    w, e, s, n = reg["box"]
    pc = ccrs.PlateCarree()
    sheet = SHAPES[shape]
    region += sheet["key"]                    # the region as this shape frames it
    dark = shape == "tall"                    # bands like the loop's, light text
    proj, x0, x1, y0, y1 = frame(region)

    # Project the grid ourselves and hand matplotlib plain x/y: quicker than
    # letting cartopy transform the mesh, and nothing to wrap at this scale.
    xy = proj.transform_points(pc, lon.astype("float64"), lat.astype("float64"))
    gx, gy = xy[..., 0], xy[..., 1]
    padx, pady = (x1 - x0) * 0.04, (y1 - y0) * 0.04
    inview = (gx > x0 - padx) & (gx < x1 + padx) & (gy > y0 - pady) & (gy < y1 + pady)
    if not inview.any():
        raise ValueError("that region is outside the model's grid")
    rows, cols = np.where(inview.any(axis=1))[0], np.where(inview.any(axis=0))[0]
    sl = (slice(rows[0], rows[-1] + 1), slice(cols[0], cols[-1] + 1))
    gx, gy, vals = gx[sl], gy[sl], np.asarray(data[sl], dtype="float32")
    stride = max(1, int(math.ceil(max(vals.shape) / 1100.0)))

    cmap, norm = _cmap_norm(f)
    paper = "#0f1723" if dark else "white"
    fig = plt.figure(figsize=(sheet["w"] / MAP_DPI, sheet["h"] / MAP_DPI), dpi=MAP_DPI, facecolor=paper)
    ax = fig.add_axes(sheet["ax"], projection=proj)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_facecolor(WATER)                           # open water

    base = _basemap(region, proj, x0, x1, y0, y1)

    def put(name, **kw):
        if base[name]:
            ax.add_geometries(base[name], crs=proj, **kw)

    put("land", facecolor=LAND, edgecolor="none", zorder=1)
    put("lakes", facecolor=WATER, edgecolor="none", zorder=1)
    # Shaded terrain, firmly on the bare land here and again, faintly, over the
    # weather further down. A map without it still draws.
    try:
        import relief
        box = ax.get_window_extent()
        shade = relief.shade(region, proj, x0, x1, y0, y1, int(round(box.width)),
                             int(round(box.height)), CACHE_DIR)
        ax.imshow(relief.overlay(shade, 0.50, 0.75), extent=(x0, x1, y0, y1), origin="upper",
                  interpolation="nearest", zorder=1.5)
    except Exception:
        shade = None
    sx, sy, sv = gx[::stride, ::stride], gy[::stride, ::stride], vals[::stride, ::stride]
    # Smoothing is sized in pixels of the finished sheet, not in grid cells, so a
    # county-scale map and a national one come out equally soft: a Gaussian of
    # about 9 px, and never less than 1.5 cells. The printed city values and
    # the maximum stay raw.
    px_per_cell = sheet["w"] * (gx.max() - gx.min()) / (x1 - x0) / max(sv.shape[1], 1)
    sigma = max(1.5, 9.0 / max(px_per_cell, 0.1))
    passes = int(min(14, round(1.5 * sigma * sigma)))
    bands = _bands(f)
    if f.get("radar_ptype"):
        # Two things to smooth, and they cannot be smoothed as one number: the
        # echo, and which type owns each spot. The type is settled by smoothing
        # a yes/no layer per group and taking the strongest, so a rain/snow
        # line is a clean edge rather than a band of invented mix.
        has = np.isfinite(sv)
        dbz, grp = np.where(has, sv % 100, 0.0), np.where(has, sv // 100, -1)
        k = int(min(6, max(1, round(1500.0 / max(sv.shape)))))
        echo = _smooth(_upsample(_smooth(dbz, passes), k), 2 if k > 1 else 0)
        own = np.argmax(np.stack([_upsample(_smooth((grp == g).astype("float32"), passes), k)
                                  for g in range(len(RP_GROUPS))]), axis=0)
        ax.pcolormesh(_upsample(sx, k), _upsample(sy, k), np.ma.masked_invalid(_rp_index(echo, own)),
                      cmap=cmap, norm=norm, shading="nearest", zorder=2, rasterized=True)
    elif bands:
        # A banded scale wants real contours: shading a mesh leaves every band
        # edge stepped along the grid cells. The grid is also resampled finer
        # first, then smoothed once more, so a band edge is a curve rather than
        # a run of straight facets one cell long.
        levels, colors, extend = bands
        k = int(min(5, max(1, round(900.0 / max(sv.shape)))))
        sm = _smooth(_upsample(_smooth(sv, passes), k), 2 if k > 1 else 0)
        ax.contourf(_upsample(sx, k), _upsample(sy, k), np.ma.masked_invalid(sm),
                    levels=levels, colors=colors, extend=extend, antialiased=False, zorder=2)
    else:
        if not f.get("cats"):
            sv = _smooth(sv, max(1, passes // 2))
        ax.pcolormesh(sx, sy, np.ma.masked_invalid(sv),
                      cmap=cmap, norm=norm, shading=_shading(f), zorder=2, rasterized=True)
    if shade is not None:
        ax.imshow(relief.overlay(shade, 0.34, 0.26), extent=(x0, x1, y0, y1), origin="upper",
                  interpolation="nearest", zorder=2.5)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    line = INK
    regional = (e - w) < 22
    # highways: the lines people actually navigate by, under the borders
    # (secondary ones too, once the map is close enough to have room for them)
    close = (e - w) < 10
    put("roads", facecolor="none", edgecolor="#5a4632", linewidth=0.75 if close else 0.5,
        alpha=0.5 if close else 0.4, zorder=2.8)
    put("lakes", facecolor="none", edgecolor=line, linewidth=0.5, zorder=3)
    put("coast", facecolor="none", edgecolor=line, linewidth=0.6, zorder=3)
    put("states", facecolor="none", edgecolor=line, linewidth=0.55, zorder=3)
    put("borders", facecolor="none", edgecolor=line, linewidth=0.9, zorder=3)
    put("counties", facecolor="none", edgecolor=line, linewidth=0.3, alpha=0.32, zorder=3)
    for sp in ax.spines.values():
        sp.set_edgecolor("#2a2f36")
        sp.set_linewidth(0.8)

    halo = [pe.withStroke(linewidth=2.6, foreground="white")]

    # ---- city values ----
    cities = city_labels(region)
    for name, cx, cy, major in cities:
        i = np.unravel_index(int(np.argmin((gx - cx) ** 2 + (gy - cy) ** 2)), gx.shape)
        v = float(vals[i])
        ax.plot(cx, cy, "o", ms=4.2 if major else 3.0, color="#111", mec="white", mew=0.8, zorder=5)
        # no number where the field is blank there: under the bottom of a banded
        # or masked scale (no echo, no snow) a "-10" or "0.00" is only clutter
        blank = (f.get("mask_under") or bands) and v < (bands[0][0] if bands else f["vmin"])
        shown = math.isfinite(v) and not blank and not f.get("cats") and not f.get("radar_ptype")
        if shown:
            ax.text(cx, cy + (y1 - y0) * 0.012, _fmt_value(v, f), ha="center", va="bottom",
                    fontsize=14.5, fontweight="bold", color="#111", path_effects=halo, zorder=6)
        ax.text(cx, cy - (y1 - y0) * 0.012, name, ha="center", va="top",
                fontsize=11 if major else 9, fontweight="semibold" if major else "medium",
                color="#111" if major else "#2b2b2b", path_effects=halo, zorder=6)

    # ---- water names (regional maps), wherever no city label already sits ----
    for name, cx, cy in water_labels(region, cities):
        ax.text(cx, cy, name.upper(), ha="center", va="center", fontsize=9.5, fontweight="medium",
                color="#4f7391", alpha=0.9, zorder=4,
                path_effects=[pe.withStroke(linewidth=2.2, foreground="white", alpha=0.7)])

    # ---- title block ----
    m = MODELS[model]
    plain = f.get("cats") or f.get("radar_ptype")      # no unit, no single number
    unit = "" if plain else " (%s)" % f["unit"]
    seen = vals[inview[sl]] if inview[sl].shape == vals.shape else vals
    seen = seen[np.isfinite(seen)]
    peak = "Max in view: %s %s" % (_fmt_value(float(seen.max()), f), f["unit"]) if seen.size and not plain else ""
    if dark:
        # Two bands of equal height, text from the left, the same space above
        # the text as below it: the arrangement settled on for the loop's reel.
        valid = run_dt + timedelta(hours=fxx)
        L, top = 0.034, 1.0 - 0.144
        fig.text(L, top + 0.113, m["name"].upper(), fontfamily=TITLE_FONT, fontsize=19,
                 fontweight="semibold", color="#b9c8da", va="center", ha="left")
        fig.text(L, top + 0.080, "%s%s" % (f["label"], unit), fontfamily=TITLE_FONT, fontsize=32,
                 fontweight="semibold", color="white", va="center", ha="left")
        fig.text(L, top + 0.036, _clock(valid, reg["tz"]), fontfamily=TITLE_FONT, fontsize=36,
                 fontweight="semibold", color="#4ea3ff", va="center", ha="left")
        fig.text(1 - L, top + 0.113, "hour %d of the %s run" % (fxx, _clock(run_dt, reg["tz"])),
                 fontsize=11.5, fontweight="medium", color="#8ea3bd", va="center", ha="right")
        cax = fig.add_axes([L, 0.092, 1 - 2 * L, 0.013])
        _colorbar(f, cax, "#dfe8f3", "#33465e", 11)
        fig.text(L, 0.118, peak or ("" if plain else f["unit"]), fontsize=11.5, fontweight="medium",
                 color="#b9c8da", va="center", ha="left")
        fig.text(L, 0.040, "ryansajdak.com", fontfamily=TITLE_FONT, fontsize=27,
                 fontweight="semibold", color="white", va="center", ha="left")
        fig.text(1 - L, 0.040, "Data: %s" % credit(model), fontsize=10.5,
                 color="#8ea3bd", va="center", ha="right")
    else:
        k = 1.0 if shape == "wide" else 0.94          # the square sheet is a little narrower
        fig.text(0.011, 0.985, "%s  ·  %s%s" % (m["name"], f["label"], unit), fontfamily=TITLE_FONT,
                 fontsize=26 * k, fontweight="semibold", color="#111", va="top", ha="left")
        sub = 0.940 if shape == "wide" else 0.951
        fig.text(0.011, sub, _when(run_dt, fxx, reg["tz"]),
                 fontsize=13 * k, fontweight="medium", color="#3a4048", va="top", ha="left")
        fig.text(0.989, 0.983, "ryansajdak.com", fontfamily=TITLE_FONT, fontsize=19,
                 fontweight="semibold", color="#111", va="top", ha="right")
        if peak:
            fig.text(0.989, sub, peak, fontsize=13 * k, fontweight="medium", color="#3a4048",
                     va="top", ha="right")

        # ---- colorbar ----
        cy = 0.036 if shape == "wide" else 0.034
        cax = fig.add_axes([0.2, cy, 0.6, 0.018 if shape == "wide" else 0.012])
        _colorbar(f, cax, "#222", "#444", 11.5)
        if not plain:
            fig.text(0.192, cy + 0.009 if shape == "wide" else cy + 0.006, f["unit"], fontsize=12,
                     fontweight="medium", color="#222", va="center", ha="right")
        fig.text(0.989, 0.014 if shape == "wide" else 0.008, "Data: %s" % credit(model), fontsize=9.5,
                 color="#666", va="bottom", ha="right")

    tmp = part_name(out_path)
    fig.savefig(tmp, format="png", dpi=MAP_DPI, facecolor=paper)
    plt.close(fig)
    os.replace(tmp, out_path)
