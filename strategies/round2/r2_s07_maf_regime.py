"""
r2_s07 — Mid MAF + regime adaptive
===================================
MAF = 5000. Detect PEPPER regime (trend / chop / fall) via slope, size
adaptively. Extra 25% volume access amplifies good-regime fills.

In uptrend: max long with larger ladders (uses extra volume most).
In chop: MM around EMA (moderate sizes).
In downtrend: reduce to floor, protect capital.
"""

import json
from datamodel import Order, OrderDepth, TradingState

# === MAF_HOOK ===
MAF = 5000
# ================

def bb_ba(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def mid_of(od):
    b, a = bb_ba(od)
    if b is None or a is None: return None
    return (b + a) / 2


ASH_LIMIT = 50
ASH_FAIR = 10000
PEPPER_LIMIT = 80
PEPPER_FLOOR = 20
WINDOW = 50
TREND_THR = 0.02


def trade_ash(state):
    prod = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(prod)
    if od is None: return []
    pos = state.position.get(prod, 0)
    bb, ba = bb_ba(od); fair = ASH_FAIR
    orders = []
    if ba is not None:
        for ap in sorted(od.sell_orders):
            if ap <= fair - 1:
                q = min(abs(od.sell_orders[ap]), ASH_LIMIT - pos)
                if q > 0: orders.append(Order(prod, ap, q)); pos += q
            else: break
    if bb is not None:
        for bp_ in sorted(od.buy_orders, reverse=True):
            if bp_ >= fair + 1:
                q = min(od.buy_orders[bp_], ASH_LIMIT + pos)
                if q > 0: orders.append(Order(prod, bp_, -q)); pos -= q
            else: break
    if bb is not None and pos < ASH_LIMIT:
        bp = min(bb + 1, fair - 1)
        if ba is not None: bp = min(bp, ba - 1)
        orders.append(Order(prod, bp, min(30, ASH_LIMIT - pos)))
    if ba is not None and pos > -ASH_LIMIT:
        ap = max(ba - 1, fair + 1)
        if bb is not None: ap = max(ap, bb + 1)
        orders.append(Order(prod, ap, -min(30, ASH_LIMIT + pos)))
    return orders


def trade_pepper(state, ts):
    prod = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(prod)
    if od is None: return []
    pos = state.position.get(prod, 0)
    bb, ba = bb_ba(od); mid = mid_of(od)
    orders = []
    if mid is None: return orders

    hist = ts.setdefault("pep_hist", [])
    hist.append(mid)
    if len(hist) > WINDOW: del hist[0]

    slope = 0.0
    if len(hist) >= 10:
        slope = (hist[-1] - hist[0]) / len(hist)

    if slope > TREND_THR:
        # UPTREND — aggressive max long, deep ladder (uses +25% access)
        if ba is not None:
            for ap in sorted(od.sell_orders):
                q = min(abs(od.sell_orders[ap]), PEPPER_LIMIT - pos)
                if q > 0: orders.append(Order(prod, ap, q)); pos += q
                if pos >= PEPPER_LIMIT: break
        rem = PEPPER_LIMIT - pos
        if rem > 0 and bb is not None:
            for offset, sz in [(0, 35), (-1, 25), (-2, 20)]:
                if rem <= 0: break
                q = min(sz, rem)
                px = bb + offset
                if ba is not None and px >= ba: px = ba - 1
                orders.append(Order(prod, px, q)); rem -= q
    elif slope < -TREND_THR:
        # DOWNTREND — reduce to floor
        if pos > PEPPER_FLOOR and bb is not None:
            q = min(pos - PEPPER_FLOOR, 25)
            if q > 0: orders.append(Order(prod, bb, -q))
    else:
        # CHOP — MM around recent mean
        ema = sum(hist[-10:]) / min(10, len(hist))
        if pos < PEPPER_LIMIT and bb is not None and bb < ema - 1:
            orders.append(Order(prod, bb, min(20, PEPPER_LIMIT - pos)))
        if pos > PEPPER_FLOOR and ba is not None and ba > ema + 1:
            orders.append(Order(prod, ba, -min(20, pos - PEPPER_FLOOR)))
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
