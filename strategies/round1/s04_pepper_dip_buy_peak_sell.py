"""
s04 — PEPPER surf the wave (dip-buy / peak-sell on drift)
==========================================================
HYPOTHESIS: On top of the +1000/day drift there are intra-day oscillations.
Track EMA; when mid < EMA - k, buy (dip); when mid > EMA + k, sell part
of long to lock profit. Maintains net long bias (floor 50% long).

STRUCTURE:
- PEPPER: EMA-based mean reversion AROUND a rising drift;
          net long floor = 50% of limit, ceiling = 100%.
- ASH:    baseline 216002 logic.
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


ASH_P = {
    "limit": 50, "fair": 10000, "half_spread": 1, "k_inv": 2.5,
    "base_size": 20, "take_edge_neutral": 1, "take_edge_loaded": 0,
    "take_neutral_thr": 0.3, "tier_med": 0.4, "tier_high": 0.7, "tier_ext": 0.9,
}
PEPPER_LIMIT = 80
PEPPER_FLOOR = 40     # min long position
DIP_EDGE = 3          # mid < ema - 3 → buy aggressively
PEAK_EDGE = 4         # mid > ema + 4 → sell partial
EMA_ALPHA = 0.02


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
    pos = state.position.get(prod, 0); limit = p["limit"]
    bb, ba = bb_ba(od); fair = p["fair"]; fr = round(fair)
    orders = []
    inv_frac = abs(pos) / limit
    buy_te = sell_te = p["take_edge_neutral"] if inv_frac < p["take_neutral_thr"] else p["take_edge_loaded"]
    if pos < 0: buy_te = max(buy_te, p["take_edge_neutral"])
    if pos > 0: sell_te = max(sell_te, p["take_edge_neutral"])
    if ba is not None:
        for ap in sorted(od.sell_orders):
            if ap <= fr - buy_te:
                q = min(abs(od.sell_orders[ap]), limit - pos)
                if q > 0: orders.append(Order(prod, ap, q)); pos += q
            else: break
    if bb is not None:
        for bp_ in sorted(od.buy_orders, reverse=True):
            if bp_ >= fr + sell_te:
                q = min(od.buy_orders[bp_], limit + pos)
                if q > 0: orders.append(Order(prod, bp_, -q)); pos -= q
            else: break
    res = fair - p["k_inv"] * (pos / limit); rr = round(res)
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

    dev = mid - ema

    # Dip-buy: mid well below EMA → aggressive buy
    if dev < -DIP_EDGE and pos < limit and ba is not None:
        for ap in sorted(od.sell_orders):
            q = min(abs(od.sell_orders[ap]), limit - pos)
            if q > 0:
                orders.append(Order(prod, ap, q)); pos += q
            if pos >= limit: break

    # Peak-sell: mid well above EMA AND above floor → partial take-profit
    elif dev > PEAK_EDGE and pos > PEPPER_FLOOR and bb is not None:
        sellable = pos - PEPPER_FLOOR
        q = min(sellable, od.buy_orders.get(bb, 0), 20)
        if q > 0:
            orders.append(Order(prod, bb, -q))

    # Otherwise: passive bid to stay filling in the drift
    else:
        if pos < limit and bb is not None:
            orders.append(Order(prod, bb, min(20, limit - pos)))
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
