#!/bin/bash
# Set a Mac up as the drawing machine for the weather portal.
#
# It draws the planned static maps every 10 minutes and copies them to the web
# server, which then only has to serve them. Run this once, from a clone of the
# repo:    bash deploy/mac/setup.sh
#
# Nothing here needs sudo. It installs uv (a Python runner) for this user if it
# is missing, makes an SSH key used only for copying maps up, and registers a
# launchd job. Undo with:   bash deploy/mac/setup.sh --remove
set -euo pipefail

# SERVER=none draws without copying anything up: for a Mac that serves the
# site itself (serve.sh), where the maps are already where they are needed.
SERVER="${SERVER:-mapsync@104.248.236.49}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LABEL="com.ryansajdak.hrrr-prewarm"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
KEY="$HOME/.ssh/hrrr_push"
LOG="$HOME/Library/Logs/hrrr-prewarm.log"

if [ "${1:-}" = "--remove" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed the job. The repo, the key ($KEY) and the cache were left alone."
    exit 0
fi

# ---- uv, which fetches Python and the map libraries on first run ----
# An older Mac (macOS 12 on Intel) has no pip wheels for pyproj or ecCodes.
# There, make a conda-forge environment and point HRRR_PYTHON at its python:
#   micromamba create -p ~/hrrr-env -c conda-forge python=3.11 numpy \
#       matplotlib-base cartopy cfgrib eccodes xarray flask
#   HRRR_PYTHON=~/hrrr-env/bin/python bash deploy/mac/setup.sh
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if [ -n "${HRRR_PYTHON:-}" ]; then
    RUN=("$HRRR_PYTHON")
else
    if ! command -v uv >/dev/null; then
        echo "Installing uv ..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
    fi
    RUN=("$(command -v uv)" run -q --python 3.11 --with numpy --with matplotlib
         --with cartopy --with cfgrib --with eccodes --with xarray python)
fi

# ---- half the cores, at least 1, at most 6: the Mac stays usable ----
CORES="$(sysctl -n hw.ncpu)"
WORKERS=$(( CORES / 2 )); [ "$WORKERS" -lt 1 ] && WORKERS=1; [ "$WORKERS" -gt 6 ] && WORKERS=6
WORKERS="${WORKERS_OVERRIDE:-$WORKERS}"

echo "Checking the map stack (the first run downloads about 300 MB) ..."
( cd "$REPO" && "${RUN[@]}" prewarm.py --list )

# ---- a key that is only for copying maps up ----
if [ ! -f "$KEY" ]; then
    mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
    ssh-keygen -q -t ed25519 -N "" -C "hrrr-prewarm@$(scutil --get ComputerName 2>/dev/null || hostname)" -f "$KEY"
fi

PUSH=""
if [ "$SERVER" != "none" ]; then
    PUSH="    <string>--push</string><string>$SERVER</string>
    <string>--key</string><string>$KEY</string>"
fi

# ---- the launchd job: one pass every 10 minutes, low priority ----
mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>ProgramArguments</key><array>
$(printf '    <string>%s</string>\n' "${RUN[@]}")
    <string>prewarm.py</string>
    <string>--workers</string><string>$WORKERS</string>
$PUSH
  </array>
  <key>StartInterval</key><integer>600</integer>
  <key>RunAtLoad</key><true/>
  <key>Nice</key><integer>10</integer>
  <!-- No ProcessType key, on purpose. "Background" puts the job in macOS's
       throttled class: measured on the drawing Mac, six workers took 4 to 5
       times longer on the clock for the same CPU time, and a pass drew 80 maps
       instead of several hundred. Nice alone keeps it polite. -->
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
PLIST_EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

if [ "$SERVER" = "none" ]; then
    echo "Drawing is set up: $WORKERS processes, a pass every 10 minutes, nothing copied up."
    echo "Log:  tail -f \"$LOG\""
    exit 0
fi
cat <<DONE

Drawing is set up: $WORKERS processes, a pass every 10 minutes.
Log:  tail -f "$LOG"

ONE STEP LEFT. The web server has to accept this Mac's key before maps can be
copied up. Send this line to whoever looks after the server (or paste it to
Claude in the weather portal session):

$(cat "$KEY.pub")

Until then each pass ends with "push failed"; the maps wait here and go up on
the first pass after the key is added.

Keep this Mac awake: System Settings > Energy > turn off sleep, and turn on
"Start up automatically after a power failure".
DONE
