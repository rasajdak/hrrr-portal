#!/bin/sh
# ./run.sh storms.py   (LE_FRAMES=frames_v1 ./run.sh storms.py for another set)
cd "$(dirname "$0")" && exec uv run -q --python 3.11 --with numpy --with matplotlib --with cartopy --with cfgrib \
  --with eccodes --with xarray --with flask --with scipy --with rasterio python "$@"
