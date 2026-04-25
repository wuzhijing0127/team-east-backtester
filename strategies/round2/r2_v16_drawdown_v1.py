"""
r2_v16_drawdown_v1 — v16 + PEPPER drawdown-triggered reduce-only mode
======================================================================

Adds a tail-risk kill switch to v16's PEPPER engine. When PEPPER PnL
drawdown from peak exceeds KILL_THRESHOLD, stops all new buys and
posts a gentle passive ask to unwind inventory. Exits the guard when
drawdown recovers to < KILL_THRESHOLD / 2 (hysteresis).

Insurance against a Round 2 eval sim where PEPPER doesn't drift up —
won't fire on clean trending data (observed normal DD ≈ 700, threshold
2500 is ~3.5x above that).

ASH: v16 engine (unchanged)
PEPPER: v16 core + drawdown guard
"""

import json
from datamodel import Order, OrderDepth, TradingState

MAF = 0

ASH_LIMIT = 20
ASH_INIT_FAIR = 10000
ASH_EMA_ALPHA = 0.05
ASH_HALF_SPREAD = 1
ASH_K_INV = 2.5
ASH_BASE_SIZE = 8
ASH_TAKE_EDGE = 1
ASH_MICRO_BETA = 0.04

PEPPER_LIMIT = 80
PEPPER_EMA_ALPHA = 0.02
PEPPER_DIP_K = 2

# Drawdown guard
PEP_DD_KILL = 2500            # enter reduce-only when PnL drops this far from peak
PEP_DD_RECOVER = 1250         # exit reduce-only when drawdown recovers below this
PEP_UNWIND_SIZE = 10          # passive ask size while in reduce-only


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

    ema = ts.get("pep_ema", mid)
    ema = PEPPER_EMA_ALPHA * mid + (1 - PEPPER_EMA_ALPHA) * ema
    ts["pep_ema"] = ema

    # --- Track unrealized PnL for drawdown guard ---
    # Approximation: delta_pos × mid ≈ fill cost this tick (close enough since
    # spread is 1-2 on PEPPER; exact P&L needs fill-price tracking we don't have).
    last_pos = ts.get("pep_last_pos", 0)
    cost_basis = ts.get("pep_cost_basis", 0.0)
    cost_basis += (pos - last_pos) * mid
    ts["pep_last_pos"] = pos
    ts["pep_cost_basis"] = cost_basis

    unrealized = pos * mid - cost_basis
    peak_pnl = max(ts.get("pep_peak_pnl", 0.0), unrealized)
    ts["pep_peak_pnl"] = peak_pnl
    drawdown = peak_pnl - unrealized

    reduce_only = ts.get("pep_reduce_only", False)
    if not reduce_only and drawdown > PEP_DD_KILL and pos > 0:
        reduce_only = True
    elif reduce_only and drawdown < PEP_DD_RECOVER:
        reduce_only = False
    ts["pep_reduce_only"] = reduce_only

    # --- Reduce-only branch: no buys, gentle passive unwind ---
    if reduce_only:
        if pos > 0 and ba is not None:
            qty = min(PEP_UNWIND_SIZE, pos)
            px = ba + 1  # passive ask above best_ask (doesn't sell into crash)
            orders.append(Order(prod, px, -qty))
        return orders

    # --- Normal v16 buy logic ---
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
