"""
r2_s02 — Low MAF (cheap shot at top-50%)
=========================================
MAF = 2000. Cheap bet: if 50%-cutoff is low (many teams bid 0 or 500),
we win cheaply and get +25% volume. Strategy otherwise matches s01.

Sizes tuned slightly larger on PEPPER passive bids to use extra depth
if we win — no cost if we lose the auction.
"""

import json
from datamodel import Order, OrderDepth, TradingState

# === MAF_HOOK ===
MAF = 2000
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
    if bb is not None and pos < ASH_LIMIT:
        bid_px = bb if bb + 1 >= eff else bb + 1
        if ba is not None: bid_px = min(bid_px, ba - 1)
        bq = min(30, ASH_LIMIT - pos)
        if bq > 0: orders.append(Order(prod, int(bid_px), bq))
    if ba is not None and pos > -ASH_LIMIT:
        ask_px = ba if ba - 1 <= eff else ba - 1
        if bb is not None: ask_px = max(ask_px, bb + 1)
        sq = min(30, ASH_LIMIT + pos)
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
    rem = PEPPER_LIMIT - pos
    if rem > 0 and bb is not None:
        # Size up passive bid to take advantage of extra volume if we win MAF
        orders.append(Order(prod, bb, rem))
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
