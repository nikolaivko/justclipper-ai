#!/bin/bash
set -euo pipefail
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$DIR"
# All diagnostics stay on stdout unless the caller redirects inside the project.
export TMPDIR="$DIR/data/temp"
export TMP="$TMPDIR"
export TEMP="$TMPDIR"
export XDG_CACHE_HOME="$DIR/.cache"
exec "$DIR/.venv/bin/python" -B "$DIR/scripts/audit_local_containment.py"
