"""
s03 — PEPPER momentum/breakout + ASH baseline
==============================================
HYPOTHESIS: Blanket max-long (s01) over-buys during intra-day pullbacks.
A momentum strategy — buy only on new rolling highs — gets better entry
prices while still riding the drift.

STRUCTURE:
- PEPPER: track rolling high over last N ticks. Only add to position
          when mid >= rolling_high - 1. Otherwise hold existing position
          (never reduce unless trend breaks: mid drops below EMA by >5).
- ASH:    baseline 216002 logic.
"""

import json
from datamodel import Order, OrderDepth, TradingState


def bb_ba(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def mid_of(od):
    b, a = bb_ba(od)
    if b is None or a is None: return None
    return (b + a) / 2


ASH_P = {
    "limit": 50, "fair": 10000, "half_spread": 1, "k_inv": 2.5,
    "base_size": 20, "flatten_size": 10, "take_edge_neutral": 1,
    "take_edge_loaded": 0, "take_neutral_thr": 0.3,
    "tier_med": 0.4, "tier_high": 0.7, "tier_ext": 0.9,
}
PEPPER_LIMIT = 80
LOOKBACK = 30      # ticks for rolling high
TREND_BREAK = 5    # mid below EMA by >5 → dump


def size_mult(pos, limit, p):
    frac = abs(pos) / limit if limit else 0
    if frac >= p["tier_ext"]: add = 0.0
    elif frac >= p["tier_high"]: add = 0.25
    elif frac >= p["tier_med"]: add = 0.5
    else: add = 1.0
    if pos > 0: return add, 1.0
    if pos < 0: return 1.0, add
    return 1.0, 1.0


def trade_ash(state):
    prod = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(prod)
    if od is None: return []
    p = ASH_P
    pos = state.position.get(prod, 0)
    limit = p["limit"]
    bb, ba = bb_ba(od)
    fair = p["fair"]; fr = round(fair)
    orders = []
    inv_frac = abs(pos) / limit
    buy_te = sell_te = p["take_edge_neutral"] if inv_frac < p["take_neutral_thr"] else p["take_edge_loaded"]
    if pos < 0: buy_te = max(buy_te, p["take_edge_neutral"])
    if pos > 0: sell_te = max(sell_te, p["take_edge_neutral"])
    if ba is not None:
        for ap in sorted(od.sell_orders):
            if ap <= fr - buy_te:
                q = min(abs(od.sell_orders[ap]), limit - pos)
                if q > 0: orders.append(Order(prod, ap, q)); pos += q
            else: break
    if bb is not None:
        for bp_ in sorted(od.buy_orders, reverse=True):
            if bp_ >= fr + sell_te:
                q = min(od.buy_orders[bp_], limit + pos)
                if q > 0: orders.append(Order(prod, bp_, -q)); pos -= q
            else: break
    res = fair - p["k_inv"] * (pos / limit); rr = round(res)
    bp = rr - p["half_spread"]; ap = rr + p["half_spread"]
    if bb is not None: bp = min(bb + 1, bp)
    if ba is not None: ap = max(ba - 1, ap)
    if ba is not None: bp = min(bp, ba - 1)
    if bb is not None: ap = max(ap, bb + 1)
    bm, sm = size_mult(pos, limit, p)
    bq = min(round(p["base_size"] * bm), limit - pos)
    sq = min(round(p["base_size"] * sm), limit + pos)
    if bq > 0: orders.append(Order(prod, bp, bq))
    if sq > 0: orders.append(Order(prod, ap, -sq))
    return orders


def trade_pepper(state, ts):
    prod = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(prod)
    if od is None: return []
    pos = state.position.get(prod, 0)
    limit = PEPPER_LIMIT
    bb, ba = bb_ba(od)
    mid = mid_of(od)
    orders = []

    # Rolling mid history
    hist = ts.setdefault("pep_hist", [])
    if mid is not None:
        hist.append(mid)
        if len(hist) > LOOKBACK: del hist[0]
    ema = ts.get("pep_ema", mid)
    if mid is not None and ema is not None:
        ema = 0.02 * mid + 0.98 * ema
        ts["pep_ema"] = ema

    if mid is None or len(hist) < 5:
        return orders

    roll_high = max(hist)
    # Breakout: add only at new highs (within 1 tick)
    if mid >= roll_high - 1 and pos < limit:
        if ba is not None:
            q = min(abs(od.sell_orders[ba]), limit - pos)
            if q > 0:
                orders.append(Order(prod, ba, q)); pos += q

    # Trend break: if mid drops far below EMA, reduce
    if ema is not None and mid < ema - TREND_BREAK and pos > 0:
        if bb is not None:
            q = min(pos, 20)
            orders.append(Order(prod, bb, -q))
    return orders


class Trader:
    def run(self, state):
        orders = {}
        ts = json.loads(state.traderData) if state.traderData else {}
        for prod in state.order_depths:
            if prod == "ASH_COATED_OSMIUM":
                orders[prod] = trade_ash(state)
            elif prod == "INTARIAN_PEPPER_ROOT":
                orders[prod] = trade_pepper(state, ts)
        return orders, 0, json.dumps(ts)
