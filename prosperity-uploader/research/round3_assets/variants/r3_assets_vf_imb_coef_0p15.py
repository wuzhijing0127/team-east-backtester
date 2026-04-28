"""
Round 3 Assets-Only Strategy — parameterized template (v2 — richer signals).

Trades ONLY HYDROGEL_PACK and VELVETFRUIT_EXTRACT. All 10 VEV options are
ignored. Fair value is built from market data alone — never from a hardcoded
anchor — so the strategy survives if the platform shifts the price level.

Signals available (each switchable via PARAMS_BLOCK):
  - microprice tilt:    fair += micro_coef * (microprice - mid)
  - L1 imbalance:       fair += imb_coef * imb * spread (instantaneous or rolling)
  - book pressure:      multi-level imbalance using all 3 quote levels
  - mid trend:          fair += trend_coef * (mid - mid_lag)        (momentum)
                              OR
                        fair -= revert_coef * (mid - mid_avg_long)  (mean reversion)
  - microprice trend:   fair += micro_trend_coef * (micro - micro_lag)
  - regime-aware spread: tighter spread when realized vol is low, wider when high
  - asymmetric inventory skew: bigger penalty when long than short (or vice-versa)

The template renders the same engine for HG and VF — the params dictate which
signals are active and how strong they are.
"""

import json
import math
from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List, Tuple, Optional

# ── PARAMS_BLOCK_START ──
VARIANT_ID = 'vf_imb_coef_0p15'
VARIANT_LABEL = 'VF imb_coef=0.15'

HG_PARAMS = {
    'limit': 200,
    'soft_cap': 150,
    'half_spread': 6,
    'base_size': 20,
    'take_edge': 8,
    'k_inv': 0.1,
    'micro_coef': 0.5,
    'imb_coef': 0.4,
    'ema_alpha': 0.2,
    'imb_window': 0,
    'trend_coef': 0.4,
    'trend_lag': 8,
    'revert_coef': 0.2,
    'revert_window': 60,
    'micro_trend_coef': 0.0,
    'deep_imb_coef': 0.0,
    'vol_widen_coef': 0.0,
    'asymm_skew': 1.0,
    'long_ema_alpha': 0.0,
    'ema_blend': 0.0,
    'vel_coef': 0.0,
    'vel_alpha': 0.1,
}

VF_PARAMS = {
    'limit': 200,
    'soft_cap': 160,
    'half_spread': 1,
    'base_size': 15,
    'take_edge': 2,
    'k_inv': 0.08,
    'micro_coef': 0.2,
    'imb_coef': 0.15,
    'ema_alpha': 0.0,
    'imb_window': 0,
    'trend_coef': 0.0,
    'trend_lag': 5,
    'revert_coef': 0.4,
    'revert_window': 60,
    'micro_trend_coef': 0.0,
    'deep_imb_coef': 0.0,
    'vol_widen_coef': 0.0,
    'asymm_skew': 1.0,
    'long_ema_alpha': 0.0,
    'ema_blend': 0.0,
    'vel_coef': 0.0,
    'vel_alpha': 0.1,
}

SESSION_END = 1_000_000
UNWIND_START = 998_000
# ── PARAMS_BLOCK_END ────────────────────────────────────────────────


# ───────────────────────── helpers ─────────────────────────────────

def get_best_bid_ask(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def get_mid(od):
    bid, ask = get_best_bid_ask(od)
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def get_microprice(od):
    bid, ask = get_best_bid_ask(od)
    if bid is None or ask is None:
        return None
    bv = od.buy_orders[bid]
    av = abs(od.sell_orders[ask])
    if bv + av == 0:
        return (bid + ask) / 2
    return (bid * av + ask * bv) / (bv + av)


def get_l1_imbalance(od):
    bid, ask = get_best_bid_ask(od)
    if bid is None or ask is None:
        return 0.0
    bv = od.buy_orders.get(bid, 0)
    av = abs(od.sell_orders.get(ask, 0))
    s = bv + av
    return 0.0 if s == 0 else (bv - av) / s


def get_deep_imbalance(od):
    """Sum buy vs sell volumes across the top-3 levels of the book."""
    bvs = sum(od.buy_orders.values())
    avs = sum(abs(v) for v in od.sell_orders.values())
    s = bvs + avs
    return 0.0 if s == 0 else (bvs - avs) / s


def closeout(product, pos, bb, ba, flat_size):
    out = []
    if pos > 0 and bb is not None:
        out.append(Order(product, bb, -min(flat_size, pos)))
    elif pos < 0 and ba is not None:
        out.append(Order(product, ba, min(flat_size, -pos)))
    return out


# ─────────────────── rolling state helpers ─────────────────────────

def push_window(ts: dict, key: str, value: float, max_len: int) -> list:
    buf = ts.setdefault(key, [])
    buf.append(value)
    if len(buf) > max_len:
        del buf[: len(buf) - max_len]
    ts[key] = buf
    return buf


def avg(xs):
    return (sum(xs) / len(xs)) if xs else 0.0


def realized_vol(xs):
    """Rough realized vol — std of consecutive diffs."""
    if len(xs) < 3:
        return 0.0
    diffs = [xs[i] - xs[i - 1] for i in range(1, len(xs))]
    m = sum(diffs) / len(diffs)
    var = sum((d - m) ** 2 for d in diffs) / len(diffs)
    return math.sqrt(var)


# ─────────────────── shared flow-MM engine ─────────────────────────

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

    if state.timestamp >= UNWIND_START:
        return closeout(product, pos, bb, ba, 25)

    mid = get_mid(od)
    micro = get_microprice(od)
    spread = ba - bb

    # ── Imbalance signals ──
    imb_now = get_l1_imbalance(od)
    if p["imb_window"] > 0:
        buf = push_window(ts, f"{product}_imb_buf", imb_now, p["imb_window"])
        imb = avg(buf)
    else:
        imb = imb_now

    deep_imb = get_deep_imbalance(od) if p["deep_imb_coef"] != 0 else 0.0

    # ── Trend signals ──
    trend_signal = 0.0
    if p["trend_coef"] != 0 and p["trend_lag"] > 0:
        mid_buf = push_window(ts, f"{product}_mid_buf", mid, max(p["trend_lag"], p.get("revert_window", 0)) + 1)
        if len(mid_buf) > p["trend_lag"]:
            trend_signal = mid - mid_buf[-(p["trend_lag"] + 1)]
    else:
        # still maintain buffer if revert_coef needs it
        if p["revert_coef"] != 0 and p["revert_window"] > 0:
            push_window(ts, f"{product}_mid_buf", mid, p["revert_window"] + 1)

    revert_signal = 0.0
    if p["revert_coef"] != 0 and p["revert_window"] > 0:
        mid_buf = ts.get(f"{product}_mid_buf", [])
        if len(mid_buf) >= 5:
            revert_signal = mid - avg(mid_buf)

    micro_trend = 0.0
    if p["micro_trend_coef"] != 0:
        mb = push_window(ts, f"{product}_micro_buf", micro, 6)
        if len(mb) >= 4:
            micro_trend = micro - mb[-4]

    # ── EMA on the CHANGE of price (denoised velocity signal) ──
    # Maintains smooth_velocity = α · Δmid + (1-α) · prev_velocity, then adds
    # vel_coef · smooth_velocity to fair. Tests the hypothesis that price changes
    # have momentum. .get() defaults make this backward-compatible with older variants.
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

    # ── Compose flow-driven fair ──
    base = (mid
            + p["micro_coef"] * (micro - mid)
            + p["imb_coef"] * imb * spread
            + p["deep_imb_coef"] * deep_imb * spread
            + p["trend_coef"] * trend_signal
            - p["revert_coef"] * revert_signal
            + p["micro_trend_coef"] * micro_trend
            + vel_coef * velocity)

    if p["ema_alpha"] > 0:
        ema_key = f"{product}_fair_ema"
        prev = ts.get(ema_key, base)
        fair = p["ema_alpha"] * base + (1 - p["ema_alpha"]) * prev
        ts[ema_key] = fair
    else:
        fair = base

    # ── Dual-EMA self-anchoring (394682-style, but adaptive) ──
    # Maintain a slow microprice EMA as a self-discovered anchor; blend with
    # the short-signal fair via ema_blend. ema_blend=0 disables (no anchor),
    # ema_blend=0.7 gives 394682-HG-like stability without hardcoding.
    if p["long_ema_alpha"] > 0 and p["ema_blend"] > 0 and micro is not None:
        long_key = f"{product}_long_ema"
        prev_long = ts.get(long_key, micro)
        long_ema = p["long_ema_alpha"] * micro + (1 - p["long_ema_alpha"]) * prev_long
        ts[long_key] = long_ema
        fair = p["ema_blend"] * long_ema + (1 - p["ema_blend"]) * fair

    # ── Regime-aware half_spread (vol widening) ──
    half_spread = p["half_spread"]
    if p["vol_widen_coef"] != 0:
        mb_for_vol = ts.get(f"{product}_mid_buf", [])
        if len(mb_for_vol) >= 10:
            rv = realized_vol(mb_for_vol[-30:])
            half_spread = max(1, int(round(half_spread + p["vol_widen_coef"] * rv)))

    orders: List[Order] = []
    base_size = p["base_size"]
    take_edge = p["take_edge"]

    # ── Aggressive takes ──
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

    # ── Inventory-aware reservation skew (asymmetric optional) ──
    inv_frac = pos / max(1, soft_cap)
    asym = p["asymm_skew"]
    if pos > 0:
        skew = p["k_inv"] * asym * inv_frac * max(2, half_spread * 2)
    elif pos < 0:
        skew = p["k_inv"] * (1.0 / asym if asym > 0 else 1.0) * inv_frac * max(2, half_spread * 2)
    else:
        skew = 0.0
    res = fair - skew

    bid_price = int(math.floor(res - half_spread))
    ask_price = int(math.ceil(res + half_spread))

    # Inside the book by 1 tick when allowed
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


# ───────────────────────── trader ─────────────────────────────────

class Trader:
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        ts = json.loads(state.traderData) if state.traderData else {}
        orders: Dict[str, List[Order]] = {}

        if "VELVETFRUIT_EXTRACT" in state.order_depths:
            orders["VELVETFRUIT_EXTRACT"] = trade_flow_mm(
                state, ts, "VELVETFRUIT_EXTRACT", VF_PARAMS
            )
        if "HYDROGEL_PACK" in state.order_depths:
            orders["HYDROGEL_PACK"] = trade_flow_mm(
                state, ts, "HYDROGEL_PACK", HG_PARAMS
            )

        return orders, 0, json.dumps(ts)

# nonce: 2026-04-25T18:03:46.733548 161892767
