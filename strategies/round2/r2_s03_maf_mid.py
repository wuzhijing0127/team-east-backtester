"""
r2_s03 — Mid MAF (rational EV bid)
===================================
MAF = 5000. Bid-size roughly 25–40% of expected +25%-volume uplift.
Strategy assumes we win: aggressive taking on PEPPER asks, larger ASH
MM sizes to capitalize on 25% more fill opportunities.
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


ASH_LIMIT = 50
ASH_FAIR = 10000
PEPPER_LIMIT = 80


def trade_ash(state):
    prod = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(prod)
    if od is None: return []
    pos = state.position.get(prod, 0)
    bb, ba = bb_ba(od); fair = ASH_FAIR
    orders = []
    if ba is not None:
        for ap in sorted(od.sell_orders):
            if ap <= fair - 1 or (ap <= fair and pos < 0):
                q = min(abs(od.sell_orders[ap]), ASH_LIMIT - pos)
                if q > 0: orders.append(Order(prod, ap, q)); pos += q
            else: break
    if bb is not None:
        for bp_ in sorted(od.buy_orders, reverse=True):
            if bp_ >= fair + 1 or (bp_ >= fair and pos > 0):
                q = min(od.buy_orders[bp_], ASH_LIMIT + pos)
                if q > 0: orders.append(Order(prod, bp_, -q)); pos -= q
            else: break
    skew = -0.3 * pos; eff = fair + skew
    # Larger passive quote sizes to exploit extra 25% volume
    if bb is not None and pos < ASH_LIMIT:
        bid_px = bb if bb + 1 >= eff else bb + 1
        if ba is not None: bid_px = min(bid_px, ba - 1)
        bq = min(35, ASH_LIMIT - pos)
        if bq > 0: orders.append(Order(prod, int(bid_px), bq))
    if ba is not None and pos > -ASH_LIMIT:
        ask_px = ba if ba - 1 <= eff else ba - 1
        if bb is not None: ask_px = max(ask_px, bb + 1)
        sq = min(35, ASH_LIMIT + pos)
        if sq > 0: orders.append(Order(prod, int(ask_px), -sq))
    return orders


def trade_pepper(state):
    prod = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(prod)
    if od is None: return []
    pos = state.position.get(prod, 0)
    bb, ba = bb_ba(od)
    orders = []
    # Take EVERY level on asks (extra 25% access = more fills per tick)
    if ba is not None:
        for ap in sorted(od.sell_orders):
            q = min(abs(od.sell_orders[ap]), PEPPER_LIMIT - pos)
            if q > 0: orders.append(Order(prod, ap, q)); pos += q
            if pos >= PEPPER_LIMIT: break
    # 2-level passive ladder for remainder
    rem = PEPPER_LIMIT - pos
    if rem > 0 and bb is not None:
        q1 = min(40, rem); orders.append(Order(prod, bb, q1)); rem -= q1
        if rem > 0:
            px = bb - 1
            if ba is not None and px < ba:
                orders.append(Order(prod, px, rem))
    return orders


class Trader:
    def run(self, state):
        orders = {}
        for prod in state.order_depths:
            if prod == "ASH_COATED_OSMIUM":
                orders[prod] = trade_ash(state)
            elif prod == "INTARIAN_PEPPER_ROOT":
                orders[prod] = trade_pepper(state)
        return orders, 0, state.traderData or ""
