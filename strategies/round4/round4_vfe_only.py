"""Round 4 VFE-only template — for alpha-signal sweeps.

Carved from round4_base.py: HG, options, and delta-hedge engines are gated
off so the platform's aggregate PnL curve equals VFE PnL alone. Only the
flow-MM engine on VELVETFRUIT_EXTRACT runs.

Timescale: IMC platform (0..99_900). UNWIND start rescaled accordingly
(round4_base.py used the 1M local-backtester scale).
"""

import json
import math
from typing import Dict, List, Tuple

from datamodel import Order, OrderDepth, TradingState


VFE = "VELVETFRUIT_EXTRACT"
STRIKES = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]
VEV = {k: f"VEV_{k}" for k in STRIKES}

UNDERLYING_UNWIND_START = 998_000


# ===== orderbook helpers =====
def get_best_bid_ask(od: OrderDepth):
    bb = max(od.buy_orders) if od.buy_orders else None
    ba = min(od.sell_orders) if od.sell_orders else None
    return bb, ba


def get_mid(od: OrderDepth):
    bb, ba = get_best_bid_ask(od)
    if bb is None or ba is None:
        return None
    return 0.5 * (bb + ba)


def get_microprice(od: OrderDepth):
    bb, ba = get_best_bid_ask(od)
    if bb is None or ba is None:
        return None
    bv = od.buy_orders[bb]
    av = abs(od.sell_orders[ba])
    if bv + av == 0:
        return 0.5 * (bb + ba)
    return (bb * av + ba * bv) / (bv + av)


def get_l1_imbalance(od: OrderDepth):
    bb, ba = get_best_bid_ask(od)
    if bb is None or ba is None:
        return 0.0
    bv = od.buy_orders[bb]
    av = abs(od.sell_orders[ba])
    if bv + av == 0:
        return 0.0
    return (bv - av) / (bv + av)


def closeout(product: str, pos: int, bb, ba, flat_size: int) -> List[Order]:
    if pos > 0 and bb is not None:
        return [Order(product, bb, -min(pos, flat_size))]
    if pos < 0 and ba is not None:
        return [Order(product, ba, min(-pos, flat_size))]
    return []


def push_window(ts: dict, key: str, value: float, max_len: int) -> list:
    buf = ts.get(key, [])
    buf.append(value)
    if len(buf) > max_len:
        buf = buf[-max_len:]
    ts[key] = buf
    return buf


def avg(xs):
    return sum(xs) / len(xs) if xs else 0.0


# ===== VFE alpha params (sweep target) =====
# 5 alpha groups (7 levers) + 6 structural + 4 catastrophic-insurance.
# This dict is what the sweep harness mutates. Defaults below are momentum
# baseline (after round-3 mean-revert post-mortem).
# === VF_PARAMS_BEGIN ===
VF_PARAMS = {
    # Inventory bounds
    'limit': 200,
    'soft_cap': 150,
    'base_size': 15,
    # Quote width / take threshold
    'half_spread': 1,
    'take_edge': 2,
    # Inventory shade (protective)
    'k_inv': 0.5,
    # Alpha — 5 groups, 7 levers
    'trend_coef': 0.3,      # momentum
    'trend_lag': 5,
    'vel_coef': 0.3,        # ema-velocity
    'vel_alpha': 0.1,
    'micro_coef': 0.2,      # microprice tilt
    'imb_coef': 0.3,        # L1 imbalance
    'ema_alpha': 0.0,       # mid smoothing
    # Catastrophic insurance
    'stop_pos_threshold': 150,
    'stop_drawdown_threshold': 1500.0,
    'stop_min_ticks_at_pos': 100,
    'stop_unwind_size': 25,
}
# === VF_PARAMS_END ===


# ===== flow-MM engine — alpha-only signals =====
def trade_flow_mm(state: TradingState, ts: dict, product: str, p: dict) -> List[Order]:
    od = state.order_depths.get(product)
    if od is None:
        return []
    bb, ba = get_best_bid_ask(od)
    if bb is None or ba is None:
        return []

    pos = state.position.get(product, 0)
    limit = p["limit"]
    soft_cap = p["soft_cap"]

    if state.timestamp >= UNDERLYING_UNWIND_START:
        return closeout(product, pos, bb, ba, 25)

    mid = get_mid(od)
    micro = get_microprice(od)
    spread = ba - bb

    imb = get_l1_imbalance(od)

    # Trend (momentum)
    trend_signal = 0.0
    trend_coef = p.get("trend_coef", 0.0)
    trend_lag = p.get("trend_lag", 5)
    if trend_coef != 0 and trend_lag > 0:
        mid_buf = push_window(ts, f"{product}_mid_buf", mid, trend_lag + 1)
        if len(mid_buf) > trend_lag:
            trend_signal = mid - mid_buf[-(trend_lag + 1)]

    # EMA-velocity
    velocity = 0.0
    vel_coef = p.get("vel_coef", 0.0)
    if vel_coef != 0:
        vel_alpha = p.get("vel_alpha", 0.10)
        prev_mid_key = f"{product}_vel_prev_mid"
        vel_key = f"{product}_smooth_vel"
        prev_mid = ts.get(prev_mid_key, mid)
        delta_mid = mid - prev_mid
        prev_vel = ts.get(vel_key, 0.0)
        velocity = vel_alpha * delta_mid + (1 - vel_alpha) * prev_vel
        ts[vel_key] = velocity
        ts[prev_mid_key] = mid

    # Compose flow-driven fair
    base = (mid
            + p["micro_coef"] * (micro - mid)
            + p["imb_coef"] * imb * spread
            + trend_coef * trend_signal
            + vel_coef * velocity)

    if p["ema_alpha"] > 0:
        ema_key = f"{product}_fair_ema"
        prev = ts.get(ema_key, base)
        fair = p["ema_alpha"] * base + (1 - p["ema_alpha"]) * prev
        ts[ema_key] = fair
    else:
        fair = base

    half_spread = p["half_spread"]

    orders: List[Order] = []
    base_size = p["base_size"]
    take_edge = p["take_edge"]

    # Catastrophic stop-loss
    stop_pos_threshold = p.get("stop_pos_threshold", 0)
    if stop_pos_threshold and abs(pos) >= stop_pos_threshold:
        sl_mtm_key = f"{product}_sl_mtm"
        sl_peak_key = f"{product}_sl_peak"
        sl_prev_mid_key = f"{product}_sl_prev_mid"
        sl_ticks_key = f"{product}_sl_ticks"

        prev_sl_mid = ts.get(sl_prev_mid_key, mid)
        cur_mtm = ts.get(sl_mtm_key, 0.0) + pos * (mid - prev_sl_mid)
        peak_mtm = max(ts.get(sl_peak_key, cur_mtm), cur_mtm)
        sl_dd = peak_mtm - cur_mtm
        ticks_at_pos = ts.get(sl_ticks_key, 0) + 1

        ts[sl_mtm_key] = cur_mtm
        ts[sl_peak_key] = peak_mtm
        ts[sl_prev_mid_key] = mid
        ts[sl_ticks_key] = ticks_at_pos

        if (sl_dd >= p.get("stop_drawdown_threshold", 800.0)
                and ticks_at_pos >= p.get("stop_min_ticks_at_pos", 100)):
            stop_size = p.get("stop_unwind_size", 20)
            if pos > 0 and ba is not None:
                qty = min(stop_size, pos)
                px = ba - 1 if ba - 1 > bb else ba
                orders.append(Order(product, px, -qty))
                pos -= qty
            elif pos < 0 and bb is not None:
                qty = min(stop_size, -pos)
                px = bb + 1 if bb + 1 < ba else bb
                orders.append(Order(product, px, qty))
                pos += qty
            ts[sl_peak_key] = cur_mtm
    elif stop_pos_threshold:
        ts[f"{product}_sl_mtm"] = 0.0
        ts[f"{product}_sl_peak"] = 0.0
        ts[f"{product}_sl_prev_mid"] = mid
        ts[f"{product}_sl_ticks"] = 0

    # Aggressive takes
    for ap in sorted(od.sell_orders.keys()):
        if fair - ap < take_edge:
            break
        room = max(0, limit - pos)
        qty = min(abs(od.sell_orders[ap]), room, base_size)
        if qty > 0:
            orders.append(Order(product, ap, qty))
            pos += qty
    for bp in sorted(od.buy_orders.keys(), reverse=True):
        if bp - fair < take_edge:
            break
        room = max(0, limit + pos)
        qty = min(od.buy_orders[bp], room, base_size)
        if qty > 0:
            orders.append(Order(product, bp, -qty))
            pos -= qty

    # Symmetric inventory-aware reservation skew
    inv_frac = pos / max(1, soft_cap)
    skew = p["k_inv"] * inv_frac * max(2, half_spread * 2)
    res = fair - skew

    bid_price = int(math.floor(res - half_spread))
    ask_price = int(math.ceil(res + half_spread))

    if bb + 1 < ba and bid_price > bb:
        bid_price = bb + 1
    if ba - 1 > bb and ask_price < ba:
        ask_price = ba - 1
    bid_price = min(bid_price, ba - 1)
    ask_price = max(ask_price, bb + 1)

    room_buy = max(0, soft_cap - pos)
    room_sell = max(0, soft_cap + pos)
    b_size = min(base_size, room_buy)
    a_size = min(base_size, room_sell)
    if b_size > 0 and bid_price > 0:
        orders.append(Order(product, bid_price, b_size))
    if a_size > 0 and ask_price > 0:
        orders.append(Order(product, ask_price, -a_size))

    return orders


def _load(td):
    if not td:
        return {}
    try:
        return json.loads(td)
    except Exception:
        return {}


class Trader:
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        ts = _load(state.traderData)
        books = state.order_depths

        # Empty orders for all non-VFE tradables (HG + 10 VEV options gated off)
        orders: Dict[str, List[Order]] = {VEV[k]: [] for k in STRIKES}
        orders["HYDROGEL_PACK"] = []
        orders[VFE] = []

        if VFE in books:
            orders[VFE] = trade_flow_mm(state, ts, VFE, VF_PARAMS)

        return orders, 0, json.dumps(ts)
