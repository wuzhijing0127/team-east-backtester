"""
r2_ash_directional_v1 — directional ASH with trend+inventory+edge gates
========================================================================

ASH engine replaced with directional passive quoting:
  1. No-trade zone: edge < 0.5 or spread <= 1
  2. Trend filter: if mid > ema+2 → only buy; if mid < ema-2 → only sell
  3. Inventory filter: |pos|/limit > 0.7 disables adding to heavy side
  4. Directional quoting: bid only if mid < fair, ask only if mid > fair
  5. Size scales with |mid - fair|

PEPPER unchanged from r3_hybrid_v3_tunedgate (trend-gated max-long).
"""

import json
from datamodel import Order, OrderDepth, TradingState

MAF = 0

# --- ASH (new directional engine) ---
ASH_LIMIT = 20
ASH_INIT_FAIR = 10000
ASH_EMA_ALPHA = 0.05
ASH_MICRO_BETA = 0.03
ASH_BASE_SIZE = 8
ASH_EDGE_MIN = 0.5
ASH_TREND_BAND = 2.0       # |mid - ema| > this triggers trend filter
ASH_INV_THRESHOLD = 0.7    # |pos|/limit above this disables heavy side

# --- PEPPER (v3_tunedgate, unchanged) ---
PEPPER_LIMIT = 80
PEPPER_EMA_ALPHA = 0.02
PEPPER_DIP_K = 2
PEPPER_TREND_FAST_ALPHA = 2 / 15
PEPPER_TREND_SLOW_ALPHA = 2 / 61
TREND_UP_CONFIRM = 0.15
TREND_DOWN_CONFIRM = -0.10
TREND_CONFIRM_TICKS = 3
PEPPER_UPTREND_TARGET = 80
PEPPER_NEUTRAL_TARGET = 60
PEPPER_DOWNTREND_TARGET = 10


def bb_ba(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def microprice(od, bid, ask):
    bv = od.buy_orders[bid]
    av = abs(od.sell_orders[ask])
    t = bv + av
    return (bid + ask) / 2 if t == 0 else (bid * av + ask * bv) / t


def compute_size(edge):
    # linear: edge=0.5→1, edge=4+→ASH_BASE_SIZE (clamped)
    s = round(edge * 2)
    if s < 1:
        s = 1
    if s > ASH_BASE_SIZE:
        s = ASH_BASE_SIZE
    return s


# ===================== ASH directional =====================
def trade_ash(state, ts):
    prod = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(prod)
    if od is None:
        return []
    pos = state.position.get(prod, 0)
    limit = ASH_LIMIT
    best_bid, best_ask = bb_ba(od)
    if best_bid is None or best_ask is None:
        return []
    mid = (best_bid + best_ask) / 2
    micro = microprice(od, best_bid, best_ask)

    ema = ts.get("ash_fair", ASH_INIT_FAIR)
    ema = ASH_EMA_ALPHA * mid + (1 - ASH_EMA_ALPHA) * ema
    ts["ash_fair"] = ema

    fair = ema + ASH_MICRO_BETA * (micro - mid)

    edge = abs(mid - fair)
    spread = best_ask - best_bid

    # 1. no-trade zone
    if edge < ASH_EDGE_MIN or spread <= 1:
        return []

    # 2. trend filter
    if mid > ema + ASH_TREND_BAND:
        allow_sell = False
        allow_buy = True
    elif mid < ema - ASH_TREND_BAND:
        allow_buy = False
        allow_sell = True
    else:
        allow_buy = True
        allow_sell = True

    # 3. inventory filter
    if limit > 0 and abs(pos) / limit > ASH_INV_THRESHOLD:
        if pos > 0:
            allow_buy = False
        else:
            allow_sell = False

    # 4. directional quoting
    place_bid = False
    place_ask = False
    if mid < fair:
        place_bid = allow_buy
    elif mid > fair:
        place_ask = allow_sell

    # 5. size scaling
    size = compute_size(edge)

    orders = []
    fr = round(fair)
    if place_bid:
        qty = min(size, limit - pos)
        if qty > 0:
            px = min(best_bid + 1, fr - 1)
            if px < best_ask:
                orders.append(Order(prod, px, qty))
    if place_ask:
        qty = min(size, limit + pos)
        if qty > 0:
            px = max(best_ask - 1, fr + 1)
            if px > best_bid:
                orders.append(Order(prod, px, -qty))
    return orders


# ===================== PEPPER (v3_tunedgate, unchanged) =====================
def trade_pepper(state, ts):
    prod = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(prod)
    if od is None:
        return []
    pos = state.position.get(prod, 0)
    bid, ask = bb_ba(od)
    if bid is None or ask is None:
        return []
    mid = (bid + ask) / 2

    ema = ts.get("pep_ema", mid)
    ema = PEPPER_EMA_ALPHA * mid + (1 - PEPPER_EMA_ALPHA) * ema
    ts["pep_ema"] = ema

    fast = ts.get("pep_fast", mid)
    slow = ts.get("pep_slow", mid)
    fast = PEPPER_TREND_FAST_ALPHA * mid + (1 - PEPPER_TREND_FAST_ALPHA) * fast
    slow = PEPPER_TREND_SLOW_ALPHA * mid + (1 - PEPPER_TREND_SLOW_ALPHA) * slow
    ts["pep_fast"] = fast
    ts["pep_slow"] = slow
    trend = fast - slow

    up_streak = ts.get("pep_up_streak", 0)
    dn_streak = ts.get("pep_dn_streak", 0)
    if trend >= TREND_UP_CONFIRM:
        up_streak += 1; dn_streak = 0
    elif trend <= TREND_DOWN_CONFIRM:
        dn_streak += 1; up_streak = 0
    else:
        up_streak = max(0, up_streak - 1)
        dn_streak = max(0, dn_streak - 1)
    ts["pep_up_streak"] = up_streak
    ts["pep_dn_streak"] = dn_streak

    regime = ts.get("pep_regime", "NEUTRAL")
    if up_streak >= TREND_CONFIRM_TICKS:
        regime = "UP"
    elif dn_streak >= TREND_CONFIRM_TICKS:
        regime = "DOWN"
    elif up_streak == 0 and dn_streak == 0:
        regime = "NEUTRAL"
    ts["pep_regime"] = regime

    if regime == "UP":
        target = PEPPER_UPTREND_TARGET
    elif regime == "DOWN":
        target = PEPPER_DOWNTREND_TARGET
    else:
        target = PEPPER_NEUTRAL_TARGET

    orders = []
    if pos > target:
        excess = pos - target
        q = min(excess, od.buy_orders.get(bid, 0))
        if q > 0:
            orders.append(Order(prod, bid, -q))
            pos -= q
        return orders

    deficit = target - pos
    if deficit <= 0:
        return orders

    if regime == "UP":
        cur = pos
        for ap in sorted(od.sell_orders):
            if cur >= target: break
            q = min(abs(od.sell_orders[ap]), target - cur)
            if q > 0:
                orders.append(Order(prod, ap, q))
                cur += q
        rem = target - cur
        if rem > 0:
            if mid < ema - PEPPER_DIP_K:
                for offset, sz in [(0, 35), (-1, 25), (-2, 20)]:
                    if rem <= 0: break
                    q = min(sz, rem)
                    px = bid + offset
                    if px >= ask: px = ask - 1
                    orders.append(Order(prod, px, q)); rem -= q
            else:
                orders.append(Order(prod, bid, rem))
    elif regime == "NEUTRAL":
        orders.append(Order(prod, bid, deficit))
    return orders


# ===================== Trader =====================
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
