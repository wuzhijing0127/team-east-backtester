"""
r3_refined_v1 — super-refined from ashfix batch winners
========================================================
BASED ON LIVE RESULTS from 5 ASH-fix variants on round 3 (100k-tick sim):
  1. r2_ashfix_04_small_limit   PnL=8,540 (winner) — small ASH limit (20) + normal MM
  2. r2_ashfix_02_dynamic_fair  PnL=8,034 — EMA-tracked fair value
  3. r2_ashfix_03_flatten_only  PnL=7,713
  4. r2_ashfix_05_combined      PnL=7,505
  5. r2_ashfix_01_scalp_only    PnL=7,291

KEY INSIGHT: ashfix_04 proved the -62k ASH bleed in round 1 was a
position-SIZE problem (limit=80), not a logic problem. The original
MM logic from 271819 works fine at smaller size.

DESIGN (stacking the two winners):
  ASH:
    - Small limit 20 (from ashfix_04)
    - Dynamic EMA fair (from ashfix_02, alpha=0.01)
    - Normal MM logic (half_spread=1, k_inv=2.5, base_size=8)
    - Take edge=1
  PEPPER:
    - Max long 80 (unchanged — the +79k engine)
    - Dip-overlay ladder when mid < EMA - 2 (from r2_s05)

MAF = 5000 (rational mid-bid; ignored by server if round 3 doesn't use MAF)
"""

import json
from datamodel import Order, OrderDepth, TradingState

# === MAF_HOOK ===
MAF = 0   # v2+: MAF useless in round 3
# ================

ASH_LIMIT = 20
ASH_INIT_FAIR = 10000
ASH_EMA_ALPHA = 0.05   # v3 ablation: 5x faster EMA (half-life ~14 ticks vs ~70)
ASH_HALF_SPREAD = 1
ASH_K_INV = 2.5
ASH_BASE_SIZE = 8
ASH_TAKE_EDGE = 1
ASH_MICRO_BETA = 0.04   # v16: test if peak is above 0.03

PEPPER_LIMIT = 80
PEPPER_EMA_ALPHA = 0.02
PEPPER_DIP_K = 2


def bb_ba(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def mid_of(od):
    b, a = bb_ba(od)
    if b is None or a is None:
        return None
    return (b + a) / 2


def microprice(od):
    """Volume-weighted fair — tilts toward heavier book side."""
    b, a = bb_ba(od)
    if b is None or a is None:
        return None
    bv = od.buy_orders[b]
    av = abs(od.sell_orders[a])
    t = bv + av
    if t == 0:
        return (b + a) / 2
    return (b * av + a * bv) / t


def trade_ash(state, ts):
    prod = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(prod)
    if od is None:
        return []
    pos = state.position.get(prod, 0)
    bb, ba = bb_ba(od)
    mid = mid_of(od)
    micro = microprice(od)

    ema_fair = ts.get("ash_fair", ASH_INIT_FAIR)
    if mid is not None:
        ema_fair = ASH_EMA_ALPHA * mid + (1 - ASH_EMA_ALPHA) * ema_fair
        ts["ash_fair"] = ema_fair

    # DYNAMIC FAIR: EMA + microprice tilt
    if mid is not None and micro is not None:
        fair = ema_fair + ASH_MICRO_BETA * (micro - mid)
    else:
        fair = ema_fair
    fr = round(fair)
    orders = []

    # Take at edge >= 1 (standard aggression)
    if ba is not None:
        for ap in sorted(od.sell_orders):
            if ap <= fr - ASH_TAKE_EDGE and pos < ASH_LIMIT:
                q = min(abs(od.sell_orders[ap]), ASH_LIMIT - pos)
                if q > 0:
                    orders.append(Order(prod, ap, q))
                    pos += q
            else:
                break
    if bb is not None:
        for bp_ in sorted(od.buy_orders, reverse=True):
            if bp_ >= fr + ASH_TAKE_EDGE and pos > -ASH_LIMIT:
                q = min(od.buy_orders[bp_], ASH_LIMIT + pos)
                if q > 0:
                    orders.append(Order(prod, bp_, -q))
                    pos -= q
            else:
                break

    # Reservation MM with inventory skew
    res = fair - ASH_K_INV * (pos / ASH_LIMIT)
    rr = round(res)
    bid_px = rr - ASH_HALF_SPREAD
    ask_px = rr + ASH_HALF_SPREAD
    if bb is not None:
        bid_px = min(bb + 1, bid_px)
    if ba is not None:
        ask_px = max(ba - 1, ask_px)
    if ba is not None:
        bid_px = min(bid_px, ba - 1)
    if bb is not None:
        ask_px = max(ask_px, bb + 1)

    if pos < ASH_LIMIT:
        orders.append(Order(prod, bid_px, min(ASH_BASE_SIZE, ASH_LIMIT - pos)))
    if pos > -ASH_LIMIT:
        orders.append(Order(prod, ask_px, -min(ASH_BASE_SIZE, ASH_LIMIT + pos)))
    return orders


def trade_pepper(state, ts):
    prod = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(prod)
    if od is None:
        return []
    pos = state.position.get(prod, 0)
    bb, ba = bb_ba(od)
    mid = mid_of(od)
    orders = []
    if mid is None:
        return orders

    ema = ts.get("pep_ema", mid)
    ema = PEPPER_EMA_ALPHA * mid + (1 - PEPPER_EMA_ALPHA) * ema
    ts["pep_ema"] = ema

    # Take all sell liquidity to max long
    if ba is not None:
        for ap in sorted(od.sell_orders):
            q = min(abs(od.sell_orders[ap]), PEPPER_LIMIT - pos)
            if q > 0:
                orders.append(Order(prod, ap, q))
                pos += q
            if pos >= PEPPER_LIMIT:
                break

    rem = PEPPER_LIMIT - pos
    if rem <= 0 or bb is None:
        return orders

    # Dip overlay: if mid is well below EMA, stack ladder for cheap entries
    if mid < ema - PEPPER_DIP_K:
        for offset, sz in [(0, 35), (-1, 25), (-2, 20)]:
            if rem <= 0:
                break
            q = min(sz, rem)
            px = bb + offset
            if ba is not None and px >= ba:
                px = ba - 1
            orders.append(Order(prod, px, q))
            rem -= q
    else:
        # Normal passive bid join
        orders.append(Order(prod, bb, rem))
    return orders


class Trader:
    def run(self, state):
        orders = {}
        ts = json.loads(state.traderData) if state.traderData else {}
        for prod in state.order_depths:
            if prod == "ASH_COATED_OSMIUM":
                orders[prod] = trade_ash(state, ts)
            elif prod == "INTARIAN_PEPPER_ROOT":
                orders[prod] = trade_pepper(state, ts)
        return orders, 0, json.dumps(ts)
