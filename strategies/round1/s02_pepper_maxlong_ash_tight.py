"""
s02 — PEPPER max-long + ASH TIGHT MM (join best)
=================================================
HYPOTHESIS: Same PEPPER as s01, but ASH runs a tight MM that joins
the best bid/ask instead of quoting inside. Also narrower take edge.
Compared to s01, this tests whether ASH can generate another few k of
PnL via higher fill rate at best-price quotes.

STRUCTURE:
- PEPPER: max-long (same as s01).
- ASH:    tight MM — always quote at best_bid / best_ask (join queue);
          take at any profitable edge; no half-spread padding.
"""

import json
from datamodel import Order, OrderDepth, TradingState


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
    limit = ASH_LIMIT
    bb, ba = bb_ba(od)
    fair = ASH_FAIR
    orders = []

    # Take anything trading through fair (including at-fair when loaded)
    if ba is not None:
        for ap in sorted(od.sell_orders):
            edge = fair - ap
            # buy when ask <= fair-1 always, or at fair when short
            if edge >= 1 or (edge >= 0 and pos < 0):
                q = min(abs(od.sell_orders[ap]), limit - pos)
                if q > 0:
                    orders.append(Order(prod, ap, q)); pos += q
            else: break
    if bb is not None:
        for bp in sorted(od.buy_orders, reverse=True):
            edge = bp - fair
            if edge >= 1 or (edge >= 0 and pos > 0):
                q = min(od.buy_orders[bp], limit + pos)
                if q > 0:
                    orders.append(Order(prod, bp, -q)); pos -= q
            else: break

    # Tight MM — join best bid/ask with size scaled by remaining capacity
    if bb is not None and pos < limit:
        bid_px = bb if bb < fair else fair - 1
        bq = min(25, limit - pos)
        if bq > 0:
            orders.append(Order(prod, bid_px, bq))
    if ba is not None and pos > -limit:
        ask_px = ba if ba > fair else fair + 1
        sq = min(25, limit + pos)
        if sq > 0:
            orders.append(Order(prod, ask_px, -sq))
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
