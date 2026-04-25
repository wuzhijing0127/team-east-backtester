"""
r2_s04 — High MAF (secure top-50%)
===================================
MAF = 10000. Higher than expected cutoff; buys near-certainty of winning
the +25% volume auction. Strategy leans into that: deeper ladders, larger
passive sizes, aggressive take on all PEPPER asks.

Breakeven note: on a ~80k-100k baseline PnL with 25% uplift (~20k-25k),
10k MAF leaves ~10k-15k of surplus if we win.
"""

import json
from datamodel import Order, OrderDepth, TradingState

# === MAF_HOOK ===
MAF = 10000
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
    skew = -0.4 * pos; eff = fair + skew
    # Max passive sizes
    if bb is not None and pos < ASH_LIMIT:
        bid_px = bb if bb + 1 >= eff else bb + 1
        if ba is not None: bid_px = min(bid_px, ba - 1)
        bq = min(40, ASH_LIMIT - pos)
        if bq > 0: orders.append(Order(prod, int(bid_px), bq))
    if ba is not None and pos > -ASH_LIMIT:
        ask_px = ba if ba - 1 <= eff else ba - 1
        if bb is not None: ask_px = max(ask_px, bb + 1)
        sq = min(40, ASH_LIMIT + pos)
        if sq > 0: orders.append(Order(prod, int(ask_px), -sq))
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
    # Deep 3-level ladder to catch any dip with +25% access
    rem = PEPPER_LIMIT - pos
    if rem > 0 and bb is not None:
        for offset, sz in [(0, 40), (-1, 30), (-2, 20)]:
            if rem <= 0: break
            q = min(sz, rem)
            px = bb + offset
            if ba is not None and px >= ba: px = ba - 1
            if q > 0:
                orders.append(Order(prod, px, q)); rem -= q
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
