"""
s05 — Pairs trade: long PEPPER, short-biased ASH
=================================================
HYPOTHESIS: If PEPPER and ASH are even weakly positively correlated,
running long PEPPER + short ASH hedges out market-wide risk and can
capture relative drift (PEPPER drifts up, ASH stays flat → spread widens).
Even if uncorrelated, short-biased ASH still captures its normal MM edge.

STRUCTURE:
- PEPPER: max long 80 (same as s01).
- ASH:    skewed MM with NEGATIVE bias — target short ~20 position,
          use ASH short notional to offset PEPPER long exposure.
"""

import json
from datamodel import Order, OrderDepth, TradingState


def bb_ba(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


ASH_LIMIT = 50
ASH_FAIR = 10000
ASH_TARGET = -20         # prefer short bias
PEPPER_LIMIT = 80


def trade_ash(state):
    prod = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(prod)
    if od is None: return []
    pos = state.position.get(prod, 0)
    limit = ASH_LIMIT
    bb, ba = bb_ba(od)
    fair = ASH_FAIR
    orders = []

    # Skewed reservation: pull toward ASH_TARGET (negative)
    inv_err = pos - ASH_TARGET     # >0 means too long, need to sell
    skew = 0.5 * inv_err           # shifts reservation down if too long
    res = fair - skew
    rr = round(res)

    # Take on ask if cheap vs skewed fair
    if ba is not None:
        for ap in sorted(od.sell_orders):
            if ap <= rr - 1 and pos < limit:
                q = min(abs(od.sell_orders[ap]), limit - pos)
                if q > 0: orders.append(Order(prod, ap, q)); pos += q
            else: break
    # Aggressive sell on bid when above fair (biased short)
    if bb is not None:
        for bp_ in sorted(od.buy_orders, reverse=True):
            if bp_ >= rr and pos > -limit:
                q = min(od.buy_orders[bp_], limit + pos)
                if q > 0: orders.append(Order(prod, bp_, -q)); pos -= q
            else: break

    # Passive quotes: wider ask (eager to sell), tighter bid
    bid_px = rr - 2
    ask_px = rr + 1
    if bb is not None: bid_px = min(bb + 1, bid_px, (ba - 1) if ba else bid_px)
    if ba is not None: ask_px = max(ba - 1, ask_px, (bb + 1) if bb else ask_px)

    bq = min(10, limit - pos)       # small buy
    sq = min(25, limit + pos)       # larger sell
    if bq > 0: orders.append(Order(prod, bid_px, bq))
    if sq > 0: orders.append(Order(prod, ask_px, -sq))
    return orders


def trade_pepper(state):
    prod = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(prod)
    if od is None: return []
    pos = state.position.get(prod, 0)
    limit = PEPPER_LIMIT
    bb, ba = bb_ba(od)
    orders = []
    if ba is not None:
        for ap in sorted(od.sell_orders):
            q = min(abs(od.sell_orders[ap]), limit - pos)
            if q > 0:
                orders.append(Order(prod, ap, q)); pos += q
            if pos >= limit: break
    rem = limit - pos
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
