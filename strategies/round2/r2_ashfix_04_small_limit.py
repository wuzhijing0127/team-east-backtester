"""
r2_ashfix_04 — ASH: small limit (20) + normal MM
=================================================
FIX: Keep the current MM logic but cap position at ±20 (vs 80 in 271819).
Same bleed RATE per unit, but 4× smaller exposure → much smaller loss.

TRADE-OFF: Lowest code delta. If 271819 bled -62k at limit=80, scaling
by 20/80 = ~-15k expected. Not great but recovers ~47k relative to baseline.
Useful as a quick-win comparison and sanity check.

PEPPER: unchanged max-long.
MAF: 5000.
"""

import json
from datamodel import Order, OrderDepth, TradingState

# === MAF_HOOK ===
MAF = 5000
# ================

ASH_LIMIT = 20           # was 80 in 271819
ASH_FAIR = 10000
ASH_HALF_SPREAD = 1
ASH_K_INV = 2.5
ASH_BASE_SIZE = 8        # scaled down with limit
ASH_TAKE_EDGE = 1
PEPPER_LIMIT = 80


def bb_ba(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def trade_ash(state):
    prod = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(prod)
    if od is None: return []
    pos = state.position.get(prod, 0)
    bb, ba = bb_ba(od); fair = ASH_FAIR; fr = round(fair)
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

    res = fair - ASH_K_INV * (pos / ASH_LIMIT); rr = round(res)
    bid_px = rr - ASH_HALF_SPREAD; ask_px = rr + ASH_HALF_SPREAD
    if bb is not None: bid_px = min(bb + 1, bid_px)
    if ba is not None: ask_px = max(ba - 1, ask_px)
    if ba is not None: bid_px = min(bid_px, ba - 1)
    if bb is not None: ask_px = max(ask_px, bb + 1)

    if pos < ASH_LIMIT:
        orders.append(Order(prod, bid_px, min(ASH_BASE_SIZE, ASH_LIMIT - pos)))
    if pos > -ASH_LIMIT:
        orders.append(Order(prod, ask_px, -min(ASH_BASE_SIZE, ASH_LIMIT + pos)))
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
        return orders, 0, state.traderData or ""
