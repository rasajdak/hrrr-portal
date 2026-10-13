# Model Interpretation Portal

A small server app that reads **raw model GRIB2** and serves it two ways: as
selectable, animatable layers on a live map, and as finished static maps made to
be saved and posted. It began as an HRRR viewer and still lives in a repo named
for it.

- **Models:** HRRR (3 km, hourly, 18 h), NAM CONUS nest (3 km, every 6 h, 60 h),
  RAP (13 km, hourly, 21 h), GFS (0.25 degree, every 6 h, 10 days) from NOAA's
  public AWS buckets, and ECMWF IFS (0.25 degree, 00Z and 12Z, 10 days) from the
  open-data mirror on Google Cloud (the AWS one throttles with 503s). No
  credentials anywhere. Add one in `MODELS`.
- **Subsetting:** each GRIB's `.idx` sidecar gives every message's byte offset,
  so one HTTP Range request pulls **only the requested field** (~1 MB), not the
  ~150 MB file. This is done directly with urllib; Herbie is no longer used.
- **Rendering:** cartopy reprojects the native grid to a Web Mercator PNG, so it
  registers exactly under a Leaflet `imageOverlay`.
- **Serving:** Flask + a content-addressed disk cache (`cache/`), so repeat
  requests for the same (model, field, run, hour) are instant.

## Updating the running droplet

`/opt/hrrr-portal` is a **git checkout**, so the deploy is a pull, not an rsync.
An rsync over it leaves the checkout dirty and the next pull refuses.

```bash
git push                                    # from the Mac
ssh -i ~/.ssh/hrrr_deploy root@DROPLET_IP '
    sudo -u hrrr git -C /opt/hrrr-portal pull --ff-only &&
    systemctl restart hrrr-portal'
```

Two things that cost real time on 2026-09-17, both worth knowing before the
next deploy:

- **The deploy key authenticates as `root`, not as `hrrr`.** `~/.ssh/hrrr_deploy`
  was installed in root's `authorized_keys` only, so `hrrr@` gives
  `Permission denied (publickey)` however right the command looks. There is no
  `~/.ssh/config` entry either, so the key has to be named with `-i` or ssh
  offers the default identity and fails. Worth fixing properly by appending
  `hrrr_deploy.pub` to `/home/hrrr/.ssh/authorized_keys`, after which the
  documented `hrrr@` form works.
- **Do not hot-patch files on the server.** The Esri basemap swap was edited
  directly into `/opt/hrrr-portal/static/index.html` and never committed there,
  so the checkout sat one commit behind with an uncommitted change that was
  byte-identical to the commit it was blocking. The pull refused until it was
  stashed. Edit here, commit, push, pull.

## Fields (57 on HRRR, grouped in the picker)
- **Radar & storms** — radar by precipitation type (composite reflectivity in three colour families: rain, mix, snow) · composite and 1 km reflectivity · hourly max reflectivity · echo top · VIL · 2–5, 0–3 and 0–2 km updraft helicity · max updraft and downdraft · max hail size · lightning
- **Instability** — surface / mixed-layer / most-unstable / 0–3 km CAPE · surface CIN · best lifted index · precipitable water
- **Rotation & shear** — 0–3 km & 0–1 km storm-relative helicity · 0–6 km & 0–1 km bulk shear
- **Surface weather** — 2 m temp · dewpoint · RH · 10 m wind · gust · MSLP · visibility
- **Precipitation** — per-frame precip · total since run start · precip rate
- **Snow & ice** — total snowfall (the model's own, and at 10:1) · per-frame snowfall · precipitation type · freezing rain · share frozen · snow depth · water in the snowpack · snow cover · freezing level
- **Clouds & radiation** — total cloud cover · ceiling · downward shortwave · PBL height
- **Smoke (HRRR-Smoke)** — near-surface smoke · vertically-integrated smoke
- **Upper air (pressure levels)** — 500 mb height · 850/500 mb temp · 700 mb RH · 700 mb vertical velocity · 250 mb (jet) & 850 mb wind speed

Not every model carries every field: NAM has 46, RAP 42, GFS 36, ECMWF 23 (its
names and index format differ, so they sit in their own `ECMWF` table). A field's
`models` says who has it and `per` holds the per-model differences (NAM and GFS
call sea-level pressure `PRMSL`, and so on). Add more by extending `FIELDS` in
`hrrr_render.py`.

Two things the files do not hand over directly:

- **NAM run totals.** NAM resets its accumulations every 3 hours, so a storm
  total is the sum of the buckets (`accum`).
- **GFS snowfall.** GFS posts no snowfall field. Its snow is each 6-hour
  precipitation bucket times the share of that window it was snowing (`CSNOW`
  average), at 10:1, summed. Running totals are kept per frame in `cache/acc_*`
  and pruned after 36 hours.

## Static maps
`/api/map` draws a finished 2400 x 1500 picture in a Lambert conformal fitted to
one of 13 regions (`REGIONS`), set in Barlow and Barlow Condensed (`fonts/`, OFL): land and water, state, county and national borders, a
title with the valid time in the region's own time zone, the value at each city that fits, the maximum
in view and a colorbar. Fields are smoothed by a Gaussian sized in sheet pixels
(about 9 px), and banded ones are resampled finer and contoured, so nothing shows
the grid; the printed values stay raw. This is the page's **default view**, with
a region picker, a download button and a share link; the **Live map** button
switches to the zoomable Leaflet map (and the choice is remembered). `/share` wraps the same image
in a page with Open Graph tags, pinned to the run, so a posted link shows the
map itself.

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

### Shapes
`/api/map` takes `shape=wide` (the default, 2400 x 1500), `square` (2160 x 2160,
for a feed) or `tall` (1620 x 2880, 9:16, for a story or reel). Each frames the
region to suit (`SHAPES`): the square and tall sheets trim the sides and run
further north and south. The tall sheet uses the loop's reel layout, dark bands
top and bottom with the text set from the left. Only the wide sheet is drawn
ahead; the others draw when asked.

## The animated loop
`/loop` is a futurecast: radar by precipitation type, in motion over the same
terrain base as the static maps. The server does not draw it. `loop.py` hands
the browser three kinds of ingredient in one region's map frame: a base picture
(land, water, relief), a transparent label sheet (roads, borders, names), and
for each forecast time a small data PNG whose red, green and blue channels are
the echo strength where rain, mix and snow own the spot. A WebGL shader blends
two neighbouring times and colours the result, sixty times a second, and the
whole stage is a 1920 x 1080 canvas that the Save as video button records to
MP4. HRRR runs on its 15-minute sub-hourly fields (73 frames over 18 hours);
NAM and GFS are hourly, ECMWF 3-hourly. Between times the weather is carried
along a flow field worked out per pair of frames (`/api/loop/flow`), not faded.
Any region can be asked for as `name@tall`, an upright 1080 x 1920 framing for
reels: the map fills the frame and the title, clock and legend sit in the band
the apps leave clear. `prewarm.py` draws the frames ahead for `LOOP_AREAS`.

### Maps drawn ahead of time
`prewarm.py` draws the static maps in its plan (per model: totals at a few
quoted hours, moving fields at a set step, for a list of regions) as soon as a model posts a new run, so the page opens them
instantly. A systemd timer runs one pass every 10 minutes at low priority; a
pass skips maps that exist, stops after 8 minutes, and deletes cached frames
older than 18 hours (and draws nothing if the disk is under 6 GB free). There are
two plans: `LEAN` for the droplet's one core, and `WIDE` (every HRRR hour, ten
days of GFS and ECMWF, the Great Lakes), which a pass takes when it is given four
or more workers, as the drawing Mac's is.

```bash
sudo cp deploy/hrrr-prewarm.service deploy/hrrr-prewarm.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now hrrr-prewarm.timer
journalctl -u hrrr-prewarm -n 20            # "drew N maps in S s" per pass
sudo systemctl disable --now hrrr-prewarm.timer   # turn it off
```

### A second machine as the drawing machine
The droplet has one core. Any Mac can do the drawing instead and send the
finished maps up, leaving the droplet to serve them:

```bash
git clone https://github.com/rasajdak/hrrr-portal.git && cd hrrr-portal
bash deploy/mac/setup.sh        # installs uv if missing, makes a key, adds a launchd job
```

The script ends by printing a public key. Add it on the droplet, one line per
machine, to `/home/mapsync/.ssh/authorized_keys`:

```
command="/usr/local/bin/mapsync-receive",restrict ssh-ed25519 AAAA... name
```

`mapsync` is an account that can do one thing: that key runs
`deploy/mapsync-receive`, which unpacks `.png` files from a tar stream into a
staging folder and moves them into `cache/` whole. The Mac must be on the same
commit as the droplet, since cache names include the render revision. Once it
is keeping up, the droplet's own timer can be left on as a fallback (it skips
what exists) or switched to `prewarm.py --prune-only`.

### Serving from the drawing machine
Copying 40 GB of maps a day to a 48 GB droplet filled it (2026-10-13), so the
drawing Mac now serves the site as well. The droplet keeps the name, the
certificate and its own copy of the app; the Mac holds an ssh connection open
that makes its app appear on the droplet's port 8001, and nginx prefers that
port and falls back to the droplet's own app when it is dead
(`deploy/nginx.conf`). Nothing at home is opened to the internet.

```bash
~/.local/bin/micromamba install -p ~/hrrr-env -c conda-forge gunicorn
HRRR_PYTHON=~/hrrr-env/bin/python bash deploy/mac/serve.sh
SERVER=none HRRR_PYTHON=~/hrrr-env/bin/python bash deploy/mac/setup.sh   # draw, copy nothing up
```

`serve.sh` prints the key line for `/home/mapsync/.ssh/authorized_keys`; that
key can only carry port 8001 back. Every response says who answered in
`X-Served-By` (`127.0.0.1:8001` is the Mac). `/api/purpleair`, `/api/panel`
and the air-quality tiles always stay on the droplet. While the Mac answers,
the droplet's timer only prunes. Saved maps live on whichever machine took the
save, so the `/saved` shelf is the Mac's while it is up.

## Endpoints
| route | purpose |
|-------|---------|
| `GET /` | portal UI |
| `GET /api/config?model=` | models, that model's fields and latest run, regions |
| `GET /api/overlay?model=&field=&run=&fxx=` | rendered field PNG |
| `GET /api/map?model=&field=&run=&fxx=&region=` | finished static map PNG (`dl=1` to download) |
| `GET /api/ready?model=&run=` | which static maps of that run are already drawn: `{field: {region: [hours]}}` |
| `GET /loop` | the animated futurecast page |
| `GET /api/loop?model=&region=` | its run, frame times and palette |
| `GET /api/loop/frame?model=&run=&m=&region=` | one forecast time as a data PNG |
| `GET /api/loop/base?region=&layer=` | a region's base or label layer |
| `GET /api/runs?model=` | recent runs, newest first, with how many maps each has drawn (the page's run picker) |
| `POST /api/save?...` | copy that map to `saved/` under a readable name; returns its permanent URL |
| `GET /saved` | gallery of kept maps (capped at 500; delete files from `saved/` to prune) |
| `GET /share?...` | the static map on a page with link-preview tags |
| `GET /api/point?model=&field=&run=&fxx=&lat=&lon=` | one grid-point value |
| `GET /api/legend?model=&field=` | colorbar PNG |

`model` defaults to `hrrr` everywhere, so every older URL still works.

## Notes
- `run` defaults to the newest run posted for that model. HRRR counts as posted
  once hour 0 lands; the others wait for their last frame, so a loop never runs
  off the end of a half-uploaded run. Pass an explicit `YYYYMMDDHH` to pin a run.
- HRRR is CONUS-only. `refc`/precip are convection fields; `apcp` needs F ≥ 1.
- This renders full-CONUS overlays per field/hour. For per-tile zoomable serving
  you'd swap the imageOverlay for a `/tiles/{z}/{x}/{y}` XYZ endpoint later.
