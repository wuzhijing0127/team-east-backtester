"""
Round 3 Strategy — r3_multi_v10_blend
======================================
Same scaffold as v8 but the option fair is now MARKET-ANCHORED:

  fair = 0.3 * bs_fair + 0.7 * ema_of_mid
  delta uses bisection-implied vol from per-strike market mid

Rationale: the v9 disaster (-264k) showed the static smile σ doesn't fit
live mids. Reverting to market-driven fair (with BS as a 30% sanity overlay)
restores ~v3.4-era option PnL behavior while keeping the unified-engine
architecture and v8's HG/VF/hedge improvements.

Other v10_blend changes:
  Q2_KEPT. Inventory shading: |pos|>200 → shade fair by up to 1 tick against pos.
  Q3_KEPT. Hedge trigger 150 (v9 evidence: helped VF +500).

(Original v8 docstring follows.)

----

Round 3 Strategy — r3_multi_v8
==============================
v8 deltas vs v7 (which scored +3,505 — VF +2255, HG +607, options +643):

  P1. FULL OPTION BOOK on the smile. SMILE_STRIKES now includes 4000/4500.
      Function renamed trade_vev_near → trade_vev_bs and dispatches all 8
      strikes (4000..5500). The legacy trade_vev_deep_itm parity engine is
      no longer dispatched — the global quadratic σ(m) parabola handles
      4000/4500 too, eliminating "spread bleed between pricing regimes."
  P2. SIGNAL AGGRESSION. RESID_AGGRESSION = 0.75 multiplier on the residual
      entry threshold (was 1.0). Effective requirement ≈ 0.75-sigma off the
      curve; lifts wing-strike trade frequency.
  P3. SHORT-ONLY FAR-OTM. trade_vev_far_otm sells at 1 only — no buys at 0.
      Accumulates shorts up to soft_cap (60), harvesting the 0.5-tick
      premium with effectively zero vol/theta exposure.
  P4. NEAR-PERFECT DELTA HEDGE. Hedge weight 0.75 → 0.95 (cap and trigger
      unchanged at ±30 / |Δ|≥250).
  P5. HG IMBALANCE SHARPENING. imb_coef 0.40 → 0.55. Sharper book-skew
      response on HG; HG still has NO anchor — pure flow MM.

Original v7 docstring follows for reference:

----

Round 3 Strategy — r3_multi_v7
==============================
v7 = v6 (smile-driven BS option engine) + flow-driven HG MM (asset_only/v5).
The v6 anchored-at-10000 HG model was the chronic loss source; replacing it
with the flow MM that scored +607 in r3_asset_only_v1 should lift total PnL
without disturbing the option engine.

CHANGES vs v6:
  HG. Anchored 10000 model REPLACED with flow-driven passive MM:
      - fair = mid + 0.50·(micro - mid) + 0.40·imb·spread
      - half_spread = 3, base_size = 20, take_edge = 8 (rare crosses)
      - soft_cap = 150 (50-lot buffer to limit), k_inv = 0.10 (~0.6 ticks
        of skew at full inventory — passive unwind via inside-the-book quotes)
  DEEP-ITM. v6's fixed +2 premium replaced with BS-derived premium capped at
            +2: fair = max(0.5, intrinsic + min(2.0, bs_premium)). Tighter
            on quiet ticks; preserves the +2 ceiling for safety.

KEPT FROM v6 (untouched):
  - Smile-driven BS (Phase 1): SMILE_A=2.308, SMILE_B=0.0526, SMILE_C=0.02047,
    m = log(K/S)/sqrt(T_days), T in DAYS.
  - Pure BS fair on near-VEVs (Phase 2): no EMA blend, residual entry filter
    (vega · 0.009 = 1-sigma threshold, floored at 1.0 ticks).
  - SMILE_STRIKES = (5000..5500) only; deep-ITM and far-OTM use other engines.
  - trade_vev_far_otm quotes 0/1 around the 0.5 floor (Phase 3.1).
  - get_total_vev_delta uses live smile-σ deltas (Phase 4).
  - v5_HEDGE: |total_delta| ≥ 250, weight 0.75, cap ±30, passive only.
  - v5_P1 circuit breaker at ±250 in trade_vev_near.
  - C1 / C1' two-stage cluster unwind, C2 tiered fair markdown, P2 cluster
    ±450 same-side kill, L_v44_1 linear half_spread on near-VEVs.

Original v6 docstring follows for reference:

----

Round 3 Strategy — r3_multi_v6
==============================
v6 = v5_bs upgraded to a stationary fitted smile + day-based BS + residual
entry. Builds on the analyst report that the smile is a fixed parabolic
surface in m = log(K/S)/sqrt(T_days).

PHASE 1: MATHEMATICAL RE-CALIBRATION
  - get_tte_days(ts) replaces get_tte_years; T = max(0.001, 5 - ts/1M).
  - smile_iv(K, S, T_days) returns sigma(m) = 2.308·m^2 + 0.0526·m + 0.02047
    (m = log(K/S)/sqrt(T_days)). Hardcoded coefficients from analyst fit.
  - bs_call_price / bs_call_delta now expect T in DAYS and per-strike sigma.

PHASE 2: SIGNAL GENERATION (residual-based)
  - In trade_vev_near: pure BS fair (NO EMA blend). Model is "ground truth".
  - 1-sigma entry filter: only take/quote toward model when
    |market_iv - model_iv| > 0.009  (≈ 0.5–2 price ticks).
    Approximated cheaply as |market_price - model_price| > vega·0.009.
  - Smile model only valid for strikes 5000–5500. Deep-ITM 4000/4500 still
    use parity engine; far-OTM 6000/6500 see Phase 3.1.

PHASE 3: TACTICAL POSITION MANAGEMENT
  - trade_vev_far_otm (6000/6500): tight bid/ask AROUND 0.5 (was passive
    sell-only). Quote 0/1 spread on small size to harvest the floor.
  - L_v44_1 linear-scaled inventory skew already in trade_vev_near (kept).
  - v5_HEDGE keeps its threshold/weight/cap but now uses the smile sigmas
    via get_total_vev_delta.

PHASE 4: OPERATIONAL DEPLOYMENT
  - TTE=5d at round start handled by the days formula.
  - v5_P1 circuit breaker preserved at ±250 (matches VF limit).

Original v5_bs docstring follows for reference:

----

Round 3 Strategy — r3_multi_v5 (BS prototype, predecessor to v6)
================================================================
v5 architectural upgrade: explicit Black-Scholes pricing with continuous TTE,
dynamic implied volatility, and tighter delta-hedging. Replaces v4.4's pure
EMA-of-mid pricing with BS-anchored fair values blended against the EMA.

  v5_BS. CONTINUOUS BS PRICING ENGINE.
         - TTE = max(0.5d, 5d - timestamp/1M) / 365 (continuous fractional decay)
         - Implied vol tracked from VEV_5200 (closest to ATM): bisection per tick,
           smoothed via EMA (alpha=0.10) for stability.
         - Per-strike BS fair = bs_call_price(VF_mid, K, TTE, IV).
         - Final fair = 0.7 * BS_fair + 0.3 * EMA_of_mid (blend for robustness).

  v5_HEDGE. STRONGER PASSIVE VF HEDGE (vs v4.4).
            Trigger: |total_delta| ≥ 250 (was 400).
            Weight:  0.75 (was 0.60).
            Cap:     ±30 (was ±20).
            Still posts INSIDE-MARKET PASSIVE — never crosses (avoids the v3.3
            spread-bleed disaster). Higher cap = bigger queue, faster fills.

  v5_DELTA. DYNAMIC BS DELTAS (replaces static VEV_DELTA dict in delta math).
            get_total_vev_delta now uses live bs_call_delta(S, K, T, IV) per strike.
            Reflects current vol regime, not stale historical regression.

  v5_P1. TIGHTER BASKET-DELTA CIRCUIT BREAKER (vs v3.3 P1).
         Threshold: ±250 (was ±350). Matches VF position limit of 200 — at
         this delta we can't fully hedge with VF, so we stop adding more.

PRESERVED FROM v4.4 (untouched): linear-scaled half_spread (L_v44_1), C1/C1' two-stage
cluster unwind, C2 tiered fair markdown, C3'' graduated per-strike ladder
(kill-buy/sell-floor/dual-gated emergency cross at cluster_pos>400 + pos>240),
P2 cluster ±450 same-side kill, HG/VF/deep-ITM/far-OTM engines.

v4.4 deltas vs v4.3 — two changes addressing the t=59,000 PnL drop:
  1) the VF hedge was too sluggish to keep up with delta swings, and
  2) the unwind quote spread was binary (kill or floor) rather than gradual.

  H_v44_1. RESPONSIVE PASSIVE HEDGE (replaces P3').
           Threshold: |total_delta| >= 400 (was 500).
           Weight: 0.60 (was 0.10).
           Per-tick cap: ±20 (was ±5).
           Still posts INSIDE-MARKET PASSIVE — never crosses.
           Net: when basket delta swings, our passive bid/ask sits much closer
           to mid with much larger size, fills come through faster, and we
           still pay zero spread on hedge fills.

  L_v44_1. LINEAR-SCALED UNWIND SPREAD on near-VEVs and deep-ITM VEVs.
           Replace symmetric maker spread with position-asymmetric:
             pos > 0 (long):  ask gets TIGHTER (closer to res), bid gets WIDER
             pos < 0 (short): bid gets TIGHTER (closer to res), ask gets WIDER
           Skew amount = |pos/limit| * 2.0 ticks.
           Combined with existing reservation skew (k_inv * inv²), this gives
           gradual unwind pressure instead of waiting for the >180/>240 ladder
           to fire. Reduces inventory duration without crossing.

DEFERRED from user's analysis:
  - Dynamic vol (#2) and TTE intraday (#4): both require introducing an explicit
    Black-Scholes engine. Current EMA-of-mid implicitly absorbs vol and TTE
    decay because the market itself prices them. Would need a v5 architecture.
  - Basket delta cap (#5): already exists as P1 at threshold 350.

v4.3 delta vs v4.2 — additionally require basket-level stress before crossing.
v4.2 already gated the emergency cross to cluster strikes (in_cluster). But it
still fired on a single strike at >240 even when the rest of the cluster was
fine — paying spread when the basket was actually balanced.

v4.3 adds a `cluster_pos > 400` requirement so the cross only triggers when
BOTH signals agree:
  - Basket-wide stress: |cluster_pos| > 400
  - Per-strike loading: |pos| > 240

Net effect: emergency crossing becomes much rarer and only fires when truly
needed. Quotes stay wide and cheap most of the time. C1 (cluster_pos>500
passive unwind), C2 (tiered fair markdown), kill-buy at >120, sell-floor at
>180, P1, P2, P3' VF hedge, HG, and all other engines unchanged.

v4.2 delta vs v4.1 — gate the C3'' emergency cross to cluster strikes only.
v4.1 applied the cross to all near-VEVs, but VEV_5300/5400/5500 are small-dollar
options where the spread cost would dominate any drawdown. Cluster strikes
(5000/5100/5200) remain protected by the cross; the kill-buy and sell-floor
parts of the ladder still apply to all near-VEVs.

  Change (single line in two places, inside trade_vev_near):
    Before: if pos > 240 and best_bid is not None:
    After:  if in_cluster and pos > 240 and best_bid is not None:
    (and the symmetric short-side check)

C1' (two-stage cluster unwind), C2 (tiered fair markdown), kill-buy at >120,
sell-floor at >180, P1, P2, P3', and all non-VEV engines: untouched.

v4.1 deltas vs v4 — passive-only unwind can still hold too long when the
market keeps drifting against us. v4.1 adds urgency tiers:

  C1'. TWO-STAGE CLUSTER UNWIND (replaces v4 C1).
       When cluster_pos > 500 and pos > 80:
         pos > 240 → CROSS at best_bid for min(15, ...) (urgency)
         else      → passive at best_ask for unwind_qty
       Symmetric for short side. Returns early either way.

  C3''. GRADUATED PER-STRIKE LADDER on near-VEVs (replaces v4 C3 single-tier).
        Per-strike position thresholds add discrete pressure stages:
          pos >  120  → kill buy_qty (stop adding long)
          pos >  180  → kill buy_qty AND floor sell_qty at 18 (passive sell-only)
          pos >  240  → above + emergency cross 12 lots at best_bid
          pos >  280  → above + emergency cross 22 lots at best_bid (bigger)
        Symmetric for short side. Quotes stay wide most of the time, but
        a single stuck strike at 280+ now bleeds via cross every tick.

C2 (tiered fair markdown), P1 (total-delta cap), P2 (cluster ±450 kill),
P3' (passive VF hedge), HG/VF/deep-ITM/far-OTM are all preserved from v4.

v4 deltas vs v3.4 — VEV cluster ROTATION (not just caps). v3.4/v3.6 backtest:
VEV_5000/5100/5200 each parked at +300 from t≈50k–70k while option mids drifted
against the basket. The cluster cap (>450 kill same-side) was too soft and
fired too late; the markdown (fair -= 1.0) was too small relative to per-strike
positions of +300.

v4 strengthens the cluster controls along three axes:

  C1. CLUSTER PASSIVE UNWIND (top of trade_vev_near, after orders=[]).
      When cluster_pos > 500 AND this strike's pos > 80 → join best_ask passively
      with up to 20 lots and RETURN (skip everything else for this strike this tick).
      Symmetric for short side. Trades inventory rotation as a hard
      first-priority — but never crosses.

  C2. TIERED FAIR MARKDOWN replacing the v3.3 P4 single-tier (-1.0 / +2 take_edge):
        cluster_pos >  450  → fair -= 6.0
        cluster_pos >  250  → fair -= 3.0     (else)
        cluster_pos < -450  → fair += 6.0
        cluster_pos < -250  → fair += 3.0     (else)
      The take_edge += 2 bump is removed — the bigger fair shift dominates.

  C3. PER-STRIKE INVENTORY CAP (NEW — most important).
        in_cluster AND pos >  240  → buy_qty = 0,  sell_qty = max(sell_qty, 18)
        in_cluster AND pos < -240  → sell_qty = 0, buy_qty  = max(buy_qty, 18)
      Catches the case where a single strike inflates to +300 even while
      cluster_pos is below the cluster threshold.

P1 (total-delta cap), P2 (cluster ±450 same-side kill), P3' (passive VF hedge),
HG, VF, deep-ITM, far-OTM are all untouched from v3.4.

v3.4 deltas vs v3.3 — v3.3's P3 hedge was a spread bleed; v3.4 made it
passive-only with a high trigger.

This revision keeps P1/P2/P4 untouched; P3 is rewritten:
  P3'. PASSIVE VF HEDGE: hedge only when |total_delta| >= 500 (was always),
       at 0.10x weighting (was 0.35x), capped at ±5/tick (was ±20), and posts
       INSIDE-MARKET PASSIVE — never crosses the spread. If it doesn't fill,
       we try again next tick. Spread cost goes from "every tick" to "zero".

v3.3 portfolio-risk additions (preserved unchanged):

  P1. TOTAL VEV-DELTA CAP: when sum of pos*delta across all 10 strikes
      exceeds ±350, force-close adds on the wrong side of trade_vev_near.
  P2. CLUSTER CAP: VEV_5000/5100/5200 specifically — if their combined position
      crosses ±450, kill same-side maker quotes.
  P3'. (rewritten — see top of v3.4 notes above)
  P4. CLUSTER EDGE WIDEN: in trade_vev_near, if strike in {5000,5100,5200}
      and cluster_pos > 300, take_edge += 2 and fair -= 1.0.

v3.2 deltas vs v3 — HG was still bleeding (-3.5k by t=2800).

  H1. Trend-mode kills the wrong-side maker (no shorting in uptrend / buying in
      downtrend). Bot stays in market but stops getting steamrolled.
  H2. Trend-mode trigger lowered: |trend| >= 5 (was 8) so we engage earlier.
  H3. HG base_size 30 -> 15 (risk budget, not a kill switch).
  V1. Near-VEV base_size 10 -> 12 (option side is profitable; small uplift).

v3 deltas vs v2: soft adaptive opinions instead of hard rules.
  A. SOFT cross-strike clip (replaces hard min/max of mod #2) — partial pull only
     when fair leaves the dynamic per-strike spread band.
  B. ADAPTIVE theta — moneyness-driven (strike − vf_mid) instead of fixed strike
     thresholds; gentler magnitudes (0.015 / 0.04 / 0.08).
  C. INVENTORY-SIZED near-VEV maker quotes via inventory_size_multiplier (mod #3
     keeps base_size=10 but no longer pumps both sides full size).
  D. HYDROGEL trend-regime quoter — rolling 20-mid window; when |range|>=8 we
     widen half_spread and shrink base_size to fade getting steamrolled in trends.
  E. PER-PRODUCT loss-based de-risking — when last-tick adverse PnL crosses a
     threshold, halve maker size for that product until conditions reset.

v2 lineage (preserved): theta bias direction, deep-ITM +2 premium, edge filter,
HG/VF drift bias, near-VEV large-long penalty. Items #2 (hard clip) and #1
(strike-step theta) are the ones replaced this round.

Twelve products, three engines, one Trader.

Products:
  HYDROGEL_PACK         anchored MM around 10000 (ASH-style)
  VELVETFRUIT_EXTRACT   anchored MM around 5250, EMA-tracked (publishes vf_mid)
  VEV_4000, VEV_4500    deep ITM — parity MM with fair = vf_mid - strike + 2
  VEV_5000..VEV_5500    near/at/just-OTM — EMA-of-mid + theta + monotonicity
  VEV_6000, VEV_6500    floor-pinned — passive only, fair = 0.5

Framework reused from round2/round2 final log and algo/361111.py.
TTE_AT_ROUND_START = 5 Solvenarian days at start of round 3.
"""

import json
from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List, Tuple, Optional


# ============================================================
# CONSTANTS
# ============================================================

SESSION_END = 1000000
UNWIND_START = 998000
HARD_UNWIND = 999000

HYDROGEL_LIMIT = 200
VF_LIMIT = 200
VEV_LIMIT = 300

# TTE in Solvenarian days at the start of round 3. Decays to 0 over the round.
# v1 absorbs theta implicitly via EMA-of-mid on each VEV (the market already prices it),
# but TTE_AT_ROUND_START is exposed for v2 cross-strike vol/theta logic.
TTE_AT_ROUND_START = 5

VEV_STRIKES_DEEP_ITM = (4000, 4500)
VEV_STRIKES_NEAR = (5000, 5100, 5200, 5300, 5400, 5500)
VEV_STRIKES_FAR_OTM = (6000, 6500)

# Hardcoded empirical deltas (from day-1 regression) — used only if cross-hedge enabled
VEV_DELTA = {
    4000: 1.00, 4500: 1.00,
    5000: 0.93, 5100: 0.81, 5200: 0.58,
    5300: 0.37, 5400: 0.16, 5500: 0.07,
    6000: 0.00, 6500: 0.00,
}

CROSS_HEDGE = False  # off in v1


# ============================================================
# HELPERS (lifted from 361111.py)
# ============================================================

def get_best_bid_ask(od: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def get_mid(od: OrderDepth) -> Optional[float]:
    bid, ask = get_best_bid_ask(od)
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def get_microprice(od: OrderDepth) -> Optional[float]:
    bid, ask = get_best_bid_ask(od)
    if bid is None or ask is None:
        return None
    bid_vol = od.buy_orders[bid]
    ask_vol = abs(od.sell_orders[ask])
    total = bid_vol + ask_vol
    if total == 0:
        return (bid + ask) / 2
    return (bid * ask_vol + ask * bid_vol) / total


def get_l1_imbalance(od: OrderDepth) -> float:
    bid, ask = get_best_bid_ask(od)
    if bid is None or ask is None:
        return 0.0
    bid_vol = od.buy_orders.get(bid, 0)
    ask_vol = abs(od.sell_orders.get(ask, 0))
    total = bid_vol + ask_vol
    if total == 0:
        return 0.0
    return (bid_vol - ask_vol) / total


def inventory_size_multiplier(position, limit, tier_med, tier_high, tier_extreme):
    frac = abs(position) / limit if limit else 0
    if frac >= tier_extreme:
        add_mult = 0.0
    elif frac >= tier_high:
        add_mult = 0.25
    elif frac >= tier_med:
        add_mult = 0.5
    else:
        add_mult = 1.0
    if position > 0:
        return add_mult, 1.0
    if position < 0:
        return 1.0, add_mult
    return 1.0, 1.0


def closeout_orders(product, pos, best_bid, best_ask, flat_size):
    out = []
    if pos > 0 and best_bid is not None:
        qty = min(flat_size, pos)
        out.append(Order(product, best_bid, -qty))
    elif pos < 0 and best_ask is not None:
        qty = min(flat_size, -pos)
        out.append(Order(product, best_ask, qty))
    return out


def soft_clip_fair(fair, lower_bound, upper_bound, strength=0.25):
    """Pull `fair` partway toward an enclosing band rather than hard-clipping."""
    if fair > upper_bound:
        return fair - strength * (fair - upper_bound)
    if fair < lower_bound:
        return fair + strength * (lower_bound - fair)
    return fair


def update_risk_mult(product, pos, mid, ts, adverse_threshold=200, decay=0.05):
    """v3 mod E: per-product loss-based de-risking.

    Tracks last mid; if last-tick adverse PnL (move-against-position * |pos|)
    exceeds threshold, halves the size multiplier for this product. Multiplier
    decays back toward 1.0 each tick we don't get hit again.
    """
    prev_key = f"{product}_prev_mid"
    mult_key = f"{product}_risk_mult"

    if mid is not None:
        if prev_key in ts:
            # PnL of holding `pos` through the last tick.
            pnl_tick = (mid - ts[prev_key]) * pos
            if pnl_tick < -adverse_threshold:
                ts[mult_key] = 0.5
        ts[prev_key] = mid

    cur = ts.get(mult_key, 1.0)
    if cur < 1.0:
        cur = min(1.0, cur + decay)
        ts[mult_key] = cur
    return cur


# v3.3: portfolio-risk helpers
VEV_CLUSTER_STRIKES = (5000, 5100, 5200)


# v6: Black-Scholes engine with DAY-based time + hardcoded fitted smile.
# (Replaces v5_bs's year-based bisection-IV approach.)
import math
TICKS_PER_DAY = 1_000_000

# v6 Phase 1: hardcoded fitted smile in log-moneyness/sqrt(T_days):
#   sigma(m) = SMILE_A * m^2 + SMILE_B * m + SMILE_C
#   m = log(K/S) / sqrt(T_days)
SMILE_A = 2.308
SMILE_B = 0.0526
SMILE_C = 0.02047

# v6 Phase 2: 1-sigma residual entry threshold (in IV units).
SMILE_RESID_SD = 0.009
# v8 P2: aggression multiplier on the residual entry threshold (1.0 = strict 1-sigma).
RESID_AGGRESSION = 0.75

# Strikes the smile model is calibrated on (excludes deep-ITM 4000/4500).
# v8 P1: include deep-ITM 4000/4500 in the global parabola.
SMILE_STRIKES = (4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_call_price(S: float, K: float, T: float, sigma: float, r: float = 0.0) -> float:
    """T is in DAYS. sigma is calibrated for the day convention."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, S - K)
    sqT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqT)
    d2 = d1 - sigma * sqT
    return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)


def bs_call_delta(S: float, K: float, T: float, sigma: float, r: float = 0.0) -> float:
    """T in days."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1)


def bs_call_vega(S: float, K: float, T: float, sigma: float, r: float = 0.0) -> float:
    """vega = dC/dsigma. T in days. Used for the 1-sigma price threshold."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    sqT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqT)
    return S * norm_pdf(d1) * sqT


def get_tte_days(timestamp: int) -> float:
    """Continuous TTE in DAYS. Round 3 starts at 5d, decays linearly to ~4d
    by end of session (1M ticks). Floor at 0.001 days for numerical safety."""
    days_remaining = TTE_AT_ROUND_START - (timestamp / TICKS_PER_DAY)
    return max(0.001, days_remaining)


def smile_iv(K: float, S: float, T_days: float) -> float:
    """Fitted parabolic smile (kept as fallback when bisection fails)."""
    if T_days <= 0 or S <= 0 or K <= 0:
        return SMILE_C
    m = math.log(K / S) / math.sqrt(T_days)
    sigma = SMILE_A * m * m + SMILE_B * m + SMILE_C
    return max(0.001, sigma)


def implied_vol_call(target_price, S, K, T_days, tol=1e-3, max_iter=20):
    """Bisection: invert BS to find sigma matching the market price."""
    intrinsic = max(0.0, S - K)
    if target_price <= intrinsic + 1e-6 or T_days <= 0:
        return None
    lo, hi = 0.001, 3.0
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        p = bs_call_price(S, K, T_days, mid)
        if p > target_price:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def get_market_iv(state, ts, strike):
    """Live implied vol for a single strike from its market mid. Cached in ts."""
    od = state.order_depths.get(f"VEV_{strike}")
    if od is None or not od.buy_orders or not od.sell_orders:
        return None
    bid = max(od.buy_orders); ask = min(od.sell_orders)
    mid_px = 0.5 * (bid + ask)
    vf_mid = ts.get("vf_mid")
    if vf_mid is None:
        return None
    T = get_tte_days(state.timestamp)
    iv = implied_vol_call(mid_px, vf_mid, strike, T)
    if iv is None or not (0.001 < iv < 3.0):
        return None
    key = f"iv_{strike}"
    prev = ts.get(key, iv)
    smoothed = 0.9 * prev + 0.1 * iv  # EMA smooth
    ts[key] = smoothed
    return smoothed


def get_total_vev_delta(state, ts: dict = None) -> float:
    """Sum pos*delta. v10_blend: use bisection-IV per strike when available,
    otherwise fall back to smile σ, otherwise static empirical."""
    total = 0.0
    if ts is not None:
        vf_mid = ts.get("vf_mid")
        if vf_mid is not None:
            T = get_tte_days(state.timestamp)
            for strike in VEV_DELTA:
                pos = state.position.get(f"VEV_{strike}", 0)
                if pos == 0:
                    continue
                iv = ts.get(f"iv_{strike}")
                if iv is None:
                    iv = smile_iv(strike, vf_mid, T)
                d = bs_call_delta(vf_mid, strike, T, iv)
                total += pos * d
            return total
    for strike, delta in VEV_DELTA.items():
        pos = state.position.get(f"VEV_{strike}", 0)
        total += pos * delta
    return total


def get_vev_cluster_pos(state) -> int:
    """Combined position across the highly-correlated 5000/5100/5200 strikes."""
    return sum(state.position.get(f"VEV_{k}", 0) for k in VEV_CLUSTER_STRIKES)


# ============================================================
# HYDROGEL_PACK — flow-driven passive MM (asset_only_v1 lineage; v5 / v7)
# Replaces the v6 anchored-at-10000 model that was a chronic loss source.
# ============================================================

HYDROGEL_PARAMS = {
    "limit": HYDROGEL_LIMIT,
    "half_spread": 3,
    "base_size": 20,
    "k_inv": 0.10,
    "soft_cap": 150,
    "take_edge": 8,
    "micro_coef": 0.50,
    "imb_coef": 0.55,  # v8 P5: 0.40 -> 0.55, sharper book-skew response
}


def trade_hydrogel(state: TradingState, ts: dict) -> List[Order]:
    product = "HYDROGEL_PACK"
    p = HYDROGEL_PARAMS
    od = state.order_depths.get(product)
    if od is None:
        return []

    pos = state.position.get(product, 0)
    limit = p["limit"]
    best_bid, best_ask = get_best_bid_ask(od)
    if best_bid is None or best_ask is None:
        return []

    timestamp = state.timestamp
    if timestamp >= UNWIND_START:
        return closeout_orders(product, pos, best_bid, best_ask, 20)

    # Flow-driven fair: mid + microprice tilt + L1-imbalance × spread.
    mid = get_mid(od)
    micro = get_microprice(od)
    imb = get_l1_imbalance(od)
    spread = best_ask - best_bid
    tilt = p["micro_coef"] * (micro - mid) if micro is not None and mid is not None else 0.0
    fair = mid + tilt + p["imb_coef"] * imb * spread

    orders: List[Order] = []

    soft_cap = p["soft_cap"]
    half_spread = p["half_spread"]
    take_edge = p["take_edge"]
    base_size = p["base_size"]

    # Aggressive takes only on large dislocations (>= 8 ticks from fair).
    for ap in sorted(od.sell_orders.keys()):
        if fair - ap < take_edge:
            break
        room = max(0, limit - pos)
        qty = min(abs(od.sell_orders[ap]), room, base_size)
        if qty > 0:
            orders.append(Order(product, ap, qty))
            pos += qty
    for bp in sorted(od.buy_orders.keys(), reverse=True):
        if bp - fair < take_edge:
            break
        room = max(0, limit + pos)
        qty = min(od.buy_orders[bp], room, base_size)
        if qty > 0:
            orders.append(Order(product, bp, -qty))
            pos -= qty

    # Reservation skewed lightly by inventory (~0.6 ticks at full soft_cap).
    skew = p["k_inv"] * (pos / max(1, soft_cap)) * max(2, half_spread * 2)
    res = fair - skew
    bid_price = int(math.floor(res - half_spread))
    ask_price = int(math.ceil(res + half_spread))

    # Inside-the-book by 1 tick when allowed.
    if best_bid + 1 < best_ask and bid_price > best_bid:
        bid_price = best_bid + 1
    if best_ask - 1 > best_bid and ask_price < best_ask:
        ask_price = best_ask - 1
    bid_price = min(bid_price, best_ask - 1)
    ask_price = max(ask_price, best_bid + 1)

    room_buy = max(0, soft_cap - pos)
    room_sell = max(0, soft_cap + pos)
    b_size = min(base_size, room_buy)
    a_size = min(base_size, room_sell)
    if b_size > 0 and bid_price > 0:
        orders.append(Order(product, bid_price, b_size))
    if a_size > 0 and ask_price > 0:
        orders.append(Order(product, ask_price, -a_size))

    return orders


# ============================================================
# VELVETFRUIT_EXTRACT — EMA-tracked anchored MM (and publishes vf_mid)
# ============================================================

VF_PARAMS = {
    "limit": VF_LIMIT,
    "init_anchor": 5250,
    "ema_alpha": 0.02,
    "micro_beta": 0.05,
    "take_edge": 3,
    "half_spread": 1,
    "k_inv": 15.0,        # scaled with limit (was 6 @ limit 50)
    "base_size": 25,      # ~12% of 200 limit
    "flatten_size": 20,
    "tier_med": 0.4,
    "tier_high": 0.7,
    "tier_extreme": 0.9,
}


def trade_velvetfruit(state: TradingState, ts: dict) -> List[Order]:
    product = "VELVETFRUIT_EXTRACT"
    p = VF_PARAMS
    od = state.order_depths.get(product)
    if od is None:
        return []

    pos = state.position.get(product, 0)
    limit = p["limit"]
    best_bid, best_ask = get_best_bid_ask(od)
    if best_bid is None and best_ask is None:
        return []

    mid = get_mid(od)
    if mid is not None:
        ts["vf_mid"] = mid  # publish for VEV engines

    timestamp = state.timestamp
    if timestamp >= UNWIND_START:
        return closeout_orders(product, pos, best_bid, best_ask, 20)

    # EMA fair
    if "vf_fair" not in ts:
        ts["vf_fair"] = float(p["init_anchor"]) if mid is None else mid
    if mid is not None:
        ts["vf_fair"] = p["ema_alpha"] * mid + (1 - p["ema_alpha"]) * ts["vf_fair"]
    fair = ts["vf_fair"]
    micro = get_microprice(od)
    if micro is not None and mid is not None:
        fair += p["micro_beta"] * (micro - mid)

    # v2 mod #7: VF drift-following bias (smaller weight than HG since VF has tighter σ)
    if "vf_prev_mid" in ts and mid is not None:
        drift = mid - ts["vf_prev_mid"]
        fair += 0.2 * drift
    if mid is not None:
        ts["vf_prev_mid"] = mid

    # v3 mod E: per-product loss-based de-risking
    risk_mult = update_risk_mult(product, pos, mid, ts)

    fair_r = round(fair)

    orders: List[Order] = []

    inv_frac = pos / limit if limit else 0
    buy_te = p["take_edge"] + (1 if inv_frac > 0.4 else (-1 if inv_frac < -0.4 else 0))
    sell_te = p["take_edge"] - (1 if inv_frac > 0.4 else (-1 if inv_frac < -0.4 else 0))
    buy_te = max(0, buy_te)
    sell_te = max(0, sell_te)

    if best_ask is not None:
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price <= fair_r - buy_te:
                vol = abs(od.sell_orders[ask_price])
                qty = min(vol, limit - pos)
                if qty > 0:
                    orders.append(Order(product, ask_price, qty))
                    pos += qty
            else:
                break
    if best_bid is not None:
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price >= fair_r + sell_te:
                vol = od.buy_orders[bid_price]
                qty = min(vol, limit + pos)
                if qty > 0:
                    orders.append(Order(product, bid_price, -qty))
                    pos -= qty
            else:
                break

    inv_frac = pos / limit if limit else 0
    reservation = fair - p["k_inv"] * inv_frac * abs(inv_frac)
    res_r = round(reservation)
    hs = p["half_spread"]
    bid_price = res_r - hs
    ask_price = res_r + hs
    if best_bid is not None:
        bid_price = min(best_bid + 1, bid_price)
    if best_ask is not None:
        ask_price = max(best_ask - 1, ask_price)
    if best_ask is not None:
        bid_price = min(bid_price, best_ask - 1)
    if best_bid is not None:
        ask_price = max(ask_price, best_bid + 1)

    buy_mult, sell_mult = inventory_size_multiplier(
        pos, limit, p["tier_med"], p["tier_high"], p["tier_extreme"]
    )
    buy_qty = min(round(p["base_size"] * buy_mult * risk_mult), limit - pos)
    sell_qty = min(round(p["base_size"] * sell_mult * risk_mult), limit + pos)
    if buy_qty > 0:
        orders.append(Order(product, bid_price, buy_qty))
    if sell_qty > 0:
        orders.append(Order(product, ask_price, -sell_qty))

    if abs(pos) >= p["tier_extreme"] * limit:
        if pos > 0 and best_bid is not None:
            fq = min(p["flatten_size"], pos)
            orders.append(Order(product, fair_r, -fq))
        elif pos < 0 and best_ask is not None:
            fq = min(p["flatten_size"], -pos)
            orders.append(Order(product, fair_r, fq))

    return orders


# ============================================================
# VEV — Tier A: deep ITM parity MM (4000, 4500)
# ============================================================

def trade_vev_deep_itm(state: TradingState, ts: dict, strike: int) -> List[Order]:
    product = f"VEV_{strike}"
    od = state.order_depths.get(product)
    if od is None:
        return []
    pos = state.position.get(product, 0)
    limit = VEV_LIMIT
    best_bid, best_ask = get_best_bid_ask(od)
    if best_bid is None and best_ask is None:
        return []

    timestamp = state.timestamp
    if timestamp >= UNWIND_START:
        return closeout_orders(product, pos, best_bid, best_ask, 50)

    vf_mid = ts.get("vf_mid")
    if vf_mid is None:
        return []  # need underlying first

    # v7 PHASE 3.2: BS-derived deep-ITM premium, capped at +2 for safety.
    # The fitted smile gives a tiny but non-zero time value above intrinsic on
    # K=4000/4500. Use it dynamically; clip at +2 to avoid extrapolation when vf_mid
    # walks far from the calibrated range.
    T_days = get_tte_days(timestamp)
    sigma_k = smile_iv(strike, vf_mid, T_days)
    bs_fair = bs_call_price(vf_mid, strike, T_days, sigma_k)
    intrinsic = max(0.0, vf_mid - strike)
    bs_premium = max(0.0, bs_fair - intrinsic)
    fair = max(0.5, intrinsic + min(2.0, bs_premium))
    fair_r = round(fair)

    orders: List[Order] = []

    # Aggressive parity arb takes — edge = 1 tick (deviations seen in data are larger)
    take_edge = 1
    if best_ask is not None:
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price <= fair_r - take_edge:
                vol = abs(od.sell_orders[ask_price])
                qty = min(vol, limit - pos)
                if qty > 0:
                    orders.append(Order(product, ask_price, qty))
                    pos += qty
            else:
                break
    if best_bid is not None:
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price >= fair_r + take_edge:
                vol = od.buy_orders[bid_price]
                qty = min(vol, limit + pos)
                if qty > 0:
                    orders.append(Order(product, bid_price, -qty))
                    pos -= qty
            else:
                break

    # Passive parity quotes inside best
    inv_frac = pos / limit if limit else 0
    res = fair - 8.0 * inv_frac * abs(inv_frac)   # scaled k_inv with 300 limit
    res_r = round(res)

    # v4.4 L_v44_1: linear-scaled asymmetric half_spread for deep-ITM too
    skew = abs(inv_frac) * 2.0
    if pos > 0:
        hs_bid = 1 + skew
        hs_ask = max(0.0, 1 - skew)
    elif pos < 0:
        hs_bid = max(0.0, 1 - skew)
        hs_ask = 1 + skew
    else:
        hs_bid = hs_ask = 1
    bid_price = res_r - round(hs_bid)
    ask_price = res_r + round(hs_ask)
    if best_bid is not None:
        bid_price = min(best_bid + 1, bid_price)
    if best_ask is not None:
        ask_price = max(best_ask - 1, ask_price)
    if best_ask is not None:
        bid_price = min(bid_price, best_ask - 1)
    if best_bid is not None:
        ask_price = max(ask_price, best_bid + 1)

    base = 25            # ~8% of 300 limit (deep ITM is the safest VEV — slightly larger)
    buy_qty = min(base, limit - pos)
    sell_qty = min(base, limit + pos)
    # Damp at extreme
    if abs(pos) >= 0.85 * limit:
        if pos > 0:
            buy_qty = 0
        else:
            sell_qty = 0
    if buy_qty > 0:
        orders.append(Order(product, bid_price, buy_qty))
    if sell_qty > 0:
        orders.append(Order(product, ask_price, -sell_qty))

    return orders


# ============================================================
# VEV — Tier B: near/at/just-OTM EMA-MM (5000..5500)
# ============================================================

VEV_NEAR_PARAMS = {
    "limit": VEV_LIMIT,
    "ema_alpha": 0.02,
    "take_edge": 3,
    "half_spread": 1,
    "k_inv": 8.0,         # scaled with 300 limit
    "base_size": 12,      # v3.2 V1: 10 -> 12 small uplift now that VEV side is healthy
    "tier_extreme": 0.85,
}


def trade_vev_bs(state: TradingState, ts: dict, strike: int) -> List[Order]:
    """v8 P1: BS-driven engine for ALL non-floor strikes (4000..5500).
    Renamed from trade_vev_near. The smile parabola handles deep-ITM 4000/4500
    too — eliminates the spread-bleed between the parity engine and the BS engine."""
    product = f"VEV_{strike}"
    p = VEV_NEAR_PARAMS
    od = state.order_depths.get(product)
    if od is None:
        return []
    pos = state.position.get(product, 0)
    limit = p["limit"]
    best_bid, best_ask = get_best_bid_ask(od)
    if best_bid is None and best_ask is None:
        return []

    # v3.3 portfolio-risk reads (computed once per call)
    total_delta = get_total_vev_delta(state, ts)  # v5: BS-dynamic delta
    cluster_pos = get_vev_cluster_pos(state)
    in_cluster = strike in VEV_CLUSTER_STRIKES

    timestamp = state.timestamp
    if timestamp >= UNWIND_START:
        return closeout_orders(product, pos, best_bid, best_ask, 40)

    mid = get_mid(od)

    # v6 PHASE 1+2: pure BS fair from fitted smile (NO EMA blend).
    # Smile is calibrated only on K=5000..5500. For other strikes (shouldn't reach
    # this function) we'd need a different engine.
    vf_mid = ts.get("vf_mid")
    T_days = get_tte_days(timestamp)
    if vf_mid is None or strike not in SMILE_STRIKES:
        return []

    # v10_blend: per-strike implied vol from market mid, used for delta and BS_fair.
    iv_market = get_market_iv(state, ts, strike)
    sigma_k = iv_market if iv_market is not None else smile_iv(strike, vf_mid, T_days)
    bs_fair = bs_call_price(vf_mid, strike, T_days, sigma_k)

    # v10_blend: track per-strike EMA-of-mid for the market anchor leg.
    mid = get_mid(od)
    ema_key = f"vev{strike}_ema"
    if ema_key not in ts:
        ts[ema_key] = mid if mid is not None else bs_fair
    if mid is not None:
        ts[ema_key] = 0.05 * mid + 0.95 * ts[ema_key]
    ema_fair = ts[ema_key]

    # MARKET-ANCHORED BLEND: 70% market EMA + 30% BS sanity overlay.
    fair = 0.3 * bs_fair + 0.7 * ema_fair

    # v10_blend Q2_KEPT: inventory shading.
    if abs(pos) > 200:
        shade = min(1.0, (abs(pos) - 200) / 100.0)
        if pos > 0:
            fair -= shade
        else:
            fair += shade

    # 1-sigma residual entry filter.
    vega_k = bs_call_vega(vf_mid, strike, T_days, sigma_k)
    resid_thresh = max(0.5, vega_k * SMILE_RESID_SD * RESID_AGGRESSION)

    # --- v3 mod B: ADAPTIVE theta — moneyness-driven instead of strike-step ---
    # moneyness = strike - vf_mid (positive = OTM, negative = ITM)
    moneyness = (strike - vf_mid) if vf_mid is not None else 0.0
    if moneyness > 200:
        theta_decay = 0.08
    elif moneyness > 0:
        theta_decay = 0.04
    else:
        theta_decay = 0.015
    if pos > 0:
        fair -= theta_decay
    elif pos < 0:
        fair += theta_decay * 0.5

    # --- v2 mod #8: large-long penalty (discourage stacking near options) ---
    if pos > 50:
        fair -= 1.5

    # --- v3 mod A: SOFT cross-strike clip with per-strike spread cap ---
    spread_cap = {
        5000: 90, 5100: 80, 5200: 65,
        5300: 50, 5400: 35, 5500: 25,
    }.get(strike, 60)
    lower_key = f"vev{strike - 100}_fair"
    higher_key = f"vev{strike + 100}_fair"
    upper_bound = ts[higher_key] + spread_cap if higher_key in ts else float("inf")
    lower_bound = ts[lower_key] - spread_cap if lower_key in ts else float("-inf")
    fair = soft_clip_fair(fair, lower_bound, upper_bound, strength=0.25)

    # --- v4 C2: TIERED cluster fair markdown (replaces v3.3 P4 single-tier) ---
    if in_cluster:
        if cluster_pos > 450:
            fair -= 6.0
        elif cluster_pos > 250:
            fair -= 3.0
        elif cluster_pos < -450:
            fair += 6.0
        elif cluster_pos < -250:
            fair += 3.0

    # --- v3 mod E: per-product loss-based de-risking ---
    risk_mult = update_risk_mult(product, pos, mid, ts)

    fair_r = round(fair)

    orders: List[Order] = []

    # v4.1 C1': TWO-STAGE CLUSTER UNWIND — passive when bearable, cross when urgent.
    # When cluster_pos is extreme AND this strike is meaningfully loaded, place ONE
    # order and return. Choice between cross-the-spread (urgent) and join-the-book
    # (passive) is made by per-strike position.
    if in_cluster:
        if cluster_pos > 500 and pos > 80:
            unwind_qty = min(20, pos, limit + pos)
            if unwind_qty > 0:
                if pos > 240 and best_bid is not None:
                    # Urgent: cross at best_bid to actually reduce.
                    orders.append(Order(product, best_bid, -min(15, unwind_qty)))
                elif best_ask is not None:
                    # Bearable: passive join at best_ask.
                    orders.append(Order(product, best_ask, -unwind_qty))
                return orders
        elif cluster_pos < -500 and pos < -80:
            unwind_qty = min(20, -pos, limit - pos)
            if unwind_qty > 0:
                if pos < -240 and best_ask is not None:
                    orders.append(Order(product, best_ask, min(15, unwind_qty)))
                elif best_bid is not None:
                    orders.append(Order(product, best_bid, unwind_qty))
                return orders

    # v6 PHASE 2: aggressive takes require the residual to exceed 1 SMILE_RESID_SD.
    # Threshold (in PRICE units) = max(0.5, vega · 0.009). For ATM ~$95 voucher with
    # vega ~25, that's ~0.225 → floored to 0.5. For deeper OTM with smaller vega,
    # threshold remains 0.5 minimum. Combined with the legacy +1 tick edge filter
    # so trivial 1-tick noise still doesn't trigger.
    if best_ask is not None:
        for ask_price in sorted(od.sell_orders.keys()):
            if (fair - ask_price) >= max(resid_thresh, 1.0):
                vol = abs(od.sell_orders[ask_price])
                qty = min(vol, limit - pos)
                if qty > 0:
                    orders.append(Order(product, ask_price, qty))
                    pos += qty
            else:
                break
    if best_bid is not None:
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if (bid_price - fair) >= max(resid_thresh, 1.0):
                vol = od.buy_orders[bid_price]
                qty = min(vol, limit + pos)
                if qty > 0:
                    orders.append(Order(product, bid_price, -qty))
                    pos -= qty
            else:
                break

    # Maker quotes
    inv_frac = pos / limit if limit else 0
    res = fair - p["k_inv"] * inv_frac * abs(inv_frac)
    res_r = round(res)

    # v4.4 L_v44_1: LINEAR-SCALED asymmetric half_spread.
    # Long → tighten ask (more aggressive sell), widen bid (less aggressive buy).
    # Short → mirror. Skew amount scales linearly with |pos|/limit.
    hs_base = p["half_spread"]
    skew = abs(inv_frac) * 2.0
    if pos > 0:
        hs_bid = hs_base + skew
        hs_ask = max(0.0, hs_base - skew)
    elif pos < 0:
        hs_bid = max(0.0, hs_base - skew)
        hs_ask = hs_base + skew
    else:
        hs_bid = hs_ask = hs_base

    bid_price = res_r - round(hs_bid)
    ask_price = res_r + round(hs_ask)
    if best_bid is not None:
        bid_price = min(best_bid + 1, bid_price)
    if best_ask is not None:
        ask_price = max(best_ask - 1, ask_price)
    if best_ask is not None:
        bid_price = min(bid_price, best_ask - 1)
    if best_bid is not None:
        ask_price = max(ask_price, best_bid + 1)
    # Don't quote below 1 (price floor)
    bid_price = max(1, bid_price)
    ask_price = max(1, ask_price)

    # v3 mod C: inventory-based maker sizing to avoid one-sided option stacks
    buy_mult, sell_mult = inventory_size_multiplier(
        pos, limit, 0.25, 0.50, p["tier_extreme"]
    )
    buy_qty = min(round(p["base_size"] * buy_mult * risk_mult), limit - pos)
    sell_qty = min(round(p["base_size"] * sell_mult * risk_mult), limit + pos)
    if abs(pos) >= p["tier_extreme"] * limit:
        if pos > 0:
            buy_qty = 0
        else:
            sell_qty = 0

    # v4.1 C3'': GRADUATED PER-STRIKE LADDER for near-VEVs.
    # Replaces v4's single-tier cap. Adds tiered urgency by per-strike position.
    if pos > 120:
        buy_qty = 0
    elif pos < -120:
        sell_qty = 0
    if pos > 180:
        sell_qty = min(max(sell_qty, 18), limit + pos)
    elif pos < -180:
        buy_qty = min(max(buy_qty, 18), limit - pos)
    # v4.3: Emergency cross requires CLUSTER STRESS + per-strike loading.
    # cluster_pos > 400 ensures the basket is actually heavy, not just one strike.
    if in_cluster and cluster_pos > 400 and pos > 240 and best_bid is not None:
        cross_qty = 22 if pos > 280 else 12
        cross_qty = min(cross_qty, pos, limit + pos)
        if cross_qty > 0:
            orders.append(Order(product, best_bid, -cross_qty))
    elif in_cluster and cluster_pos < -400 and pos < -240 and best_ask is not None:
        cross_qty = 22 if pos < -280 else 12
        cross_qty = min(cross_qty, -pos, limit - pos)
        if cross_qty > 0:
            orders.append(Order(product, best_ask, cross_qty))

    # v5_P1: TIGHTER total VEV-delta circuit breaker (matches VF limit of 200).
    # When |delta| > 250, we can't fully hedge with VF — stop adding more delta.
    if total_delta > 250:
        buy_qty = 0
        if sell_qty < p["base_size"]:
            sell_qty = p["base_size"]
        sell_qty = min(sell_qty, limit + pos)
    elif total_delta < -250:
        sell_qty = 0
        if buy_qty < p["base_size"]:
            buy_qty = p["base_size"]
        buy_qty = min(buy_qty, limit - pos)

    # v3.3 P2: cluster cap — same-side kill when 5000/5100/5200 combined position is huge
    if in_cluster:
        if cluster_pos > 450:
            buy_qty = 0
        elif cluster_pos < -450:
            sell_qty = 0

    if buy_qty > 0:
        orders.append(Order(product, bid_price, buy_qty))
    if sell_qty > 0:
        orders.append(Order(product, ask_price, -sell_qty))

    return orders


# ============================================================
# VEV — Tier C: floor-pinned passive (6000, 6500)
# ============================================================

def trade_vev_far_otm(state: TradingState, ts: dict, strike: int) -> List[Order]:
    """v8 P3: SHORT-ONLY at 1. The 6000/6500 mid is pinned at 0.5; we cannot
    realistically buy at 0 (no rational seller will hit the floor). The only
    profitable move is to sell passively at 1 and harvest the 0.5-tick premium.
    Accumulate shorts up to a soft_cap; expiry/cash-settlement at zero pays
    out the full 1.0 received.
    """
    product = f"VEV_{strike}"
    od = state.order_depths.get(product)
    if od is None:
        return []
    pos = state.position.get(product, 0)
    limit = VEV_LIMIT
    best_bid, best_ask = get_best_bid_ask(od)

    timestamp = state.timestamp
    if timestamp >= UNWIND_START:
        return closeout_orders(product, pos, best_bid, best_ask, 20)

    orders: List[Order] = []

    soft_cap = 60     # max accumulated short
    base_size = 15    # slightly larger than v7's 10 since we only quote one side
    if pos > -soft_cap:
        ask_px = 1
        if best_bid is not None:
            ask_px = max(ask_px, best_bid + 1)
        sell_qty = min(base_size, soft_cap + pos)
        if sell_qty > 0:
            orders.append(Order(product, ask_px, -sell_qty))

    return orders


# ============================================================
# MAIN TRADER
# ============================================================

class Trader:
    def bid(self):
        # Round 3 access fee placeholder — same conservative posture as round 2.
        return 800

    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        orders: Dict[str, List[Order]] = {}
        conversions = 0
        ts = json.loads(state.traderData) if state.traderData else {}

        # VF first so vf_mid is available to VEV engines this tick.
        if "VELVETFRUIT_EXTRACT" in state.order_depths:
            orders["VELVETFRUIT_EXTRACT"] = trade_velvetfruit(state, ts)

        if "HYDROGEL_PACK" in state.order_depths:
            orders["HYDROGEL_PACK"] = trade_hydrogel(state, ts)

        # v8 P1: route ALL non-floor strikes (4000..5500) through the BS engine.
        for k in VEV_STRIKES_DEEP_ITM + VEV_STRIKES_NEAR:
            sym = f"VEV_{k}"
            if sym in state.order_depths:
                orders[sym] = trade_vev_bs(state, ts, k)

        for k in VEV_STRIKES_FAR_OTM:
            sym = f"VEV_{k}"
            if sym in state.order_depths:
                orders[sym] = trade_vev_far_otm(state, ts, k)

        # v5_HEDGE: STRONGER PASSIVE VF HEDGE.
        # Threshold |total_delta| ≥ 250 (matches VF limit), weight 0.75, cap ±30.
        # Still posts inside-market passively — never crosses (preserves anti-bleed).
        vf_sym = "VELVETFRUIT_EXTRACT"
        if vf_sym in state.order_depths and state.timestamp < UNWIND_START:
            total_delta = get_total_vev_delta(state, ts)  # v5: BS-dynamic
            if abs(total_delta) >= 150:  # v10_blend Q3_KEPT: 250 -> 150
                vf_pos = state.position.get(vf_sym, 0)
                target_vf = -round(0.95 * total_delta)  # v8 P4: 0.75 -> 0.95
                hedge_qty = max(-30, min(30, target_vf - vf_pos))
                if hedge_qty > 0:
                    hedge_qty = min(hedge_qty, VF_LIMIT - vf_pos)
                elif hedge_qty < 0:
                    hedge_qty = max(hedge_qty, -VF_LIMIT - vf_pos)
                if hedge_qty != 0:
                    vf_od = state.order_depths[vf_sym]
                    vf_bid, vf_ask = get_best_bid_ask(vf_od)
                    # Inside-market PASSIVE post — counterparty must come to us.
                    if hedge_qty > 0 and vf_bid is not None and vf_ask is not None:
                        px = min(vf_bid + 1, vf_ask - 1)
                        if px >= vf_bid + 1:  # only post if it improves the bid
                            orders.setdefault(vf_sym, []).append(Order(vf_sym, px, hedge_qty))
                    elif hedge_qty < 0 and vf_bid is not None and vf_ask is not None:
                        px = max(vf_ask - 1, vf_bid + 1)
                        if px <= vf_ask - 1:  # only post if it improves the ask
                            orders.setdefault(vf_sym, []).append(Order(vf_sym, px, hedge_qty))

        return orders, conversions, json.dumps(ts)
