"""
r2_s01 — No MAF baseline (control)
===================================
MAF = 0. Uses best round-1 structure (PEPPER max-long + tight ASH MM).
Control variant: measures what we get WITHOUT paying for extra volume access.

If PnL here is close to paid-MAF variants, the 25% extra isn't worth the fee.
"""

import json
from datamodel import Order, OrderDepth, TradingState

# === MAF_HOOK: market access fee for auction (top-50% pays & gets +25% vol) ===
MAF = 0
# =============================================================================


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
    bb, ba = bb_ba(od)
    fair = ASH_FAIR
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

    skew = -0.3 * pos
    eff = fair + skew
    if bb is not None and pos < ASH_LIMIT:
        bid_px = bb if bb + 1 >= eff else bb + 1
        if ba is not None: bid_px = min(bid_px, ba - 1)
        bq = min(25, ASH_LIMIT - pos)
        if bq > 0: orders.append(Order(prod, int(bid_px), bq))
    if ba is not None and pos > -ASH_LIMIT:
        ask_px = ba if ba - 1 <= eff else ba - 1
        if bb is not None: ask_px = max(ask_px, bb + 1)
        sq = min(25, ASH_LIMIT + pos)
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
        # MAF_HOOK: if platform API requires MAF in return signature, append here
        return orders, 0, state.traderData or ""
