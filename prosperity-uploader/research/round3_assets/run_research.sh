#!/bin/bash
# Launch the autonomous 12-hour research loop for HG + VF assets-only.
#
# Usage:
#   ./run_research.sh                 # 12 hours, default seed
#   ./run_research.sh --hours 6       # shorter run
#   ./run_research.sh --reset         # wipe state and start fresh
#   ./run_research.sh --background    # detach, log to nohup.out
#
# Token: relies on auth_cognito.py auto-auth via ~/.prosperity_creds.

set -e
cd "$(dirname "$0")"

BACKGROUND=0
ARGS=()
for arg in "$@"; do
    if [[ "$arg" == "--background" ]]; then
        BACKGROUND=1
    else
        ARGS+=("$arg")
    fi
done

if [[ $BACKGROUND -eq 1 ]]; then
    echo "Launching in background — logs at $(pwd)/nohup.out"
    nohup python3 research_loop.py "${ARGS[@]}" > nohup.out 2>&1 &
    echo "PID: $!"
    echo "Watch progress:  tail -f $(pwd)/log.txt"
    echo "Inspect state:   cat $(pwd)/state.json"
    echo "Stop early:      kill $!"
else
    python3 research_loop.py "${ARGS[@]}"
fi
