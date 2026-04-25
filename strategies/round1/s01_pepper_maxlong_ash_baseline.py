"""
s01 — PEPPER max-long + ASH baseline MM
========================================
HYPOTHESIS: The 10x gap is almost entirely PEPPER position utilization.
ASH baseline from 216002 already captures its stationary-MM edge; we just
fill PEPPER to the full 80-long limit at every tick and never sell.

STRUCTURE:
- PEPPER: take ALL sell liquidity up to 80 long, then post passive bids
  for remainder at best_bid (join queue, not best_bid+1). Never sells.
- ASH:    unchanged from 216002 (anchor 10000, position-aware MM).

EXPECTED: If PEPPER drifts +1000/day and we hold 80 long ≈ 80k ticks/day
of directional edge vs baseline which cycles ~25 long ≈ 25k/day.
"""

import json
from datamodel import Order, OrderDepth, TradingState


def bb_ba(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def microprice(od):
    b, a = bb_ba(od)
    if b is None or a is None:
        return None
    bv, av = od.buy_orders[b], abs(od.sell_orders[a])
    t = bv + av
    return (b + a) / 2 if t == 0 else (b * av + a * bv) / t


ASH_P = {
    "limit": 50, "fair": 10000, "half_spread": 1, "k_inv": 2.5,
    "base_size": 20, "flatten_size": 10, "take_edge_neutral": 1,
    "take_edge_loaded": 0, "take_neutral_thr": 0.3,
    "tier_med": 0.4, "tier_high": 0.7, "tier_ext": 0.9,
}
PEPPER_LIMIT = 80


def size_mult(pos, limit, p):
    frac = abs(pos) / limit if limit else 0
    if frac >= p["tier_ext"]: add = 0.0
    elif frac >= p["tier_high"]: add = 0.25
    elif frac >= p["tier_med"]: add = 0.5
    else: add = 1.0
    if pos > 0: return add, 1.0
    if pos < 0: return 1.0, add
    return 1.0, 1.0


def trade_ash(state):
    prod = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(prod)
    if od is None: return []
    p = ASH_P
    pos = state.position.get(prod, 0)
    limit = p["limit"]
    bb, ba = bb_ba(od)
    fair = p["fair"]
    fr = round(fair)
    orders = []

    inv_frac = abs(pos) / limit
    if inv_frac < p["take_neutral_thr"]:
        buy_te = sell_te = p["take_edge_neutral"]
    else:
        buy_te = sell_te = p["take_edge_loaded"]
    if pos < 0: buy_te = max(buy_te, p["take_edge_neutral"])
    if pos > 0: sell_te = max(sell_te, p["take_edge_neutral"])

    if ba is not None:
        for ap in sorted(od.sell_orders):
            if ap <= fr - buy_te:
                q = min(abs(od.sell_orders[ap]), limit - pos)
                if q > 0:
                    orders.append(Order(prod, ap, q)); pos += q
            else: break
    if bb is not None:
        for bp in sorted(od.buy_orders, reverse=True):
            if bp >= fr + sell_te:
                q = min(od.buy_orders[bp], limit + pos)
                if q > 0:
                    orders.append(Order(prod, bp, -q)); pos -= q
            else: break

    res = fair - p["k_inv"] * (pos / limit)
    rr = round(res)
    bp = rr - p["half_spread"]; ap = rr + p["half_spread"]
    if bb is not None: bp = min(bb + 1, bp)
    if ba is not None: ap = max(ba - 1, ap)
    if ba is not None: bp = min(bp, ba - 1)
    if bb is not None: ap = max(ap, bb + 1)

    bm, sm = size_mult(pos, limit, p)
    bq = min(round(p["base_size"] * bm), limit - pos)
    sq = min(round(p["base_size"] * sm), limit + pos)
    if bq > 0: orders.append(Order(prod, bp, bq))
    if sq > 0: orders.append(Order(prod, ap, -sq))

    if abs(pos) >= p["tier_ext"] * limit:
        if pos > 0 and bb is not None:
            orders.append(Order(prod, bb, -min(p["flatten_size"], pos)))
        elif pos < 0 and ba is not None:
            orders.append(Order(prod, ba, min(p["flatten_size"], -pos)))
    return orders


def trade_pepper(state):
    prod = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(prod)
    if od is None: return []
    pos = state.position.get(prod, 0)
    limit = PEPPER_LIMIT
    bb, ba = bb_ba(od)
    orders = []

    # Take all sell liquidity up to full long
    if ba is not None:
        for ap in sorted(od.sell_orders):
            vol = abs(od.sell_orders[ap])
            q = min(vol, limit - pos)
            if q > 0:
                orders.append(Order(prod, ap, q)); pos += q
            if pos >= limit: break

    # Post passive bid at best_bid (join queue) for remainder
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
