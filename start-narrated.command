#!/bin/bash
set -euo pipefail
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd -P )"
echo "Narrated recap: http://127.0.0.1:8010"
echo "Download your result before closing this terminal. Clipper runs separately."
# Open the UI only after the server responds; keep browser launch outside the
# contained media runtime. This does not install or configure anything.
(
    for attempt in {1..60}; do
        if /usr/bin/curl -fsS --max-time 1 http://127.0.0.1:8010/ -o /dev/null 2>/dev/null; then
            /usr/bin/open http://127.0.0.1:8010
            exit
        fi
        sleep 1
    done
    echo "Open http://127.0.0.1:8010 in your browser once startup completes."
) &
exec "$DIR/tools/narrated-dev/scripts/run-narrated.sh"
