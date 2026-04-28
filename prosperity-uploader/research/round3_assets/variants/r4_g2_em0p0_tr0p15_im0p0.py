"""
Round 4 Assets-Only Strategy — momentum-aware engine.

Trades ONLY HYDROGEL_PACK and VELVETFRUIT_EXTRACT (10 VEV options ignored).
Fair value comes from market data — no hardcoded anchors.

Refactored from cautious-MM to asymmetric momentum trading (2026-04-26):
  - Composite momentum signal = trend_coef·Δmid + vel_coef·EMA-velocity
  - Target position = scaler · signal, clipped to ±soft_cap
  - Inventory penalty (k_inv) RELAXES under strong signal (k_relax_coef)
  - Asymmetric maker quoting: bigger toward target, smaller (or zero) opposite
  - Aggressive takes use alpha_take_size when |signal| > alpha_threshold

Active mechanisms:
  - micro_coef × (microprice − mid)        # book tilt
  - imb_coef × L1_imbalance × spread       # flow tilt
  - trend_coef × (mid − mid_lag)           # mid momentum (PRIMARY ALPHA)
  - vel_coef × EMA-velocity                # smoothed momentum (PRIMARY ALPHA)
  - ema_alpha smoothing on composed fair   # stability anchor
  - k_inv inventory shade — relaxes under strong alpha
  - target_pos + asymmetric quoting + alpha-aware sizing
  - catastrophic insurance stop-loss

Removed: mean-reversion, asymm_skew, vol_widen_coef, deep_imb_coef,
imb_window, long_ema_alpha, ema_blend, micro_trend_coef.
"""

import json
import math
from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List, Tuple

# ── PARAMS_BLOCK_START ──
VARIANT_ID = 'g2_em0p0_tr0p15_im0p0'
VARIANT_LABEL = "VF grid {'ema_alpha': 0.0, 'trend_coef': 0.15, 'imb_coef': 0.0}, HG={'ema_alpha': 0.2, 'trend_coef': 0.45, 'imb_coef': 0.4, 'vel_coef': 0.3}"

HG_PARAMS = {
    'limit': 200,
    'soft_cap': 150,
    'half_spread': 4,
    'base_size': 20,
    'quote_size': 20,
    'take_size': 20,
    'take_edge': 8,
    'k_inv': 1.0,
    'micro_coef': 0.5,
    'imb_coef': 0.4,
    'ema_alpha': 0.2,
    'trend_coef': 0.45,
    'trend_lag': 5,
    'vel_coef': 0.3,
    'vel_alpha': 0.1,
    'target_pos_scaler': 10.0,
    'alpha_threshold': 1.5,
    'alpha_take_size': 50,
    'alpha_quote_size': 40,
    'mm_size_reduced': 5,
    'k_relax_coef': 0.5,
    'pos_gap_threshold': 10,
    'alpha_scale': 0.0,
    'target_cap': 150,
    'size_step': 1000000000,
    'take_size_big': 20,
    'take_trigger': 1000000000,
    'conviction_norm': 1000000000,
    'conviction_relief': 0.0,
    'k_inv_floor': 1.0,
    'one_sided_trigger': 1000000000,
    'opposite_min_size': 20,
    'chop_threshold': 0.0,
    'chop_target_cap_scale': 1.0,
    'chop_take_scale': 1.0,
    'chop_relief_scale': 1.0,
    'diag_print_every': 0,
    'stop_pos_threshold': 100,
    'stop_drawdown_threshold': 4000.0,
    'stop_min_ticks_at_pos': 200,
    'stop_unwind_size': 30,
}

VF_PARAMS = {
    'limit': 200,
    'soft_cap': 150,
    'half_spread': 1,
    'base_size': 15,
    'quote_size': 15,
    'take_size': 15,
    'take_edge': 2,
    'k_inv': 0.7,
    'micro_coef': 0.2,
    'imb_coef': 0.0,
    'ema_alpha': 0.0,
    'trend_coef': 0.15,
    'trend_lag': 5,
    'vel_coef': 0.2,
    'vel_alpha': 0.1,
    'target_pos_scaler': 10.0,
    'alpha_threshold': 1.0,
    'alpha_take_size': 40,
    'alpha_quote_size': 30,
    'mm_size_reduced': 3,
    'k_relax_coef': 0.5,
    'pos_gap_threshold': 10,
    'alpha_scale': 0.0,
    'target_cap': 150,
    'size_step': 1000000000,
    'take_size_big': 15,
    'take_trigger': 1000000000,
    'conviction_norm': 1000000000,
    'conviction_relief': 0.0,
    'k_inv_floor': 1.0,
    'one_sided_trigger': 1000000000,
    'opposite_min_size': 15,
    'chop_threshold': 0.0,
    'chop_target_cap_scale': 1.0,
    'chop_take_scale': 1.0,
    'chop_relief_scale': 1.0,
    'diag_print_every': 0,
    'stop_pos_threshold': 100,
    'stop_drawdown_threshold': 4000.0,
    'stop_min_ticks_at_pos': 200,
    'stop_unwind_size': 30,
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


def closeout(product, pos, bb, ba, flat_size):
    out = []
    if pos > 0 and bb is not None:
        out.append(Order(product, bb, -min(flat_size, pos)))
    elif pos < 0 and ba is not None:
        out.append(Order(product, ba, min(flat_size, -pos)))
    return out


def push_window(ts: dict, key: str, value: float, max_len: int) -> list:
    buf = ts.setdefault(key, [])
    buf.append(value)
    if len(buf) > max_len:
        del buf[: len(buf) - max_len]
    ts[key] = buf
    return buf


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
    half_spread = p["half_spread"]

    if state.timestamp >= UNWIND_START:
        return closeout(product, pos, bb, ba, 25)

    mid = get_mid(od)
    micro = get_microprice(od)
    spread = ba - bb

    # ── L1 imbalance (instantaneous) ──
    imb = get_l1_imbalance(od)

    # ── Mid-momentum signal ──
    trend_signal = 0.0
    if p["trend_coef"] != 0 and p["trend_lag"] > 0:
        mid_buf = push_window(ts, f"{product}_mid_buf", mid, p["trend_lag"] + 1)
        if len(mid_buf) > p["trend_lag"]:
            trend_signal = mid - mid_buf[-(p["trend_lag"] + 1)]

    # ── EMA-velocity signal (smoothed Δmid momentum) ──
    velocity = 0.0
    vel_coef = p["vel_coef"]
    if vel_coef != 0:
        vel_alpha = p["vel_alpha"]
        prev_mid_key = f"{product}_vel_prev_mid"
        vel_key = f"{product}_smooth_vel"
        prev_mid = ts.get(prev_mid_key, mid)
        prev_vel = ts.get(vel_key, 0.0)
        velocity = vel_alpha * (mid - prev_mid) + (1 - vel_alpha) * prev_vel
        ts[vel_key] = velocity
        ts[prev_mid_key] = mid

    # ── Momentum-only sub-signal (used for target_pos and strong-mode detection) ──
    momentum_signal = p["trend_coef"] * trend_signal + vel_coef * velocity
    signal_strength = abs(momentum_signal)
    is_strong = signal_strength >= p["alpha_threshold"]

    # ── Explicit alpha score (the "conviction" — bullish/bearish in price units) ──
    alpha_raw = (p["micro_coef"] * (micro - mid)
                 + p["imb_coef"] * imb * spread
                 + momentum_signal)
    ts[f"{product}_alpha_raw"] = alpha_raw  # exposed for inspection

    # ── Compose flow-driven fair ──
    base = mid + alpha_raw

    # ── Optional EMA smoothing on the composed fair ──
    if p["ema_alpha"] > 0:
        ema_key = f"{product}_fair_ema"
        prev = ts.get(ema_key, base)
        fair = p["ema_alpha"] * base + (1 - p["ema_alpha"]) * prev
        ts[ema_key] = fair
    else:
        fair = base

    orders: List[Order] = []
    base_size = p["base_size"]                       # legacy default
    take_size_default = p.get("take_size", base_size)
    quote_size_default = p.get("quote_size", base_size)
    take_edge = p["take_edge"]

    # ── Catastrophic insurance ──
    # Fires only when |pos| ≥ stop_pos_threshold AND
    # peak_mtm − cur_mtm ≥ stop_drawdown_threshold AND
    # ticks_at_pos ≥ stop_min_ticks_at_pos.
    # Posts an aggressive maker (best_ask−1 / best_bid+1) of stop_unwind_size.
    stop_pos_threshold = p["stop_pos_threshold"]
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

        if (sl_dd >= p["stop_drawdown_threshold"]
                and ticks_at_pos >= p["stop_min_ticks_at_pos"]):
            stop_size = p["stop_unwind_size"]
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
            ts[sl_peak_key] = cur_mtm  # reset so we don't refire on same dd
    elif stop_pos_threshold:
        # |pos| dropped below threshold — reset trackers
        ts[f"{product}_sl_mtm"] = 0.0
        ts[f"{product}_sl_peak"] = 0.0
        ts[f"{product}_sl_prev_mid"] = mid
        ts[f"{product}_sl_ticks"] = 0

    # ── Step 9: regime detection (chop vs trend) ──
    # trend_strength = |trend_signal| + |velocity| (in ticks)
    # Below `chop_threshold` → chop regime (smaller cap, normal brake, smaller takes).
    # Above → trend regime (full cap, weaker brake, full takes).
    trend_strength = abs(trend_signal) + abs(velocity)
    chop_threshold = p.get("chop_threshold", 0.0)  # 0 → never chop (default)
    in_chop = chop_threshold > 0 and trend_strength < chop_threshold
    ts[f"{product}_in_chop"] = 1 if in_chop else 0

    # Apply chop-time scales (1.0 = no change)
    chop_target_cap_scale = p.get("chop_target_cap_scale", 1.0)
    chop_take_scale = p.get("chop_take_scale", 1.0)
    chop_relief_scale = p.get("chop_relief_scale", 1.0)  # multiplies conviction_relief

    # ── Target position from alpha conviction ──
    # alpha_scale converts alpha_raw (in price units) into a desired lot count.
    # target_cap is the absolute lot ceiling (independent of soft_cap).
    alpha_scale = p.get("alpha_scale", p.get("target_pos_scaler", 0.0))
    target_cap = p.get("target_cap", soft_cap)
    if in_chop:
        target_cap = int(target_cap * chop_target_cap_scale)
    target_pos = int(max(-target_cap, min(target_cap, alpha_scale * alpha_raw)))
    ts[f"{product}_target_pos"] = target_pos
    pos_gap = target_pos - pos

    # ── Aggressive takes — per-side dynamic sizing toward target_pos ──
    # When pos is far below target → bigger BUY-side take (close the gap).
    # When pos is far above target → bigger SELL-side take.
    # Defaults (take_trigger=1e9) preserve symmetric take_size behavior.
    take_trigger = p.get("take_trigger", 1_000_000_000)
    take_size_big = p.get("take_size_big", take_size_default)
    if in_chop:
        take_size_big = int(take_size_big * chop_take_scale)
    buy_take = take_size_big if pos_gap > take_trigger else take_size_default
    sell_take = take_size_big if pos_gap < -take_trigger else take_size_default

    for ap in sorted(od.sell_orders.keys()):
        if fair - ap < take_edge:
            break
        room = max(0, limit - pos)
        qty = min(abs(od.sell_orders[ap]), room, buy_take)
        if qty > 0:
            orders.append(Order(product, ap, qty))
            pos += qty
    for bp in sorted(od.buy_orders.keys(), reverse=True):
        if bp - fair < take_edge:
            break
        room = max(0, limit + pos)
        qty = min(od.buy_orders[bp], room, sell_take)
        if qty > 0:
            orders.append(Order(product, bp, -qty))
            pos -= qty

    # ── Inventory shade — k_inv relaxes with conviction strength ──
    # conviction in [0,1]: how confident is alpha_raw (in ticks)?
    # k_eff drops as conviction grows, but never below k_inv_floor.
    conviction_norm = p.get("conviction_norm", 1_000_000_000)
    conviction_relief = p.get("conviction_relief", 0.0)
    if in_chop:
        conviction_relief = conviction_relief * chop_relief_scale  # weaker relief in chop
    k_inv_floor = p.get("k_inv_floor", p["k_inv"])
    conviction = min(1.0, abs(alpha_raw) / max(1e-9, conviction_norm))
    k_eff = p["k_inv"] * (1.0 - conviction_relief * conviction)
    k_eff = max(k_inv_floor, k_eff)
    inv_frac = pos / max(1, soft_cap)
    skew = k_eff * inv_frac * max(2, half_spread * 2) if pos != 0 else 0.0
    res = fair - skew

    bid_price = int(round(res - half_spread))
    ask_price = int(round(res + half_spread))

    # Inside-the-book by 1 tick when possible
    if bb + 1 < ba and bid_price > bb:
        bid_price = bb + 1
    if ba - 1 > bb and ask_price < ba:
        ask_price = ba - 1
    bid_price = min(bid_price, ba - 1)
    ask_price = max(ask_price, bb + 1)

    # ── Maker quote sizes — gentle graduated lean toward target_pos ──
    # buy_bias = how many lots we need to BUY to reach target → bigger bid
    # sell_bias = how many we need to SELL → bigger ask
    # size_step controls how aggressively we scale: 1 extra lot per `size_step`
    # of gap. Default 1e9 = no lean (behavior-inert), tune down to activate.
    room_buy = max(0, soft_cap - pos)
    room_sell = max(0, soft_cap + pos)
    size_step = p.get("size_step", 1_000_000_000)  # huge default = inert
    buy_bias = max(0, pos_gap)
    sell_bias = max(0, -pos_gap)
    b_size = min(int(quote_size_default + buy_bias // size_step), room_buy)
    a_size = min(int(quote_size_default + sell_bias // size_step), room_sell)

    # ── Step 8: one-sided gate — shrink opposite-side quote on big gap ──
    one_sided_trigger = p.get("one_sided_trigger", 1_000_000_000)
    opposite_min = p.get("opposite_min_size", quote_size_default)
    if pos_gap > one_sided_trigger:
        a_size = min(a_size, opposite_min)
    elif pos_gap < -one_sided_trigger:
        b_size = min(b_size, opposite_min)
    if b_size > 0 and bid_price > 0:
        orders.append(Order(product, bid_price, b_size))
    if a_size > 0 and ask_price > 0:
        orders.append(Order(product, ask_price, -a_size))

    # ── Step 10: post-fill diagnostics (sampled to keep log size sane) ──
    # Prints once every `diag_print_every` ticks per product.
    # Schema (parseable): "DIAG <product> ts=N alpha=X tgt=Y pos=Z gap=W k_eff=K
    #                       buy=B ask=A trend=T vel=V chop=C tickmid=M"
    # Plus tracks last fill mid + ts so an offline analyzer can compute
    # post-fill drift over the next 3/5/10 ticks.
    diag_every = p.get("diag_print_every", 0)
    if diag_every > 0:
        diag_key = f"{product}_diag_ctr"
        ctr = ts.get(diag_key, 0) + 1
        ts[diag_key] = ctr
        if ctr % diag_every == 0:
            chop_tag = 1 if in_chop else 0
            print(f"DIAG {product} ts={state.timestamp} "
                  f"alpha={alpha_raw:.2f} tgt={target_pos} pos={pos} "
                  f"gap={pos_gap} k_eff={k_eff:.3f} "
                  f"buy={b_size} ask={a_size} trend={trend_signal:.2f} "
                  f"vel={velocity:.3f} chop={chop_tag} mid={mid:.1f}")
        # Track last fill snapshot for offline post-fill drift
        if orders:
            ts[f"{product}_last_fill_ts"] = state.timestamp
            ts[f"{product}_last_fill_mid"] = mid

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

# nonce: 2026-04-26T19:25:46.448630 217390660
