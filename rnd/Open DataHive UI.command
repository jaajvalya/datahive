#!/bin/bash
# macOS launcher — starts the connector API watchdog, then opens main.html
set -euo pipefail
RND="$(cd "$(dirname "$0")" && pwd)"
cd "$RND"

if [[ ! -x "$RND/.venv/bin/python" ]]; then
  python3 -m venv "$RND/.venv"
  "$RND/.venv/bin/pip" install -r "$RND/requirements.txt"
fi

if [[ ! -f "$RND/../.env" && -f "$RND/../.env.example" ]]; then
  cp "$RND/../.env.example" "$RND/../.env"
fi

# Start watchdog (keeps connector_api.py on :5055 while the UI is open)
"$RND/.venv/bin/python" "$RND/connector_watchdog.py" >/tmp/datahive-watchdog.log 2>&1 &

sleep 1
open "$RND/main.html"
echo "DataHive UI opened. Connector API: http://127.0.0.1:5055/health"
