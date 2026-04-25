"""
r2_ashfix_01 — ASH: scalp-only (NO MM)
=======================================
FIX: Stop providing liquidity. Only take when ask <= fair-3 or bid >= fair+3
(deep dislocations). No passive quotes = no adverse selection.

TRADE-OFF: We give up the occasional free spread capture, but we stop
the -62k bleed. Expected: ASH goes from -62k to near 0 (a few small wins).

PEPPER: unchanged max-long (the working engine).
MAF: 5000 (rational mid bid).
"""

import json
from datamodel import Order, OrderDepth, TradingState

# === MAF_HOOK ===
MAF = 5000
# ================

ASH_LIMIT = 30           # also reduced from 80 → 30 to cap worst-case risk
ASH_FAIR = 10000
ASH_MIN_EDGE = 3         # only trade when |px - fair| >= 3
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
    fair = ASH_FAIR
    orders = []

    # Take only at deep dislocation (fair - 3 or better)
    for ap in sorted(od.sell_orders):
        if ap <= fair - ASH_MIN_EDGE and pos < ASH_LIMIT:
            q = min(abs(od.sell_orders[ap]), ASH_LIMIT - pos)
            if q > 0:
                orders.append(Order(prod, ap, q)); pos += q
        else: break

    for bp_ in sorted(od.buy_orders, reverse=True):
        if bp_ >= fair + ASH_MIN_EDGE and pos > -ASH_LIMIT:
            q = min(od.buy_orders[bp_], ASH_LIMIT + pos)
            if q > 0:
                orders.append(Order(prod, bp_, -q)); pos -= q
        else: break

    # No passive quotes — scalp only
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
