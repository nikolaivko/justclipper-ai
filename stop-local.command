#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

PID_FILE="$DIR/data/temp/justclipper.pid"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    echo "Stopping justClipper (PID: $PID)..."
    kill -15 "$PID" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "justClipper stopped successfully."
else
    echo "justClipper does not appear to be running (no PID file found)."
    
    # Optional fallback: look for uvicorn processes from this directory
    PIDS=$(pgrep -f "uvicorn app.main:app" || true)
    if [ ! -z "$PIDS" ]; then
        echo "Found orphaned justClipper processes. Stopping them..."
        for p in $PIDS; do
            kill -15 "$p" 2>/dev/null || true
        done
        echo "Done."
    fi
fi

# ALWAYS clean up any orphaned ffmpeg processes spawned by justclipper
FFMPEG_PIDS=$(pgrep -f "ffmpeg.*justclipper" || true)
if [ ! -z "$FFMPEG_PIDS" ]; then
    echo "Stopping background video tasks (ffmpeg)..."
    for p in $FFMPEG_PIDS; do
        kill -9 "$p" 2>/dev/null || true
    done
    echo "Video tasks stopped."
fi
