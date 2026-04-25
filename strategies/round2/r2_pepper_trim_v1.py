"""
r2_pepper_trim_v1 — v12 + take-profit overlay on PEPPER
========================================================

v12 PEPPER holds 80 long passively — captures drift but skips all
intraday waves. Round 1 (271819) had 9 pullbacks of ≥20 price units
that buy-and-hold left on the table.

This variant keeps v12 as the base (proven 8,868 PnL) and adds a
take-profit overlay: trim 20 units on clear local peaks, suppress
rebuy until price pulls back below the trim level.

ASH: v12 engine (unchanged)
PEPPER: v12 + trim logic
"""

import json
from datamodel import Order, OrderDepth, TradingState

MAF = 0

# --- ASH (v12, unchanged) ---
ASH_LIMIT = 20
ASH_INIT_FAIR = 10000
ASH_EMA_ALPHA = 0.05
ASH_HALF_SPREAD = 1
ASH_K_INV = 2.5
ASH_BASE_SIZE = 8
ASH_TAKE_EDGE = 1
ASH_MICRO_BETA = 0.03

# --- PEPPER core (v12) ---
PEPPER_LIMIT = 80
PEPPER_EMA_ALPHA = 0.02
PEPPER_DIP_K = 2

# --- PEPPER trim overlay ---
PEP_SLOW_ALPHA = 2 / 201        # slow EMA for peak detection (half-life ~140 ticks)
PEP_PEAK_THRESH = 3              # price units above slow_ema to be "elevated"
PEP_CONFIRM_TICKS = 5            # sustained elevation before trimming
PEP_TRIM_SIZE = 20               # units to sell at peak
PEP_REBUY_GAP = 2                # best_ask must be ≤ trim_px - gap to resume buying
PEP_TRIM_MAX_AGE = 10000         # ticks to capitulate trim if no rebuy signal


def bb_ba(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def mid_of(od):
    b, a = bb_ba(od)
    if b is None or a is None:
        return None
    return (b + a) / 2


def microprice(od):
    b, a = bb_ba(od)
    if b is None or a is None:
        return None
    bv = od.buy_orders[b]
    av = abs(od.sell_orders[a])
    t = bv + av
    if t == 0:
        return (b + a) / 2
    return (b * av + a * bv) / t


# ===================== ASH (v12, unchanged) =====================
def trade_ash(state, ts):
    prod = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(prod)
    if od is None:
        return []
    pos = state.position.get(prod, 0)
    bb, ba = bb_ba(od)
    mid = mid_of(od)
    micro = microprice(od)

    ema_fair = ts.get("ash_fair", ASH_INIT_FAIR)
    if mid is not None:
        ema_fair = ASH_EMA_ALPHA * mid + (1 - ASH_EMA_ALPHA) * ema_fair
        ts["ash_fair"] = ema_fair

    if mid is not None and micro is not None:
        fair = ema_fair + ASH_MICRO_BETA * (micro - mid)
    else:
        fair = ema_fair
    fr = round(fair)
    orders = []

    if ba is not None:
        for ap in sorted(od.sell_orders):
            if ap <= fr - ASH_TAKE_EDGE and pos < ASH_LIMIT:
                q = min(abs(od.sell_orders[ap]), ASH_LIMIT - pos)
                if q > 0:
                    orders.append(Order(prod, ap, q))
                    pos += q
            else:
                break
    if bb is not None:
        for bp_ in sorted(od.buy_orders, reverse=True):
            if bp_ >= fr + ASH_TAKE_EDGE and pos > -ASH_LIMIT:
                q = min(od.buy_orders[bp_], ASH_LIMIT + pos)
                if q > 0:
                    orders.append(Order(prod, bp_, -q))
                    pos -= q
            else:
                break

    res = fair - ASH_K_INV * (pos / ASH_LIMIT)
    rr = round(res)
    bid_px = rr - ASH_HALF_SPREAD
    ask_px = rr + ASH_HALF_SPREAD
    if bb is not None:
        bid_px = min(bb + 1, bid_px)
    if ba is not None:
        ask_px = max(ba - 1, ask_px)
    if ba is not None:
        bid_px = min(bid_px, ba - 1)
    if bb is not None:
        ask_px = max(ask_px, bb + 1)

    if pos < ASH_LIMIT:
        orders.append(Order(prod, bid_px, min(ASH_BASE_SIZE, ASH_LIMIT - pos)))
    if pos > -ASH_LIMIT:
        orders.append(Order(prod, ask_px, -min(ASH_BASE_SIZE, ASH_LIMIT + pos)))
    return orders


# ===================== PEPPER with trim overlay =====================
def trade_pepper(state, ts):
    prod = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(prod)
    if od is None:
        return []
    pos = state.position.get(prod, 0)
    bb, ba = bb_ba(od)
    mid = mid_of(od)
    orders = []
    if mid is None:
        return orders

    # existing v12 fast EMA (for dip overlay)
    ema = ts.get("pep_ema", mid)
    ema = PEPPER_EMA_ALPHA * mid + (1 - PEPPER_EMA_ALPHA) * ema
    ts["pep_ema"] = ema

    # slow EMA for peak detection
    slow_ema = ts.get("pep_slow_ema", mid)
    slow_ema = PEP_SLOW_ALPHA * mid + (1 - PEP_SLOW_ALPHA) * slow_ema
    ts["pep_slow_ema"] = slow_ema

    # elevation streak
    streak = ts.get("pep_elev_streak", 0)
    streak = streak + 1 if mid > slow_ema + PEP_PEAK_THRESH else 0
    ts["pep_elev_streak"] = streak

    trim_px = ts.get("pep_trim_px", 0)
    trim_age = ts.get("pep_trim_age", 0)

    # --- TRIM: at max long + sustained elevation -> sell TRIM_SIZE at best_bid ---
    if pos >= PEPPER_LIMIT and streak >= PEP_CONFIRM_TICKS and trim_px == 0 and bb is not None:
        qty = min(PEP_TRIM_SIZE, pos)
        if qty > 0:
            orders.append(Order(prod, bb, -qty))
            ts["pep_trim_px"] = bb
            ts["pep_trim_age"] = 0
            return orders  # skip rebuy logic this tick

    # --- REBUY GATE: if trimmed, wait for ask ≤ trim_px - gap, or capitulate at max age ---
    if trim_px > 0:
        trim_age += 1
        ts["pep_trim_age"] = trim_age
        ask_ok = ba is not None and ba <= trim_px - PEP_REBUY_GAP
        capitulate = trim_age >= PEP_TRIM_MAX_AGE
        if ask_ok or capitulate:
            ts["pep_trim_px"] = 0
            ts["pep_trim_age"] = 0
            # fall through to v12 buy logic
        else:
            return orders  # still holding trimmed position

    # ===== v12 normal buy logic =====
    # Take all sell liquidity to max long
    if ba is not None:
        for ap in sorted(od.sell_orders):
            q = min(abs(od.sell_orders[ap]), PEPPER_LIMIT - pos)
            if q > 0:
                orders.append(Order(prod, ap, q))
                pos += q
            if pos >= PEPPER_LIMIT:
                break

    rem = PEPPER_LIMIT - pos
    if rem <= 0 or bb is None:
        return orders

    if mid < ema - PEPPER_DIP_K:
        for offset, sz in [(0, 35), (-1, 25), (-2, 20)]:
            if rem <= 0:
                break
            q = min(sz, rem)
            px = bb + offset
            if ba is not None and px >= ba:
                px = ba - 1
            orders.append(Order(prod, px, q))
            rem -= q
    else:
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
                orders[prod] = trade_pepper(state, ts)
        return orders, 0, json.dumps(ts)
