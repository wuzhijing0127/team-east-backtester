"""
r2_ashfix_03 — ASH: asymmetric flatten-only quoting
====================================================
FIX: Only quote the side that reduces inventory. Never add to existing
position passively. If long → only quote ask (to sell); if short → only
quote bid (to buy). Near-flat → quote both sides thinly.

RATIONALE: Adverse selection comes from BOTH sides of symmetric quoting
catching picks from informed flow. One-sided flattening quotes only catch
favorable fills (selling your long, buying your short).

PEPPER: unchanged max-long.
MAF: 5000.
"""

import json
from datamodel import Order, OrderDepth, TradingState

# === MAF_HOOK ===
MAF = 5000
# ================

ASH_LIMIT = 40
ASH_FAIR = 10000
ASH_TAKE_EDGE = 2
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
    bb, ba = bb_ba(od); fair = ASH_FAIR
    orders = []

    # Take at deep edge only
    if ba is not None:
        for ap in sorted(od.sell_orders):
            if ap <= fair - ASH_TAKE_EDGE and pos < ASH_LIMIT:
                q = min(abs(od.sell_orders[ap]), ASH_LIMIT - pos)
                if q > 0: orders.append(Order(prod, ap, q)); pos += q
            else: break
    if bb is not None:
        for bp_ in sorted(od.buy_orders, reverse=True):
            if bp_ >= fair + ASH_TAKE_EDGE and pos > -ASH_LIMIT:
                q = min(od.buy_orders[bp_], ASH_LIMIT + pos)
                if q > 0: orders.append(Order(prod, bp_, -q)); pos -= q
            else: break

    # Asymmetric passive: only quote flattening direction
    near_flat_thr = 5
    if pos >= near_flat_thr and ba is not None:
        # Long — only quote ask (to sell)
        ask_px = min(ba, fair + 1)
        if bb is not None: ask_px = max(ask_px, bb + 1)
        orders.append(Order(prod, int(ask_px), -min(pos, 20)))
    elif pos <= -near_flat_thr and bb is not None:
        # Short — only quote bid (to buy)
        bid_px = max(bb, fair - 1)
        if ba is not None: bid_px = min(bid_px, ba - 1)
        orders.append(Order(prod, int(bid_px), min(-pos, 20)))
    else:
        # Near flat — thin two-sided quoting with wide spread (low risk)
        if bb is not None:
            bid_px = min(fair - 2, bb)
            if ba is not None: bid_px = min(bid_px, ba - 1)
            orders.append(Order(prod, int(bid_px), 8))
        if ba is not None:
            ask_px = max(fair + 2, ba)
            if bb is not None: ask_px = max(ask_px, bb + 1)
            orders.append(Order(prod, int(ask_px), -8))
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
