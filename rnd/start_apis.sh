#!/bin/bash
# Install/load a LaunchAgent that keeps connector_api.py alive on :5055.
set -euo pipefail
RND="$(cd "$(dirname "$0")" && pwd)"
cd "$RND"
PY="$RND/.venv/bin/python"
UI=http://127.0.0.1:5055
LABEL=com.datahive.connector-api
PLIST_SRC="$RND/com.datahive.connector-api.plist"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"

if [[ ! -x "$PY" ]]; then
  python3 -m venv "$RND/.venv"
  "$RND/.venv/bin/pip" install -r "$RND/requirements.txt"
fi

# Rewrite plist paths for this machine/checkout.
mkdir -p "$HOME/Library/LaunchAgents"
cat >"$PLIST_DST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY}</string>
    <string>${RND}/connector_api.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${RND}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>2</integer>
  <key>StandardOutPath</key>
  <string>/tmp/datahive-connector-api.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/datahive-connector-api.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
EOF
cp "$PLIST_DST" "$PLIST_SRC" 2>/dev/null || true

# Stop anything already bound to 5055/5056 that isn't the LaunchAgent child.
for p in 5055 5056; do
  pids=$(lsof -tiTCP:"$p" -sTCP:LISTEN 2>/dev/null || true)
  if [[ -n "${pids:-}" ]]; then
    echo "$pids" | xargs kill 2>/dev/null || true
    sleep 0.4
    echo "$pids" | xargs kill -9 2>/dev/null || true
  fi
done

# (Re)load LaunchAgent under the user GUI domain.
launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "${DOMAIN}" "$PLIST_DST"
launchctl enable "${DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl kickstart -k "${DOMAIN}/${LABEL}"

for _ in $(seq 1 40); do
  if curl -sf "$UI/health" >/dev/null 2>&1; then
    echo "OK $UI  (LaunchAgent ${LABEL}, KeepAlive=true)"
    exit 0
  fi
  sleep 0.35
done

echo "Failed to start connector API. See /tmp/datahive-connector-api.log" >&2
tail -60 /tmp/datahive-connector-api.log 2>/dev/null || true
launchctl print "${DOMAIN}/${LABEL}" 2>/dev/null | head -40 || true
exit 1
