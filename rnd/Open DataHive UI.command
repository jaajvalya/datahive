#!/bin/bash
# macOS launcher — starts APIs, then opens the HTTP UI (never file://).
set -euo pipefail
RND="$(cd "$(dirname "$0")" && pwd)"
cd "$RND"

bash "$RND/start_apis.sh"
open "http://127.0.0.1:5055/"
echo "DataHive UI: http://127.0.0.1:5055/"
