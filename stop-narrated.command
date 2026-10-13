#!/bin/bash
set -euo pipefail
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd -P )"
exec "$DIR/.venv/bin/python" -B "$DIR/scripts/stop_narrated.py" "$@"
