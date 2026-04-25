"""
r3_hybrid_v2_trendgate — robustness-focused for out-of-sample
==============================================================

RATIONALE: Final sim runs on similar-but-different data. v12 unconditionally
max-longs PEPPER — this is an all-in bet on the drift pattern. If final-sim
PEPPER doesn't drift up (flat/down), v12 loses ~80 × spread = several k with
no payoff. Adding a directional trend gate on PEPPER makes us robust across
scenarios at the cost of a bit of PnL when drift IS present.

STRATEGY DESIGN:
  ASH: v12 engine (simple EMA fair + microbeta=0.03 + small-limit MM) —
       proven winner, relatively robust already
  PEPPER: v12 engine GATED by trend direction:
    • Confirmed uptrend  → max-long + dip overlay (v12 behavior)
    • Neutral            → base long position only (partial exposure)
    • Confirmed downtrend → actively reduce to floor (don't load into decline)

Expected observed-sim outcome: slightly below v12 (~8,200-8,600) because
the gate adds hysteresis. But out-of-sample: much safer — won't blow up if
PEPPER doesn't drift.
"""

import json
from datamodel import Order, OrderDepth, TradingState

MAF = 0

# --- ASH (v12 proven) ---
ASH_LIMIT = 20
ASH_INIT_FAIR = 10000
ASH_EMA_ALPHA = 0.05
ASH_HALF_SPREAD = 1
ASH_K_INV = 2.5
ASH_BASE_SIZE = 8
ASH_TAKE_EDGE = 1
ASH_MICRO_BETA = 0.03

# --- PEPPER with trend gate ---
PEPPER_LIMIT = 80
PEPPER_EMA_ALPHA = 0.02        # for dip overlay
PEPPER_DIP_K = 2

# Trend gate parameters (v3 TUNED — thresholds match observed drift signal)
PEPPER_TREND_FAST_ALPHA = 2 / 15    # ~14-tick
PEPPER_TREND_SLOW_ALPHA = 2 / 61    # ~60-tick
# Steady-state: at drift r per tick, trend signal ≈ 23.5*r.
# Observed PEPPER drift ~0.01/tick → signal ≈ 0.23 → use 0.15 threshold.
TREND_UP_CONFIRM   =  0.15     # catches observed drift; below it stays NEUTRAL
TREND_DOWN_CONFIRM = -0.10     # slightly more sensitive to downside (flatten fast)
TREND_CONFIRM_TICKS = 3        # faster confirmation at lower threshold

# Position targets by regime
PEPPER_UPTREND_TARGET = 80     # max long when confirmed uptrend
PEPPER_NEUTRAL_TARGET = 60     # higher neutral (was 40) — less drag if gate flaps
PEPPER_DOWNTREND_TARGET = 10   # near-flat but keep tiny long (avoid full-flat thrash)


def bb_ba(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def microprice(od, bid, ask):
    bv = od.buy_orders[bid]
    av = abs(od.sell_orders[ask])
    t = bv + av
    return (bid + ask) / 2 if t == 0 else (bid * av + ask * bv) / t


# ===================== ASH (v12 engine, unchanged) =====================
def trade_ash(state, ts):
    prod = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(prod)
    if od is None:
        return []
    pos = state.position.get(prod, 0)
    bid, ask = bb_ba(od)
    if bid is None or ask is None:
        return []
    mid = (bid + ask) / 2
    micro = microprice(od, bid, ask)

    ema_fair = ts.get("ash_fair", ASH_INIT_FAIR)
    ema_fair = ASH_EMA_ALPHA * mid + (1 - ASH_EMA_ALPHA) * ema_fair
    ts["ash_fair"] = ema_fair

    fair = ema_fair + ASH_MICRO_BETA * (micro - mid)
    fr = round(fair)
    orders = []

    # Take
    for ap in sorted(od.sell_orders):
        if ap <= fr - ASH_TAKE_EDGE and pos < ASH_LIMIT:
            q = min(abs(od.sell_orders[ap]), ASH_LIMIT - pos)
            if q > 0:
                orders.append(Order(prod, ap, q)); pos += q
        else: break
    for bp in sorted(od.buy_orders, reverse=True):
        if bp >= fr + ASH_TAKE_EDGE and pos > -ASH_LIMIT:
            q = min(od.buy_orders[bp], ASH_LIMIT + pos)
            if q > 0:
                orders.append(Order(prod, bp, -q)); pos -= q
        else: break

    # Passive MM
    res = fair - ASH_K_INV * (pos / ASH_LIMIT)
    rr = round(res)
    bp = rr - ASH_HALF_SPREAD
    ap = rr + ASH_HALF_SPREAD
    bp = min(bid + 1, bp, ask - 1)
    ap = max(ask - 1, ap, bid + 1)
    if pos < ASH_LIMIT:
        orders.append(Order(prod, bp, min(ASH_BASE_SIZE, ASH_LIMIT - pos)))
    if pos > -ASH_LIMIT:
        orders.append(Order(prod, ap, -min(ASH_BASE_SIZE, ASH_LIMIT + pos)))
    return orders


# ===================== PEPPER with trend gate =====================
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

    # Update EMAs
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

    # Trend confirmation with streak tracking (hysteresis against flapping)
    up_streak = ts.get("pep_up_streak", 0)
    dn_streak = ts.get("pep_dn_streak", 0)
    if trend >= TREND_UP_CONFIRM:
        up_streak += 1; dn_streak = 0
    elif trend <= TREND_DOWN_CONFIRM:
        dn_streak += 1; up_streak = 0
    else:
        # Neutral — decay both (slow transition)
        up_streak = max(0, up_streak - 1)
        dn_streak = max(0, dn_streak - 1)
    ts["pep_up_streak"] = up_streak
    ts["pep_dn_streak"] = dn_streak

    # Sticky regime: once confirmed, hold until reversed for CONFIRM_TICKS
    regime = ts.get("pep_regime", "NEUTRAL")
    if up_streak >= TREND_CONFIRM_TICKS:
        regime = "UP"
    elif dn_streak >= TREND_CONFIRM_TICKS:
        regime = "DOWN"
    elif up_streak == 0 and dn_streak == 0:
        regime = "NEUTRAL"
    ts["pep_regime"] = regime

    # Pick target position by regime
    if regime == "UP":
        target = PEPPER_UPTREND_TARGET
    elif regime == "DOWN":
        target = PEPPER_DOWNTREND_TARGET
    else:
        target = PEPPER_NEUTRAL_TARGET

    orders = []

    # === Position management ===
    # Reduce if over target
    if pos > target:
        excess = pos - target
        q = min(excess, od.buy_orders.get(bid, 0))
        if q > 0:
            orders.append(Order(prod, bid, -q))
            pos -= q
        return orders

    # Build up to target
    deficit = target - pos
    if deficit <= 0:
        return orders

    # In UP regime: v12 engine (aggressive take + dip overlay)
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
    # NEUTRAL: passive accumulation only (no aggressive take)
    elif regime == "NEUTRAL":
        orders.append(Order(prod, bid, deficit))
    # DOWN: target=0, don't add. Already handled above (pos > target reduces).
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
