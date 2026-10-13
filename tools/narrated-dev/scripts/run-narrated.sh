#!/bin/bash
set -euo pipefail
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd -P )"
cd "$DIR"
mkdir -p data/temp data/narrated/jobs data/narrated/voices .cache .models logs
export PROJECT_ROOT="$DIR"
export DATA_ROOT="$DIR/data"
export APP_ROOT="$DIR"
export VOICEBOX_MODELS_DIR="$DIR/.models/voicebox"
export HF_HUB_CACHE="$VOICEBOX_MODELS_DIR"
export NUMBA_CACHE_DIR="$DIR/.cache/numba"
export MPLCONFIGDIR="$DIR/.cache/matplotlib"
export AUTONOMOUS_TRENDS_ENABLED=false
export AUTO_PROCESS_ELIGIBLE_SOURCES=false
exec "$DIR/scripts/run-contained.sh" "$DIR/.venv/bin/python" -m app.narrated_server
