"""
r2_s05 — Mid MAF + dip-overlay (EMA-based)
===========================================
MAF = 5000. Uses extra 25% volume specifically on PEPPER dips (mid < EMA - k)
when liquidity is cheap and plentiful. In normal conditions, standard max-long.

HYPOTHESIS: the 25% volume uplift is most valuable when market gives you
cheap inventory — the dip moments. Over-sized ladders at those moments
convert volume advantage directly into discounted entries.
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
EMA_ALPHA = 0.02
DIP_K = 2


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
        orders.append(Order(prod, bp, min(25, ASH_LIMIT - pos)))
    if ba is not None and pos > -ASH_LIMIT:
        ap = max(ba - 1, fair + 1)
        if bb is not None: ap = max(ap, bb + 1)
        orders.append(Order(prod, ap, -min(25, ASH_LIMIT + pos)))
    return orders


def trade_pepper(state, ts):
    prod = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(prod)
    if od is None: return []
    pos = state.position.get(prod, 0)
    bb, ba = bb_ba(od); mid = mid_of(od)
    orders = []
    if mid is None: return orders

    ema = ts.get("pep_ema", mid)
    ema = EMA_ALPHA * mid + (1 - EMA_ALPHA) * ema
    ts["pep_ema"] = ema

    if ba is not None:
        for ap in sorted(od.sell_orders):
            q = min(abs(od.sell_orders[ap]), PEPPER_LIMIT - pos)
            if q > 0: orders.append(Order(prod, ap, q)); pos += q
            if pos >= PEPPER_LIMIT: break

    rem = PEPPER_LIMIT - pos
    if rem <= 0 or bb is None: return orders

    if mid < ema - DIP_K:
        # DIP overlay — stack deep, size aggressively (use +25% access here)
        for offset, sz in [(0, 40), (-1, 30), (-2, 25), (-3, 20)]:
            if rem <= 0: break
            q = min(sz, rem)
            px = bb + offset
            if ba is not None and px >= ba: px = ba - 1
            orders.append(Order(prod, px, q)); rem -= q
    else:
        orders.append(Order(prod, bb, rem))
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
