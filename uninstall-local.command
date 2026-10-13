#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

echo "=========================================="
echo " justClipper Local Uninstall Guide "
echo "=========================================="
echo ""
echo "Deleting this project folder ($DIR) removes approximately:"

calc_size() {
    if [ -d "$1" ]; then
        du -sh "$1" 2>/dev/null | cut -f1 | tr -d '[:space:]'
    else
        echo "0B"
    fi
}

echo "Environment: $(calc_size .venv)"
echo "Models: $(calc_size .models)"
echo "Sources: $(calc_size data/sources)"
echo "Renders: $(calc_size data/output)"
echo "Cache/temp: $(calc_size .cache) / $(calc_size data/temp)"
echo "Total: $(calc_size .)"
echo ""
echo "External dependencies that will remain:"
if command -v ffmpeg >/dev/null 2>&1; then
    FFMPEG_PATH=$(command -v ffmpeg)
    if [[ "$FFMPEG_PATH" != "$DIR"* ]]; then
        echo "- System FFmpeg ($FFMPEG_PATH)"
    fi
fi
if command -v ffprobe >/dev/null 2>&1; then
    FFPROBE_PATH=$(command -v ffprobe)
    if [[ "$FFPROBE_PATH" != "$DIR"* ]]; then
        echo "- System FFprobe ($FFPROBE_PATH)"
    fi
fi
echo ""
echo "Running local containment audit to verify no other files remain..."
./scripts/audit-local-containment.sh

echo ""
echo "To completely uninstall justClipper, simply delete this directory:"
echo "rm -rf \"$DIR\""
