# Round 2 Strategy Design Notes

## The MAF game (what we know)

- You declare a **Market Access Fee (MAF)** in your Python program.
- If your MAF is in the **top 50%** of all submitted MAFs → you pay it and get **+25% volume access**.
- If not in top 50% → you pay nothing but get nothing extra.
- This is a **first-price sealed-bid auction** with capped downside: losing costs 0.

## Strategic framing

Let `U` = expected PnL uplift from +25% volume access (for a volume-bottlenecked
strategy like max-long PEPPER, realistic estimate is 15–25% of baseline PnL).

Let `C` = unknown 50%-cutoff bid (depends on other teams).

**Rational bid**: `MAF = min(U - ε, C + δ)`. You lose nothing by losing, but
you overpay if you bid above cutoff. Bid somewhere between 20% and 60% of U.

## Data check (round 2 training days -1, 0, 1)

| Product | Day -1 mid | Day 0 mid | Day 1 mid | Pattern |
|---------|-----------|-----------|-----------|---------|
| ASH     | ~10000    | ~10000    | ~10000    | Stationary |
| PEPPER  | ~11500    | ~12500    | ~13500    | +1000/day trend continues |

**Conclusion**: core strategy insights from round 1 hold. PEPPER max-long
still dominates. MAF buys faster fills on both products.

## MAF API placeholder

Since no spec exists, every file declares `MAF = <int>` at module top and
returns `(orders, conversions, traderData)` as round 1. **Adjust the return
signature once the actual API is announced** — likely to `(orders, conversions,
traderData, MAF)` or similar. Search each file for `# MAF_HOOK` to locate.

## Variant matrix

| File | MAF | PEPPER structure | ASH structure | Hypothesis |
|------|-----|------------------|--------------|------------|
| `r2_s01_no_maf_baseline.py` | 0 | Max long 80 | Tight MM | Control — test if MAF matters at all |
| `r2_s02_maf_low.py` | 2000 | Max long 80 | Tight MM | Cheap shot at winning cutoff |
| `r2_s03_maf_mid.py` | 5000 | Max long + bigger fills | Tight MM + wider size | Rational bid, uses extra volume |
| `r2_s04_maf_high.py` | 10000 | Max long + stack overlay | Tight MM large size | High bid to likely secure top 50% |
| `r2_s05_maf_dip_overlay.py` | 5000 | EMA dip-buy stack | Baseline MM | Extra volume amplifies dip entries |
| `r2_s06_maf_pairs.py` | 3000 | Max long 80 | Skewed short (-20 target) | Pairs hedge with MAF |
| `r2_s07_maf_regime.py` | 5000 | Regime-adaptive | Baseline MM | Adaptive size by trend strength |
| `r2_s08_maf_full_attack.py` | 15000 | Max long + aggressive dip ladder | Tight MM huge size | All-in: bet on winning, max size use |

## Test playbook

1. Upload `r2_s01_no_maf_baseline.py` first — this is your "free" baseline.
2. Upload `r2_s02` and `r2_s04` — low-bid vs high-bid with same strategy.
   If both lose MAF auction, they fall back to baseline; if either wins, delta
   reveals the 25% volume uplift.
3. Upload `r2_s03`, `r2_s05`, `r2_s07` — rational mid-MAF, structurally varied.
4. Upload `r2_s08` — upper bound / all-in.
5. Whichever maximises PnL, submit.

**Warning**: since each file is one MAF bid, strategies differ only in MAF level
*and* in the downstream use of the extra volume. Don't interpret s01 vs s02
as same strategy — s02 also tunes sizes assuming it may win the 25% boost.
