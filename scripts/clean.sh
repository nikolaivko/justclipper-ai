#!/bin/bash
set -e
echo "Cleaning temporary files..."
find data/temp -type f -not -name ".gitkeep" -exec rm -f {} +
echo "Done."

