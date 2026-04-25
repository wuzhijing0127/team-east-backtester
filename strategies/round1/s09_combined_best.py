"""
s09 — Combined best: PEPPER max-long + ASH tight MM + dip-buy overlay
======================================================================
HYPOTHESIS: Stack the best structural ideas.
  • PEPPER: always max long (capture all drift).
  • ASH: tight MM joining best bid/ask (capture higher fill rate).
  • OVERLAY: when PEPPER dips hard below EMA, double-stack on passive bids
    so we accumulate cheaper than market price.

STRUCTURE:
- PEPPER: max long + dip-overlay ladder when below EMA.
- ASH:    tight MM at best bid/ask with inventory skew (ASH_target=0).
"""

import json
from datamodel import Order, OrderDepth, TradingState


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
    pos = state.position.get(prod, 0); limit = ASH_LIMIT
    bb, ba = bb_ba(od); fair = ASH_FAIR
    orders = []

    # Take aggressively at any profitable edge
    if ba is not None:
        for ap in sorted(od.sell_orders):
            if ap <= fair - 1 or (ap <= fair and pos < 0):
                q = min(abs(od.sell_orders[ap]), limit - pos)
                if q > 0: orders.append(Order(prod, ap, q)); pos += q
            else: break
    if bb is not None:
        for bp_ in sorted(od.buy_orders, reverse=True):
            if bp_ >= fair + 1 or (bp_ >= fair and pos > 0):
                q = min(od.buy_orders[bp_], limit + pos)
                if q > 0: orders.append(Order(prod, bp_, -q)); pos -= q
            else: break

    # Tight MM: join best bid/ask with inventory-aware skew
    skew = -0.3 * pos        # if long, quote below fair
    eff_fair = fair + skew
    if bb is not None and pos < limit:
        bid_px = bb if bb + 1 >= eff_fair else bb + 1
        if ba is not None: bid_px = min(bid_px, ba - 1)
        bq = min(25, limit - pos)
        if bq > 0: orders.append(Order(prod, int(bid_px), bq))
    if ba is not None and pos > -limit:
        ask_px = ba if ba - 1 <= eff_fair else ba - 1
        if bb is not None: ask_px = max(ask_px, bb + 1)
        sq = min(25, limit + pos)
        if sq > 0: orders.append(Order(prod, int(ask_px), -sq))
    return orders


def trade_pepper(state, ts):
    prod = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(prod)
    if od is None: return []
    pos = state.position.get(prod, 0); limit = PEPPER_LIMIT
    bb, ba = bb_ba(od); mid = mid_of(od)
    orders = []
    if mid is None: return orders

    ema = ts.get("pep_ema", mid)
    ema = EMA_ALPHA * mid + (1 - EMA_ALPHA) * ema
    ts["pep_ema"] = ema

    # Take all sell liquidity up to full long
    if ba is not None:
        for ap in sorted(od.sell_orders):
            q = min(abs(od.sell_orders[ap]), limit - pos)
            if q > 0: orders.append(Order(prod, ap, q)); pos += q
            if pos >= limit: break

    rem = limit - pos
    if rem <= 0 or bb is None: return orders

    # Dip overlay: mid below EMA → stack ladder for bigger fill
    if mid < ema - DIP_K:
        for offset in (0, -1, -2):
            if rem <= 0: break
            q = min(30, rem)
            px = bb + offset
            if ba is not None and px >= ba: px = ba - 1
            orders.append(Order(prod, px, q))
            rem -= q
    else:
        # Normal passive bid join queue
        orders.append(Order(prod, bb, rem))
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
