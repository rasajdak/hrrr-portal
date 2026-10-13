#!/bin/bash
# Serve the weather portal from this Mac, with the droplet as its front door.
#
# The droplet keeps the name, the certificate and a copy of the app. This Mac
# runs the app as well, next to the maps it draws, and holds an ssh connection
# open to the droplet that carries the droplet's port 8001 back to the app
# here. nginx on the droplet sends visitors down that connection, and answers
# them itself whenever it is not there (this Mac off, asleep or offline). So
# nothing at home is opened to the internet, and maps no longer have to be
# copied up. Run once, after setup.sh, from the same clone:
#
#   HRRR_PYTHON=~/hrrr-env/bin/python bash deploy/mac/serve.sh
#
# The python needs gunicorn as well as the map stack. Undo with --remove.
set -euo pipefail

SERVER="${SERVER:-mapsync@104.248.236.49}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${HRRR_PYTHON:?set HRRR_PYTHON to the python that has the map stack and gunicorn}"
WEB="com.ryansajdak.hrrr-web"
TUNNEL="com.ryansajdak.hrrr-tunnel"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs"
KEY="$HOME/.ssh/hrrr_tunnel"
PORT=8000            # the app, on this Mac only
FAR=8001             # where it appears on the droplet, on the droplet only

if [ "${1:-}" = "--remove" ]; then
    for l in "$WEB" "$TUNNEL"; do
        launchctl bootout "gui/$(id -u)/$l" 2>/dev/null || true
        rm -f "$AGENTS/$l.plist"
    done
    echo "Removed both jobs. The droplet answers by itself again. The key ($KEY) was left alone."
    exit 0
fi

"$PY" -m gunicorn --version

if [ ! -f "$KEY" ]; then
    mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
    ssh-keygen -q -t ed25519 -N "" -C "hrrr-tunnel@$(scutil --get ComputerName 2>/dev/null || hostname)" -f "$KEY"
fi

mkdir -p "$AGENTS" "$LOGS"
cat > "$AGENTS/$WEB.plist" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$WEB</string>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string>
    <string>-m</string><string>gunicorn</string>
    <string>--workers</string><string>3</string>
    <string>--threads</string><string>2</string>
    <string>--timeout</string><string>300</string>
    <string>--bind</string><string>127.0.0.1:$PORT</string>
    <string>app:app</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <!-- gunicorn forks its workers, which macOS otherwise kills on sight -->
    <key>OBJC_DISABLE_INITIALIZE_FORK_SAFETY</key><string>YES</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOGS/hrrr-web.log</string>
  <key>StandardErrorPath</key><string>$LOGS/hrrr-web.log</string>
</dict></plist>
PLIST_EOF

cat > "$AGENTS/$TUNNEL.plist" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$TUNNEL</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/ssh</string>
    <string>-N</string><string>-T</string>
    <string>-o</string><string>BatchMode=yes</string>
    <string>-o</string><string>IdentitiesOnly=yes</string>
    <string>-o</string><string>ExitOnForwardFailure=yes</string>
    <string>-o</string><string>ServerAliveInterval=20</string>
    <string>-o</string><string>ServerAliveCountMax=3</string>
    <string>-o</string><string>ConnectTimeout=20</string>
    <string>-i</string><string>$KEY</string>
    <string>-R</string><string>127.0.0.1:$FAR:127.0.0.1:$PORT</string>
    <string>$SERVER</string>
  </array>
  <key>RunAtLoad</key><true/>
  <!-- ssh exits when the line drops; launchd starts it again -->
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$LOGS/hrrr-tunnel.log</string>
  <key>StandardErrorPath</key><string>$LOGS/hrrr-tunnel.log</string>
</dict></plist>
PLIST_EOF

for l in "$WEB" "$TUNNEL"; do
    launchctl bootout "gui/$(id -u)/$l" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$AGENTS/$l.plist"
done

cat <<DONE

The app is running here on 127.0.0.1:$PORT (log: $LOGS/hrrr-web.log) and the
connection to the droplet is being held open (log: $LOGS/hrrr-tunnel.log).

The droplet has to accept this Mac's key before the connection comes up. The
key may do one thing, carry the droplet's port $FAR back here. Add this line to
/home/mapsync/.ssh/authorized_keys on the droplet:

restrict,port-forwarding,permitlisten="127.0.0.1:$FAR",command="/bin/false" $(cat "$KEY.pub")
DONE
