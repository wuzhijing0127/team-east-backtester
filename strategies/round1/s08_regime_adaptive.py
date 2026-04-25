"""
s08 — Regime-adaptive PEPPER
=============================
HYPOTHESIS: Detect whether PEPPER is trending (momentum up) or chopping,
and adapt: if trending → max long; if chopping → MM around EMA.
Uses short-window slope sign as regime indicator.

STRUCTURE:
- PEPPER: slope > threshold → accumulate to max; slope ≈ 0 → MM
          around EMA with ±3 spread; slope < -threshold → reduce to floor.
- ASH:    baseline 216002.
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
    "base_size": 20, "take_edge_neutral": 1, "take_edge_loaded": 0,
    "take_neutral_thr": 0.3, "tier_med": 0.4, "tier_high": 0.7, "tier_ext": 0.9,
}
PEPPER_LIMIT = 80
PEPPER_FLOOR = 20
WINDOW = 50
TREND_THR = 0.02      # slope per tick (mid units / tick) to call trending


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
    pos = state.position.get(prod, 0); limit = p["limit"]
    bb, ba = bb_ba(od); fair = p["fair"]; fr = round(fair)
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
    pos = state.position.get(prod, 0); limit = PEPPER_LIMIT
    bb, ba = bb_ba(od); mid = mid_of(od)
    orders = []
    if mid is None: return orders

    hist = ts.setdefault("pep_hist", [])
    hist.append(mid)
    if len(hist) > WINDOW: del hist[0]

    # Slope = (current - first) / window
    slope = 0.0
    if len(hist) >= 10:
        slope = (hist[-1] - hist[0]) / len(hist)

    if slope > TREND_THR:
        # Uptrend regime: max long
        if ba is not None:
            for ap in sorted(od.sell_orders):
                q = min(abs(od.sell_orders[ap]), limit - pos)
                if q > 0: orders.append(Order(prod, ap, q)); pos += q
                if pos >= limit: break
        rem = limit - pos
        if rem > 0 and bb is not None:
            orders.append(Order(prod, bb, rem))
    elif slope < -TREND_THR:
        # Downtrend regime: reduce to floor
        if pos > PEPPER_FLOOR and bb is not None:
            q = min(pos - PEPPER_FLOOR, 20)
            if q > 0: orders.append(Order(prod, bb, -q))
    else:
        # Chop regime: MM around EMA-style (last mid)
        ema = sum(hist[-10:]) / min(10, len(hist))
        if pos < limit and bb is not None and bb < ema - 1:
            q = min(15, limit - pos)
            orders.append(Order(prod, bb, q))
        if pos > PEPPER_FLOOR and ba is not None and ba > ema + 1:
            q = min(15, pos - PEPPER_FLOOR)
            orders.append(Order(prod, ba, -q))
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
