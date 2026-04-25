# Round 1 Structural Strategy Variants

Context: baseline (submission 216002) scored ~10,400 PnL. Top 1000 teams
hit ~120,000. Gap diagnosis — PEPPER utilization / strategy class mismatch:
baseline cycles 20-30 long on a product that drifts +1000/day with
position_limit = 80, while ASH MM is under-filled.

Each file is a **self-contained** `Trader` class, ready to upload to the
Prosperity platform independently.

## Variants (ordered by expected impact, least → most aggressive)

| File | PEPPER strategy | ASH strategy | Hypothesis |
|------|----------------|-------------|------------|
| `s01_pepper_maxlong_ash_baseline.py` | Max long 80 + join best_bid | 216002 baseline | Just fix PEPPER utilization |
| `s02_pepper_maxlong_ash_tight.py` | Max long 80 | Tight MM, join best | PEPPER fix + ASH MM upgrade |
| `s03_pepper_momentum_breakout.py` | Buy only on new rolling highs | 216002 baseline | Momentum entries > blanket long |
| `s04_pepper_dip_buy_peak_sell.py` | EMA surf: dip-buy, peak-sell, floor 40 long | 216002 baseline | Capture oscillations on top of drift |
| `s05_pairs_long_pepper_short_ash.py` | Max long | Skewed short ASH ≈ -20 | Pairs hedge of market-wide risk |
| `s06_pepper_ladder_accumulate.py` | 4-level passive bid ladder | 216002 baseline | Accumulate cheaper via passive orders |
| `s07_pepper_trailing_stop.py` | Max long + trailing stop on 8-tick drawdown | 216002 baseline | Protect against day-close reversals |
| `s08_regime_adaptive.py` | Slope-detect: trend→max, chop→MM, fall→reduce | 216002 baseline | Adaptive per-regime sizing |
| `s09_combined_best.py` | Max long + dip overlay ladder | Tight MM with skew | Stacked best ideas |

## Testing order (recommended)

1. **s01 first** — isolate whether PEPPER utilization alone closes most of the gap.
   If this hits ~60-90k, the diagnosis was right.
2. **s09 next** — stacked best case, upper bound estimate.
3. **s03, s04, s07** — ride-the-trend refinements; pick whichever beats s01.
4. **s02 vs s01** — incremental ASH MM value.
5. **s05** — only if s01 shows PEPPER is trending as expected (pairs trade is
   a risk play; may underperform outright max-long).
6. **s06, s08** — fallbacks if s01 overbuys at bad prices.

## Notes

- All files use `datamodel` imports (standard Prosperity).
- Position limits assumed: ASH=50, PEPPER=80 (from baseline constants).
- ASH anchor fair = 10,000 (confirmed stationary around this value).
- EMA / history state stored in `state.traderData` JSON where used.
