#!/bin/bash
set -e

# Get absolute path to this script's directory (PROJECT_ROOT)
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

# Process-local settings only; do not change shell profiles or system PATH.
mkdir -p "$DIR/.cache/pip" "$DIR/data/temp"
export PIP_CACHE_DIR="$DIR/.cache/pip"
export XDG_CACHE_HOME="$DIR/.cache"
export TMPDIR="$DIR/data/temp"
export TMP="$TMPDIR"
export TEMP="$TMPDIR"

echo "=========================================="
echo " Setting up justClipper (Local Mode) "
echo "=========================================="
echo "Project Root: $DIR"

# 1. Detect macOS / Apple Silicon
if [[ "$OSTYPE" != "darwin"* ]]; then
    echo "Warning: This setup script is optimized for macOS."
fi

if [[ $(uname -m) != "arm64" ]]; then
    echo "Warning: You are not running on Apple Silicon (arm64). Performance may be limited."
else
    echo "Hardware: Apple Silicon (arm64) detected."
fi

# 2. Python Environment
echo ""
echo "--- Python Environment ---"
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment in .venv..."
    "$DIR/scripts/run-contained.sh" python3 -m venv .venv
else
    echo "Virtual environment .venv already exists."
fi

echo "Activating virtual environment..."
source .venv/bin/activate

echo "Upgrading pip..."
"$DIR/scripts/run-contained.sh" "$DIR/.venv/bin/python" -m pip install --upgrade pip

echo "Installing project dependencies from requirements.txt..."
"$DIR/scripts/run-contained.sh" "$DIR/.venv/bin/python" -m pip install -r requirements.txt

# 3. Directories & Structure
echo ""
echo "--- Directories ---"
# App will create these on first run, but we can ensure the top levels exist
mkdir -p .models .cache data/temp data/sources data/renders data/youtube logs tools

echo "Data directories created successfully."

# 4. Verify FFmpeg / FFprobe
echo ""
echo "--- External Dependencies ---"
python -c 'from app.config import config; print("FFmpeg:", config.get_ffmpeg_path()); print("FFprobe:", config.get_ffprobe_path())'

# 5. Application Import Check
echo ""
echo "--- Application Validation ---"
echo "Validating Python imports..."
"$DIR/scripts/run-contained.sh" "$DIR/.venv/bin/python" -c "import app.main; print('Imports successful.')" || echo "WARNING: Import validation failed."

echo "=========================================="
echo "Setup Complete!"
echo "Run ./start-local.command to launch justClipper."
