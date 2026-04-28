"""
Round 3 Strategy — r3_multi_v4.1
================================
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


def get_total_vev_delta(state) -> float:
    """Sum pos*delta across every VEV strike — directional VF exposure of options book."""
    total = 0.0
    for strike, delta in VEV_DELTA.items():
        pos = state.position.get(f"VEV_{strike}", 0)
        total += pos * delta
    return total


def get_vev_cluster_pos(state) -> int:
    """Combined position across the highly-correlated 5000/5100/5200 strikes."""
    return sum(state.position.get(f"VEV_{k}", 0) for k in VEV_CLUSTER_STRIKES)


# ============================================================
# HYDROGEL_PACK — anchored MM, anchor 10000
# ============================================================

HYDROGEL_PARAMS = {
    "limit": HYDROGEL_LIMIT,
    "anchor": 10000,
    "micro_beta": 0.15,
    "take_edge": 4,
    "half_spread": 2,
    "k_inv": 25.0,        # scaled with limit (was 10 @ limit 50)
    "base_size": 15,      # v3.2: 30 -> 15, tighter risk budget on HG
    "flatten_size": 25,
    "tier_med": 0.4,
    "tier_high": 0.7,
    "tier_extreme": 0.9,
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
    if best_bid is None and best_ask is None:
        return []

    timestamp = state.timestamp
    if timestamp >= UNWIND_START:
        return closeout_orders(product, pos, best_bid, best_ask, 20)

    fair = float(p["anchor"])
    micro = get_microprice(od)
    mid = get_mid(od)
    if micro is not None and mid is not None:
        fair += p["micro_beta"] * (micro - mid)

    # v2 mod #6: light drift-following bias on HG fair
    if "hg_prev_mid" in ts and mid is not None:
        drift = mid - ts["hg_prev_mid"]
        fair += 0.3 * drift
    if mid is not None:
        ts["hg_prev_mid"] = mid

    # v3 mod D: trend-regime detection on a rolling 20-mid window
    if mid is not None:
        ts.setdefault("hg_mids", []).append(mid)
        ts["hg_mids"] = ts["hg_mids"][-20:]
    hg_mids = ts.get("hg_mids", [])
    trend = (hg_mids[-1] - hg_mids[0]) if len(hg_mids) >= 10 else 0.0
    trend_mode = abs(trend) >= 5  # v3.2 H2: engage earlier

    # v3 mod E: per-product loss-based de-risking
    risk_mult = update_risk_mult(product, pos, mid, ts)

    fair_r = round(fair)

    orders: List[Order] = []

    inv_frac = pos / limit if limit else 0
    buy_te = p["take_edge"] + (1 if inv_frac > 0.4 else (-1 if inv_frac < -0.4 else 0))
    sell_te = p["take_edge"] - (1 if inv_frac > 0.4 else (-1 if inv_frac < -0.4 else 0))
    buy_te = max(0, buy_te)
    sell_te = max(0, sell_te)

    # Aggressive takes
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

    # Maker quotes
    inv_frac = pos / limit if limit else 0
    reservation = fair - p["k_inv"] * inv_frac * abs(inv_frac)
    res_r = round(reservation)

    # v3 mod D: in trend mode, widen spread and shrink size to fade getting run over
    if trend_mode:
        p_base = 10
        p_half_spread = 4
    else:
        p_base = p["base_size"]
        p_half_spread = p["half_spread"]

    hs = p_half_spread
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
    buy_qty = min(round(p_base * buy_mult * risk_mult), limit - pos)
    sell_qty = min(round(p_base * sell_mult * risk_mult), limit + pos)

    # v3.2 H1: in a trend, only quote the side that fades the trend.
    # Uptrend (trend>0) — keep buying off, would still want to sell into strength.
    # Wait — opposite. If price keeps rising, our shorts get steamrolled. Kill sells.
    if trend_mode:
        if trend > 0:
            sell_qty = 0
        elif trend < 0:
            buy_qty = 0

    if buy_qty > 0:
        orders.append(Order(product, bid_price, buy_qty))
    if sell_qty > 0:
        orders.append(Order(product, ask_price, -sell_qty))

    # Flatten extreme inventory at fair
    if abs(pos) >= p["tier_extreme"] * limit:
        if pos > 0 and best_bid is not None:
            fq = min(p["flatten_size"], pos)
            orders.append(Order(product, fair_r, -fq))
        elif pos < 0 and best_ask is not None:
            fq = min(p["flatten_size"], -pos)
            orders.append(Order(product, fair_r, fq))

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

    # Deep ITM still carries some time value — add a +2 premium so we don't undervalue.
    fair = max(0.5, vf_mid - strike + 2)
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
    bid_price = res_r - 1
    ask_price = res_r + 1
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


def trade_vev_near(state: TradingState, ts: dict, strike: int) -> List[Order]:
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
    total_delta = get_total_vev_delta(state)
    cluster_pos = get_vev_cluster_pos(state)
    in_cluster = strike in VEV_CLUSTER_STRIKES

    timestamp = state.timestamp
    if timestamp >= UNWIND_START:
        return closeout_orders(product, pos, best_bid, best_ask, 40)

    mid = get_mid(od)
    key = f"vev{strike}_fair"
    if key not in ts:
        if mid is None:
            return []
        ts[key] = mid
    if mid is not None:
        ts[key] = p["ema_alpha"] * mid + (1 - p["ema_alpha"]) * ts[key]
    fair = ts[key]

    # --- v3 mod B: ADAPTIVE theta — moneyness-driven instead of strike-step ---
    # moneyness = strike - vf_mid (positive = OTM, negative = ITM)
    vf_mid = ts.get("vf_mid")
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

    # Aggressive takes — v2 mod #4: require +1 extra tick of edge
    take_edge = p["take_edge"]
    # v4: take_edge bump removed; the C2 tiered fair markdown is the dominant
    # protective effect now (a -6.0 fair shift is far stronger than a +2 take_edge bump).
    if best_ask is not None:
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price <= fair_r - take_edge - 1:
                vol = abs(od.sell_orders[ask_price])
                qty = min(vol, limit - pos)
                if qty > 0:
                    orders.append(Order(product, ask_price, qty))
                    pos += qty
            else:
                break
    if best_bid is not None:
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price >= fair_r + take_edge + 1:
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
    # Emergency cross at extreme single-strike position.
    if pos > 240 and best_bid is not None:
        cross_qty = 22 if pos > 280 else 12
        cross_qty = min(cross_qty, pos, limit + pos)
        if cross_qty > 0:
            orders.append(Order(product, best_bid, -cross_qty))
    elif pos < -240 and best_ask is not None:
        cross_qty = 22 if pos < -280 else 12
        cross_qty = min(cross_qty, -pos, limit - pos)
        if cross_qty > 0:
            orders.append(Order(product, best_ask, cross_qty))

    # v3.3 P1: total VEV-delta cap — stop adding correlated risk when basket too long/short
    if total_delta > 350:
        buy_qty = 0
        if sell_qty < p["base_size"]:
            sell_qty = p["base_size"]
        sell_qty = min(sell_qty, limit + pos)
    elif total_delta < -350:
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

    # If we somehow ended up long, try to sell at 1 (penny scalp).
    if pos > 0:
        ask_price = 1
        if best_bid is not None:
            ask_price = max(ask_price, best_bid + 1)
        sell_qty = min(10, pos)
        if sell_qty > 0:
            orders.append(Order(product, ask_price, -sell_qty))

    # Passive bid only at floor in case someone dumps below 0.5
    # (mid is pinned at 0.5 → only an aggressive seller could cross at 0; skip to avoid junk).
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

        for k in VEV_STRIKES_DEEP_ITM:
            sym = f"VEV_{k}"
            if sym in state.order_depths:
                orders[sym] = trade_vev_deep_itm(state, ts, k)

        for k in VEV_STRIKES_NEAR:
            sym = f"VEV_{k}"
            if sym in state.order_depths:
                orders[sym] = trade_vev_near(state, ts, k)

        for k in VEV_STRIKES_FAR_OTM:
            sym = f"VEV_{k}"
            if sym in state.order_depths:
                orders[sym] = trade_vev_far_otm(state, ts, k)

        # v3.4 P3': PASSIVE-ONLY partial VF delta hedge.
        # Only triggers above a high delta threshold; tiny weight; tiny per-tick cap;
        # never crosses the spread (avoids the spread bleed that wrecked v3.3).
        vf_sym = "VELVETFRUIT_EXTRACT"
        if vf_sym in state.order_depths and state.timestamp < UNWIND_START:
            total_delta = get_total_vev_delta(state)
            if abs(total_delta) >= 500:
                vf_pos = state.position.get(vf_sym, 0)
                target_vf = -round(0.10 * total_delta)
                hedge_qty = max(-5, min(5, target_vf - vf_pos))
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
