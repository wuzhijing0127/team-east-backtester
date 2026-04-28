#!/bin/bash
# Auto-upload the v10 / v11 strategy candidates in sequence.
# Each upload submits, polls until complete, downloads the artifact, and
# writes metrics to runs/. Run from anywhere — script cd's to its own dir.
#
# Usage:
#   ./auto_upload_v10_v11.sh                # upload in default order
#   ./auto_upload_v10_v11.sh --safe-only    # just the safe baseline (v10)
#   ./auto_upload_v10_v11.sh --pick v11_a v10_blend
#
# Token: relies on auth_cognito.py auto-auth via ~/.prosperity_creds.
# If that's not set up, set PROSPERITY_TOKEN env var first.

set -e
cd "$(dirname "$0")"

# ── Config ──────────────────────────────────────────────────────────
STRATEGY_DIR="../strategies/round3"
INTER_UPLOAD_DELAY=15  # seconds between uploads (lets the platform queue clear)

# Default order: safest first → main candidate → variants → auxiliary
DEFAULT_ORDER=(
    "r3_multi_v10.py"
    "r3_multi_v10_blend.py"
    "r3_multi_v11_a.py"
    "r3_multi_v11_b.py"
    "r3_multi_v11_c.py"
    "r3_multi_v10_recal.py"
)

# ── Arg parsing ─────────────────────────────────────────────────────
TARGETS=()
if [[ "$1" == "--safe-only" ]]; then
    TARGETS=("r3_multi_v10.py")
elif [[ "$1" == "--pick" ]]; then
    shift
    for tag in "$@"; do
        TARGETS+=("r3_multi_${tag}.py")
    done
else
    TARGETS=("${DEFAULT_ORDER[@]}")
fi

# ── Sanity: confirm files exist ─────────────────────────────────────
echo "Auto-upload plan:"
for f in "${TARGETS[@]}"; do
    full="$STRATEGY_DIR/$f"
    if [ ! -f "$full" ]; then
        echo "  ❌ MISSING: $full"
        exit 1
    fi
    echo "  • $f"
done
echo

# ── Run ─────────────────────────────────────────────────────────────
SUCCESS=()
FAILED=()
for f in "${TARGETS[@]}"; do
    full="$STRATEGY_DIR/$f"
    echo "════════════════════════════════════════════════════════════"
    echo "Uploading: $f"
    echo "════════════════════════════════════════════════════════════"

    if python main.py upload "$full"; then
        SUCCESS+=("$f")
        echo "  ✅ $f complete"
    else
        FAILED+=("$f")
        echo "  ❌ $f failed — continuing with remaining uploads"
    fi

    # Pause between uploads (skip after last). Use indexed access for bash 3.2 compat.
    LAST_IDX=$((${#TARGETS[@]} - 1))
    if [[ "$f" != "${TARGETS[$LAST_IDX]}" ]]; then
        echo "Sleeping ${INTER_UPLOAD_DELAY}s before next upload..."
        sleep "$INTER_UPLOAD_DELAY"
    fi
done

# ── Summary ─────────────────────────────────────────────────────────
echo
echo "════════════════════════════════════════════════════════════"
echo "Auto-upload complete"
echo "════════════════════════════════════════════════════════════"
echo "Succeeded (${#SUCCESS[@]}):"
for f in "${SUCCESS[@]}"; do echo "  ✓ $f"; done
if [ ${#FAILED[@]} -gt 0 ]; then
    echo "Failed (${#FAILED[@]}):"
    for f in "${FAILED[@]}"; do echo "  ✗ $f"; done
fi
echo
echo "Per-strategy results in: $(pwd)/runs/"
echo "Aggregated leaderboard:  $(pwd)/results/summary.csv"
