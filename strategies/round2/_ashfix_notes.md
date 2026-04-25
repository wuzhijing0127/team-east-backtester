# ASH-Fix Variants (r2_ashfix_*)

## Why

271819 analysis: PEPPER made +79,255 in 5 trades. ASH lost **-62,411** across
7,082 round-trip sell-vs-buys with avg_buy=10,004 / avg_sell=9,996 → ~$9/unit
adverse selection × 7k+ trades.

The #1 lever from 17k → 120k is fixing ASH. These variants try four
structurally different repairs plus a combined option.

## Variant matrix

| File | ASH fix | Limit | Take edge | Passive quotes | Expected ASH PnL |
|------|---------|-------|-----------|----------------|-----------------|
| `r2_ashfix_01_scalp_only.py` | No MM, scalp only | 30 | 3 (deep) | **None** | ~0 to +small |
| `r2_ashfix_02_dynamic_fair.py` | EMA fair (α=0.01) | 50 | 2 | Normal MM around moving fair | 0 to +10k |
| `r2_ashfix_03_flatten_only.py` | Asymmetric quote | 40 | 2 | Only quote flattening side | ~0 |
| `r2_ashfix_04_small_limit.py` | Same MM, limit 20 | 20 | 1 | Normal MM | ~-15k (scaled bleed) |
| `r2_ashfix_05_combined.py` | EMA + asymmetric + small limit | 25 | 2 | Asymmetric flatten | 0 to +5k (safest) |

All variants:
- PEPPER = same max-long engine (+79k expected).
- MAF = 5,000 (rational mid-bid, ~25% of expected PEPPER +25%-MAF uplift).

## Expected total PnL (vs 271819's 17k baseline)

| Variant | ASH | PEPPER | Gross | MAF-paid | Net |
|---------|-----|--------|-------|----------|-----|
| 271819 baseline | -62 | +79 | 17 | 0 | 17 |
| r2_ashfix_01 | 0 | +79 | 79 | -5 | **74** |
| r2_ashfix_02 | +5 | +79 | 84 | -5 | **79** |
| r2_ashfix_03 | 0 | +79 | 79 | -5 | **74** |
| r2_ashfix_04 | -15 | +79 | 64 | -5 | 59 |
| r2_ashfix_05 | +3 | +79 | 82 | -5 | **77** |

With MAF WIN (+25% vol on PEPPER → +20k):

| Variant | Net with MAF-win |
|---------|------------------|
| r2_ashfix_01 | **94** |
| r2_ashfix_02 | **99** |
| r2_ashfix_03 | **94** |
| r2_ashfix_05 | **97** |

## Testing order

1. **`r2_ashfix_01_scalp_only`** first — simplest, safest, tests "does turning
   off MM stop the bleed?" If ASH goes to ~0, confirmed hypothesis.
2. **`r2_ashfix_02_dynamic_fair`** — if EMA fair catches drift, this could add
   small positive ASH PnL on top of scalping.
3. **`r2_ashfix_05_combined`** — expected best risk-adjusted outcome.
4. **`r2_ashfix_03` and `04`** — ablation / diagnostics, not final candidates.

## Structural assumptions baked in

- ASH mean drifts (not truly anchored at 10000); verified from 271819 data
  where avg mid ≈ 9997, not 10000.
- PEPPER position_limit=80, ASH position_limit varies per variant.
- Adverse selection is the core ASH enemy — symmetric passive MM is a trap.

## Still unknowns / risks

- MAF placement in return signature (module-level constant for now; adjust
  once API confirmed).
- Whether the +25% volume applies to visible order book depth or to
  position limits.
- Whether round 2 competition data day has the same drift characteristics.
