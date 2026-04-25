"""
r2_ashfix_02 — ASH: dynamic fair (EMA) + normal MM
===================================================
FIX: Track fair value as EMA of mid-price instead of hardcoded 10000.
This addresses adverse selection by letting fair drift with the market —
no more selling at 10001 when real fair is 10008.

TRADE-OFF: A moving fair can be pulled by noise; keep EMA slow enough to
resist manipulation (alpha = 0.01 → half-life ~70 ticks).

KEY DIFFERENCE FROM 271819: fair is ema, not 10000. Take edge = 2 (slightly
wider than baseline's 1 to add safety margin).

PEPPER: unchanged max-long.
MAF: 5000.
"""

import json
from datamodel import Order, OrderDepth, TradingState

# === MAF_HOOK ===
MAF = 5000
# ================

ASH_LIMIT = 50
ASH_INIT_FAIR = 10000
ASH_EMA_ALPHA = 0.01
ASH_TAKE_EDGE = 2
ASH_HALF_SPREAD = 2
PEPPER_LIMIT = 80


def bb_ba(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def mid_of(od):
    b, a = bb_ba(od)
    if b is None or a is None: return None
    return (b + a) / 2


def trade_ash(state, ts):
    prod = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(prod)
    if od is None: return []
    pos = state.position.get(prod, 0)
    bb, ba = bb_ba(od)
    mid = mid_of(od)

    fair = ts.get("ash_fair", ASH_INIT_FAIR)
    if mid is not None:
        fair = ASH_EMA_ALPHA * mid + (1 - ASH_EMA_ALPHA) * fair
        ts["ash_fair"] = fair
    fr = round(fair)
    orders = []

    if ba is not None:
        for ap in sorted(od.sell_orders):
            if ap <= fr - ASH_TAKE_EDGE and pos < ASH_LIMIT:
                q = min(abs(od.sell_orders[ap]), ASH_LIMIT - pos)
                if q > 0: orders.append(Order(prod, ap, q)); pos += q
            else: break
    if bb is not None:
        for bp_ in sorted(od.buy_orders, reverse=True):
            if bp_ >= fr + ASH_TAKE_EDGE and pos > -ASH_LIMIT:
                q = min(od.buy_orders[bp_], ASH_LIMIT + pos)
                if q > 0: orders.append(Order(prod, bp_, -q)); pos -= q
            else: break

    # Inventory-skewed passive quotes around dynamic fair
    skew = 2.0 * (pos / ASH_LIMIT)
    res = fair - skew
    rr = round(res)
    bid_px = rr - ASH_HALF_SPREAD
    ask_px = rr + ASH_HALF_SPREAD
    if bb is not None: bid_px = max(bid_px, bb)       # don't quote below best bid
    if ba is not None: ask_px = min(ask_px, ba)       # don't quote above best ask
    if bb is not None: bid_px = min(bid_px, (ba - 1) if ba else bid_px)
    if ba is not None: ask_px = max(ask_px, (bb + 1) if bb else ask_px)

    if pos < ASH_LIMIT and bid_px is not None:
        orders.append(Order(prod, int(bid_px), min(15, ASH_LIMIT - pos)))
    if pos > -ASH_LIMIT and ask_px is not None:
        orders.append(Order(prod, int(ask_px), -min(15, ASH_LIMIT + pos)))
    return orders


def trade_pepper(state):
    prod = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(prod)
    if od is None: return []
    pos = state.position.get(prod, 0)
    bb, ba = bb_ba(od)
    orders = []
    if ba is not None:
        for ap in sorted(od.sell_orders):
            q = min(abs(od.sell_orders[ap]), PEPPER_LIMIT - pos)
            if q > 0: orders.append(Order(prod, ap, q)); pos += q
            if pos >= PEPPER_LIMIT: break
    rem = PEPPER_LIMIT - pos
    if rem > 0 and bb is not None:
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
                orders[prod] = trade_pepper(state)
        return orders, 0, json.dumps(ts)
