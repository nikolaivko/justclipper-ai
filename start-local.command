#!/bin/bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"
mkdir -p "$DIR/data/temp" "$DIR/.cache"
export TMPDIR="$DIR/data/temp"
export TMP="$TMPDIR"
export TEMP="$TMPDIR"
export XDG_CACHE_HOME="$DIR/.cache"
export PIP_CACHE_DIR="$DIR/.cache/pip"
export DENO_DIR="$DIR/.cache/deno"

echo "=========================================="
echo " Starting justClipper (Local Mode) "
echo "=========================================="

if [ ! -d ".venv" ]; then
    echo "Error: .venv not found. Please run setup-local.command first."
    exit 1
fi

source .venv/bin/activate

# Use local port 8000 if PORT is not set
export PORT="${PORT:-8000}"

PID_FILE="$DIR/data/temp/justclipper.pid"

if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "justClipper is already running (PID: $OLD_PID)."
        echo "Access it at http://127.0.0.1:$PORT"
        echo "To stop it, run ./stop-local.command"
        exit 0
    else
        echo "Removing stale PID file."
        rm -f "$PID_FILE"
    fi
fi

# Ensure output directory exists before starting logs
mkdir -p "$DIR/logs"
mkdir -p "$DIR/data/temp"

echo "Starting server on http://127.0.0.1:$PORT..."

# Run uvicorn in the background
"$DIR/scripts/run-contained.sh" "$DIR/.venv/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" > "$DIR/logs/local-server.log" 2>&1 &
APP_PID=$!

echo $APP_PID > "$PID_FILE"

echo "Waiting for server to become healthy..."
MAX_TRIES=15
TRIES=0
while [ $TRIES -lt $MAX_TRIES ]; do
    if curl -s http://127.0.0.1:$PORT/health | grep '"status":"ok"' > /dev/null; then
        echo "Server is HEALTHY!"
        echo "Opening justClipper in your default browser..."
        open "http://127.0.0.1:$PORT" || true
        echo "Server running in background with PID $APP_PID."
        echo "Logs are available at $DIR/logs/local-server.log"
        echo "Run ./stop-local.command to shut down safely."
        exit 0
    fi
    sleep 2
    TRIES=$((TRIES + 1))
done

echo "Error: Server failed to become healthy within 30 seconds."
echo "Check the logs at $DIR/logs/local-server.log"
kill $APP_PID || true
rm -f "$PID_FILE"
exit 1
