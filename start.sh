#!/usr/bin/env sh
# Serve the dashboard and wait for the client:  ./start.sh [--port N]
set -e
cd "$(dirname "$0")"
PYTHON="${PYTHON:-$(command -v python3 || command -v python)}"
exec "$PYTHON" -u -m bazaar_dashboard start "$@"
