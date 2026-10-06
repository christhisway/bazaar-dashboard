#!/usr/bin/env sh
# Connect the dashboard to a client, once:  ./setup.sh /path/to/client
# Extra flags pass through; see `./setup.sh --help`.
set -e
cd "$(dirname "$0")"
PYTHON="${PYTHON:-$(command -v python3 || command -v python)}"
if [ "$#" -eq 0 ] || [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    exec "$PYTHON" -u -m bazaar_dashboard setup --help
fi
exec "$PYTHON" -u -m bazaar_dashboard setup "$@"
