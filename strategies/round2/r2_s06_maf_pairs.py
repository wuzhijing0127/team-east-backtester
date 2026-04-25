"""
r2_s06 — Small MAF + pairs trade
=================================
MAF = 3000. Long PEPPER (max 80), short-biased ASH (target -20).
The +25% volume uplift sharpens both legs — faster PEPPER fills,
more ASH short-side fills on buy-side lifts.

Insurance play: if market regime reverses (PEPPER trend breaks),
short ASH offsets some of the PEPPER drawdown.
"""

import json
from datamodel import Order, OrderDepth, TradingState

# === MAF_HOOK ===
MAF = 3000
# ================

def bb_ba(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


ASH_LIMIT = 50
ASH_FAIR = 10000
ASH_TARGET = -20
PEPPER_LIMIT = 80


def trade_ash(state):
    prod = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(prod)
    if od is None: return []
    pos = state.position.get(prod, 0)
    bb, ba = bb_ba(od); fair = ASH_FAIR
    orders = []
    # Skewed reservation toward short target
    inv_err = pos - ASH_TARGET
    res = fair - 0.5 * inv_err
    rr = round(res)

    if ba is not None:
        for ap in sorted(od.sell_orders):
            if ap <= rr - 1 and pos < ASH_LIMIT:
                q = min(abs(od.sell_orders[ap]), ASH_LIMIT - pos)
                if q > 0: orders.append(Order(prod, ap, q)); pos += q
            else: break
    if bb is not None:
        for bp_ in sorted(od.buy_orders, reverse=True):
            if bp_ >= rr and pos > -ASH_LIMIT:
                q = min(od.buy_orders[bp_], ASH_LIMIT + pos)
                if q > 0: orders.append(Order(prod, bp_, -q)); pos -= q
            else: break

    bid_px = rr - 2; ask_px = rr + 1
    if bb is not None: bid_px = min(bb + 1, bid_px)
    if ba is not None:
        ask_px = max(ba - 1, ask_px)
        bid_px = min(bid_px, ba - 1)
    if bb is not None: ask_px = max(ask_px, bb + 1)

    bq = min(10, ASH_LIMIT - pos)
    sq = min(30, ASH_LIMIT + pos)
    if bq > 0: orders.append(Order(prod, bid_px, bq))
    if sq > 0: orders.append(Order(prod, ask_px, -sq))
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
