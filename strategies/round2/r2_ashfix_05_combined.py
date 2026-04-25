"""
r2_ashfix_05 — ASH: combined best (dynamic fair + flatten-only + small limit)
==============================================================================
FIX: Stack the three safest ideas.
  • Dynamic fair (EMA) — don't mis-price
  • Asymmetric flatten-only quoting — no adverse selection on both sides
  • Limit 25 — cap blast radius

Expected: ASH goes from -62k → break-even or small positive. Safest bet.

PEPPER: unchanged max-long.
MAF: 5000.
"""

import json
from datamodel import Order, OrderDepth, TradingState

# === MAF_HOOK ===
MAF = 5000
# ================

ASH_LIMIT = 25
ASH_INIT_FAIR = 10000
ASH_EMA_ALPHA = 0.01
ASH_TAKE_EDGE = 2
PEPPER_LIMIT = 80


def bb_ba(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def mid_of(od):
    b, a = bb_ba(od)
    if b is None or a is None: return None
    return (b + a) / 2


def trade_ash(state, ts):
    prod = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(prod)
    if od is None: return []
    pos = state.position.get(prod, 0)
    bb, ba = bb_ba(od)
    mid = mid_of(od)

    fair = ts.get("ash_fair", ASH_INIT_FAIR)
    if mid is not None:
        fair = ASH_EMA_ALPHA * mid + (1 - ASH_EMA_ALPHA) * fair
        ts["ash_fair"] = fair
    fr = round(fair)
    orders = []

    # Scalp on deep edges
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

    # Asymmetric passive: flatten direction only
    near_flat = 4
    if pos >= near_flat and ba is not None:
        ask_px = max(fr + 1, ba)
        if bb is not None: ask_px = max(ask_px, bb + 1)
        orders.append(Order(prod, int(ask_px), -min(pos, 12)))
    elif pos <= -near_flat and bb is not None:
        bid_px = min(fr - 1, bb)
        if ba is not None: bid_px = min(bid_px, ba - 1)
        orders.append(Order(prod, int(bid_px), min(-pos, 12)))
    else:
        # Near-flat: thin wide quotes
        if bb is not None:
            bid_px = min(fr - 2, bb)
            if ba is not None: bid_px = min(bid_px, ba - 1)
            orders.append(Order(prod, int(bid_px), 6))
        if ba is not None:
            ask_px = max(fr + 2, ba)
            if bb is not None: ask_px = max(ask_px, bb + 1)
            orders.append(Order(prod, int(ask_px), -6))
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
        ts = json.loads(state.traderData) if state.traderData else {}
        for prod in state.order_depths:
            if prod == "ASH_COATED_OSMIUM":
                orders[prod] = trade_ash(state, ts)
            elif prod == "INTARIAN_PEPPER_ROOT":
                orders[prod] = trade_pepper(state)
        return orders, 0, json.dumps(ts)
