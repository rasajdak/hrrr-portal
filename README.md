# HRRR Interpretation Portal

A small server app that reads **raw HRRR GRIB2** and serves it as selectable,
animatable map layers — a viewing/interpretation portal for the NOAA
High-Resolution Rapid Refresh model.

- **Source:** AWS Open Data bucket `noaa-hrrr-bdp-pds` (public, no credentials).
- **Subsetting:** [Herbie](https://herbie.readthedocs.io) reads each GRIB's `.idx`
  sidecar and HTTP byte-ranges **only the requested field** (~1 MB), not the
  ~150 MB file.
- **Rendering:** cartopy reprojects the native 3 km Lambert grid to Web Mercator
  PNG, so it registers exactly under a Leaflet `imageOverlay`.
- **Serving:** Flask + a content-addressed disk cache (`cache/`), so repeat
  requests for the same (field, run, hour) are instant.

## Fields (35, grouped in the picker)
- **Radar & storms** — composite reflectivity · 1 km AGL reflectivity · echo top · 2–5 km updraft helicity
- **Instability** — surface / mixed-layer / most-unstable CAPE · surface CIN · best lifted index · precipitable water
- **Rotation** — 0–3 km & 0–1 km storm-relative helicity
- **Surface weather** — 2 m temp · dewpoint · RH · 10 m wind · gust · MSLP · visibility
- **Precipitation** — 1-hr precip · precip rate · snow depth
- **Clouds & radiation** — total cloud cover · ceiling · downward shortwave · PBL height
- **Smoke (HRRR-Smoke)** — near-surface smoke · vertically-integrated smoke
- **Upper air (pressure levels)** — 500 mb height · 850/500 mb temp · 700 mb RH · 700 mb vertical velocity · 250 mb (jet) & 850 mb wind speed

Forecast hours 0–18, scrub or animate. Optional live NEXRAD overlay for
forecast-vs-reality comparison. Pressure-level fields pull from the HRRR `prs`
product; jet-level winds are the U/V magnitude. Add more by extending `FIELDS`
in `hrrr_render.py`.

## Interpretation features
- **Click-to-probe** — click anywhere on the map to read the nearest grid-point
  value of the current field, and get a **meteogram**: that field sampled at the
  point across all 18 forecast hours, with a cursor tracking the current hour.
  Switching fields re-probes the pinned point. Backed by `/api/point`, which
  reuses whatever the render/animation path already downloaded (fast once warm).
- **Real valid times** — the app probes AWS for the newest posted run and shows
  the true valid clock time (local + UTC), not just lead time.
- **Smooth animation** — hitting play prefetches every forecast-hour PNG so the
  loop doesn't stall on first pass.
- **Resilient fetch** — an interrupted/corrupt GRIB subset is re-downloaded once
  automatically, and a single bad hour never kills a meteogram.

## Run locally
```bash
mamba env create -f environment.yml   # or: conda env create -f environment.yml
conda activate hrrr-portal
python app.py                          # http://127.0.0.1:8000
```
First render of a field downloads its GRIB message (a few seconds); after that
it is cached. `pip install -r requirements.txt` also works if geos/proj/eccodes
system libs are present.

## Deploy to a DigitalOcean droplet

**Droplet:** Ubuntu 24.04, **2 GB / 1 vCPU** ($12/mo) is comfortable — cartopy +
matplotlib can spike ~1 GB while rendering. The 1 GB basic plan works if you add
a swap file. Disk fills slowly (each cached PNG is small); the raw GRIB temp
files are removed after render.

```bash
# 1. create the droplet, then ssh in as root
adduser hrrr && usermod -aG sudo hrrr        # a non-root service user
mkdir -p /opt/hrrr-portal && chown hrrr /opt/hrrr-portal

# 2. as hrrr: install miniforge (conda-forge handles geos/proj/eccodes cleanly)
curl -L -o mf.sh https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
bash mf.sh -b -p /opt/miniforge3
/opt/miniforge3/bin/mamba env create -f /opt/hrrr-portal/environment.yml

# 3. copy the app up (from your Mac)
#    rsync -av --exclude cache --exclude __pycache__ ./ hrrr@DROPLET_IP:/opt/hrrr-portal/

# 4. service + reverse proxy
sudo cp /opt/hrrr-portal/deploy/hrrr-portal.service /etc/systemd/system/
sudo systemctl enable --now hrrr-portal
sudo apt-get install -y nginx
sudo cp /opt/hrrr-portal/deploy/nginx.conf /etc/nginx/sites-available/hrrr-portal
sudo ln -s /etc/nginx/sites-available/hrrr-portal /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# 5. (optional) domain + HTTPS
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your.domain
```

Then browse to the droplet's IP (or domain). Logs: `journalctl -u hrrr-portal -f`.

### Optional: pre-warm the cache
Add a cron job that renders the common fields for the latest run so the first
visitor never waits:
```bash
# hits the API for a few fields at F0 after each hourly run posts
0 * * * * for f in refc t2m cape wind10m; do curl -s "http://127.0.0.1:8000/api/overlay?field=$f&fxx=0" -o /dev/null; done
```

## Endpoints
| route | purpose |
|-------|---------|
| `GET /` | portal UI |
| `GET /api/config` | fields, extent bounds, latest run |
| `GET /api/overlay?field=&run=&fxx=` | rendered field PNG |
| `GET /api/legend?field=` | colorbar PNG |

## Notes
- `run` defaults to the latest likely-posted HRRR init (now − 1 h, UTC). Pass an
  explicit `YYYYMMDDHH` to pin a run.
- HRRR is CONUS-only. `refc`/precip are convection fields; `apcp` needs F ≥ 1.
- This renders full-CONUS overlays per field/hour. For per-tile zoomable serving
  you'd swap the imageOverlay for a `/tiles/{z}/{x}/{y}` XYZ endpoint later.
