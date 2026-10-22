# Checking the lake effect score against real storms

`lake_effect.py` gives a 0-100 score. These scripts run it on archived model
runs and compare it with the snow that fell (NOHRSC national snowfall analysis,
24 h totals ending 12Z, inches, 0.04 degree). Everything they download or work
out goes in `data/`, which git ignores (about 1 GB after a full pass).

    ./run.sh getsnow.py                  # observed snowfall grids for the dates below
    ./run.sh fetch.py jobs.txt 8         # score frames: 8 storms 3-hourly, 2 winters 6-hourly (HRRR analyses, ~12 min)
    ./run.sh storms.py                   # storm by storm: rank correlation, peak offset, named towns, a figure
    ./run.sh season.py                   # every day of winters 2022-23 and 2024-25: reliability, hits, false alarms
    ./run.sh fetch.py jobs_gfs.txt 8     # GFS forecasts 1, 3, 5, 7 days ahead of four storms
    ./run.sh lead.py                     # ...and how they compare

`LE_FRAMES=name` puts a pass in `data/name/` (default `frames`), so a changed
score can be compared with the old one: fetch, storms and season all honour it.

Retuning without downloading again:

    ./run.sh tune_fetch.py jobs_storms.txt   # store heated fetch, metres climbed, shear/cap trim per frame
    ./run.sh tune.py                         # terrain gain x full rise, belt by belt
    ./run.sh rescale.py                      # the FETCH_KM that keeps the scale where it was
    ./run.sh build_terrain.py                # rebuilds ../lake_terrain.npz from HRRR orography

## What it found (2026-10-12)

Before the terrain term (`frames_v1`) and after (`frames_v3`), HRRR analyses:

- Lake Erie: band placed 3 to 30 km from the observed maximum in the big storms; unchanged by terrain.
- Lake Ontario: the peak sat on the shore near Henderson Harbor in every storm.
  With terrain it moved inland; mean rank correlation over 8 storms 0.54 -> 0.67,
  mean peak offset 49 -> 33 km. Oswego is still scored too high.
- Two winters, daily: no belt-day scoring 30+ got under 1 inch. A daily-mean
  score of 50+ averaged 14.5 in. Best CSI for 12 in on Ontario 0.18 -> 0.24.
- Still blind to short-fetch northwest multi-lake events (Nov 19-22, 2016) and
  to synoptic snow; lights the whole downwind shore, not a single band.
- GFS carries the signal about 5 days ahead.
