#!/bin/bash
# Full 504-point grid search over ASH parameters.
# Token expires every ~1 hour. When you see "Token expired", paste a new one.
#
# Usage:
#   export PROSPERITY_TOKEN="eyJ..."
#   ./run_grid.sh
#
# To resume after interruption:
#   export PROSPERITY_TOKEN="new_token_here"
#   ./run_grid.sh

cd "$(dirname "$0")"

if [ -z "$PROSPERITY_TOKEN" ]; then
    echo "Set PROSPERITY_TOKEN first:"
    echo "  export PROSPERITY_TOKEN=\"your-jwt-token\""
    exit 1
fi

echo "Starting 504-point grid search..."
echo "Estimated time: ~21 hours"
echo "Results save continuously — safe to interrupt and resume."
echo ""

python -m optimizer.runner \
    --token "$PROSPERITY_TOKEN" \
    --interval 20 \
    grid \
    "ash_L1_size=8,10,12,14,16,18,20,25" \
    "ash_k_inv=1.0,1.5,2.0,2.5,3.0,3.5,4.0" \
    "ash_take_edge_buy=1,2,3" \
    "ash_take_edge_sell=2,3,4"
