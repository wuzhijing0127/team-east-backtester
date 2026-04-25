"""
r2_s08 — Full attack MAF
=========================
MAF = 15000. Extremely high bid — near-certain to win top-50%. Strategy
is built on the assumption we DO win: max-size everything, deepest ladders,
dip overlay, tight ASH MM with maximum quote sizes.

Breakeven: at ~80k-100k baseline, 25% uplift = 20k-25k; 15k MAF leaves
only 5k-10k surplus. Only worth it if other teams bid aggressively too
and we need to secure the contract.

Use this as upper-bound stress test — don't default to it.
"""

import json
from datamodel import Order, OrderDepth, TradingState

# === MAF_HOOK ===
MAF = 15000
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
    # Take at any profitable edge
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
    skew = -0.5 * pos; eff = fair + skew
    # MAXIMUM passive sizes — saturate position limit
    if bb is not None and pos < ASH_LIMIT:
        bid_px = bb if bb + 1 >= eff else bb + 1
        if ba is not None: bid_px = min(bid_px, ba - 1)
        bq = min(50, ASH_LIMIT - pos)
        if bq > 0: orders.append(Order(prod, int(bid_px), bq))
    if ba is not None and pos > -ASH_LIMIT:
        ask_px = ba if ba - 1 <= eff else ba - 1
        if bb is not None: ask_px = max(ask_px, bb + 1)
        sq = min(50, ASH_LIMIT + pos)
        if sq > 0: orders.append(Order(prod, int(ask_px), -sq))
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

    # Always aggressive ladder regardless of dip/non-dip
    ladder = [(0, 50), (-1, 40), (-2, 30), (-3, 25)] if mid < ema - DIP_K \
             else [(0, 50), (-1, 25), (-2, 15)]
    for offset, sz in ladder:
        if rem <= 0: break
        q = min(sz, rem)
        px = bb + offset
        if ba is not None and px >= ba: px = ba - 1
        orders.append(Order(prod, px, q)); rem -= q
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
