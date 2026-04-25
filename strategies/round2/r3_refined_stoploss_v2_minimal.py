"""
r3_refined_stoploss_v2_minimal — v3 base + ONLY catastrophic triggers
======================================================================

LESSONS LEARNED from stoploss_v1 (PnL 7,290 vs v3 8,826 — lost 1,536):
  • Trailing stops on PEPPER kill the drift-capture engine
  • Per-product drawdown stops trigger too often on normal volatility
  • Regime/vol/adverse-sel triggers fire too eagerly → frequent mode changes
  • The state-machine downgrade forced reduced sizes during profitable drift

STRIPPED DESIGN — only TWO triggers remain:
  L1  PORTFOLIO KILL SWITCH  — total MtM PnL < -3000 → HALT all trading
       (acts as a wide backstop; should rarely trigger on a working strategy)

  L2  END-OF-SESSION REDUCTION — last 5k ticks → reduce PEPPER passive quote by 50%
       (light touch — avoids last-moment adverse fills; does NOT force flatten)

No trailing stops, no regime detection, no adverse-selection pauses, no
stagnation triggers. Everything that hurt in v1 is gone.

ALSO includes microprice tilt β=0.05 (from v13 sweep) if it wins.
"""

import json
from datamodel import Order, OrderDepth, TradingState

MAF = 0

# v3 params
ASH_LIMIT = 20
ASH_INIT_FAIR = 10000
ASH_EMA_ALPHA = 0.05
ASH_HALF_SPREAD = 1
ASH_K_INV = 2.5
ASH_BASE_SIZE = 8
ASH_TAKE_EDGE = 1
ASH_MICRO_BETA = 0.05   # will be swept separately by v12-v14; keep moderate here

PEPPER_LIMIT = 80
PEPPER_EMA_ALPHA = 0.02
PEPPER_DIP_K = 2

# Minimal stop-loss thresholds
PORTFOLIO_KILL_SWITCH = -3000
EOS_REDUCE_WINDOW = 5000
SIM_END_TS = 99900


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


def update_pnl_est(ts_data, prod_key, pos, mid):
    """Minimal MtM estimate for kill switch."""
    state = ts_data.setdefault("pnl_est_" + prod_key, {"cb": None, "last_pos": pos, "pnl": 0.0})
    last_pos = state["last_pos"]
    if mid is not None:
        delta = pos - last_pos
        if delta != 0 and state["cb"] is not None:
            fill_px = ts_data.get("last_mid_" + prod_key, mid)
            if last_pos == 0:
                state["cb"] = fill_px
            elif (last_pos > 0 and delta > 0) or (last_pos < 0 and delta < 0):
                new_qty = abs(last_pos) + abs(delta)
                state["cb"] = (abs(last_pos) * state["cb"] + abs(delta) * fill_px) / new_qty
            else:
                realized = abs(delta) * (fill_px - state["cb"]) * (1 if last_pos > 0 else -1)
                state["pnl"] += realized
        if state["cb"] is None and pos != 0:
            state["cb"] = mid
        ts_data["last_mid_" + prod_key] = mid
    state["last_pos"] = pos
    unrealized = pos * (mid - state["cb"]) if (pos != 0 and mid is not None and state["cb"] is not None) else 0.0
    return state["pnl"] + unrealized


def trade_ash(state, ts, halted, eos_reduce):
    prod = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(prod)
    if od is None or halted:
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


def trade_pepper(state, ts, halted, eos_reduce):
    prod = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(prod)
    if od is None or halted:
        return []
    pos = state.position.get(prod, 0)
    bb, ba = bb_ba(od)
    mid = mid_of(od)
    orders = []
    if mid is None:
        return orders

    ema = ts.get("pep_ema", mid)
    ema = PEPPER_EMA_ALPHA * mid + (1 - PEPPER_EMA_ALPHA) * ema
    ts["pep_ema"] = ema

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

    # EOS reduction: halve the passive bid size in last 5k ticks
    size_factor = 0.5 if eos_reduce else 1.0

    if mid < ema - PEPPER_DIP_K:
        for offset, sz in [(0, 35), (-1, 25), (-2, 20)]:
            if rem <= 0:
                break
            q = min(int(sz * size_factor), rem)
            if q <= 0:
                continue
            px = bb + offset
            if ba is not None and px >= ba:
                px = ba - 1
            orders.append(Order(prod, px, q))
            rem -= q
    else:
        q = max(1, int(rem * size_factor))
        orders.append(Order(prod, bb, q))
    return orders


class Trader:
    def run(self, state):
        orders = {}
        ts = json.loads(state.traderData) if state.traderData else {}
        timestamp = getattr(state, "timestamp", 0)

        # Compute portfolio PnL estimate for kill switch
        ash_pos = state.position.get("ASH_COATED_OSMIUM", 0)
        pep_pos = state.position.get("INTARIAN_PEPPER_ROOT", 0)
        ash_od = state.order_depths.get("ASH_COATED_OSMIUM")
        pep_od = state.order_depths.get("INTARIAN_PEPPER_ROOT")
        ash_mid = mid_of(ash_od) if ash_od else None
        pep_mid = mid_of(pep_od) if pep_od else None

        ash_pnl = update_pnl_est(ts, "ash", ash_pos, ash_mid)
        pep_pnl = update_pnl_est(ts, "pepper", pep_pos, pep_mid)
        total_pnl = ash_pnl + pep_pnl

        # L1: Portfolio kill switch
        halted = total_pnl < PORTFOLIO_KILL_SWITCH
        # L2: End-of-session reduction
        eos_reduce = (SIM_END_TS - timestamp) <= EOS_REDUCE_WINDOW

        for prod in state.order_depths:
            if prod == "ASH_COATED_OSMIUM":
                orders[prod] = trade_ash(state, ts, halted, eos_reduce)
            elif prod == "INTARIAN_PEPPER_ROOT":
                orders[prod] = trade_pepper(state, ts, halted, eos_reduce)

        return orders, 0, json.dumps(ts)
