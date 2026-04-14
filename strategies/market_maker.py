"""
Adaptive Market Making Strategy — EMERALDS + TOMATOES  [vNext]
==============================================================
EMERALDS: anchored MM with two-level quoting and regime-aware take ladder.
TOMATOES: state-machine alpha MM with tiered toxicity, take ladder, and L2 quotes.

Architecture:
- Shared helpers: book signals, OFI, toxicity, safety guards, regime classifiers
- trade_emeralds: regime (anchor_normal / anchor_wide / inv_extreme) → multi-level quotes
- trade_tomatoes: 10-state machine → state-gated take ladder + optional L2 passive
- Passive quotes always recomputed AFTER aggressive takes using post-take pos_sim
"""

import json
import math
from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List, Optional, Tuple

# ============================================================
# TUNABLE PARAMETERS
# ============================================================
PARAMS = {
    "EMERALDS": {
        "position_limit": 50,
        "micro_beta": 0.0,              # pure anchor; raise to 0.1-0.2 for microprice nudge
        "k_inv": 2.5,                   # reservation = fair - k_inv*(pos/limit)*base_hs
        "base_half_spread": 5,          # L2 half-spread (deeper quote)
        "l1_half_spread": 2,            # L1 half-spread (queue-priority quote)
        "l1_size": 4,                   # size at L1 (queue-competitive, smaller)
        "enable_multi_level": True,     # enable two-level bid/ask quoting
        "take_edge": 2,                 # take if ask <= fair - take_edge
        "base_size": 10,                # total passive size per side (L1 + L2)
        "vol_window": 12,               # rolling window for returns-based vol
        "enable_vol_adjust": False,     # widen spread proportionally to vol
        "vol_spread_multiplier": 0.3,
        "flatten_size": 5,              # units placed at fair when |pos| >= 90% limit
        "wide_spread_threshold": 3,     # spread >= this → "wide" bucket
        "tight_spread_threshold": 1,    # spread <= this → "tight" bucket
    },
    "TOMATOES": {
        "position_limit": 50,
        "fair_weights": {
            "wall_mid":   0.5,
            "vwap_mid":   0.3,
            "microprice": 0.2,
        },
        "alpha_weights": {
            "micro_dev":  1.0,          # spread-normalized (micro - mid) / spread
            "imbalance":  0.5,          # L1 (bid_sz - ask_sz) / total
            "flow":       0.5,          # recent trade flow ratio
            "ofi":        0.3,          # best-level OFI
        },
        "gamma_return_fair": 0.05,      # fair += gamma * (mid - prev_mid)
        "k_inv": 3.0,
        "base_half_spread": 4,
        "take_edge": 3,
        "base_size": 8,
        "vol_window": 20,
        "vol_spread_multiplier": 0.5,
        "flow_window": 10,
        # State-machine thresholds
        "attack_alpha_threshold": 0.35, # |alpha_norm| >= this → attack_long/short
        "mild_alpha_threshold": 0.15,   # |alpha_norm| >= this → bullish/bearish_mm
        "tox_threshold": 0.5,           # |tox_score| >= this → toxic_buy/sell
        "severe_tox_threshold": 0.75,   # |tox_score| >= this → severe_toxic_*
        "tox_spread_add": 1,            # extra half-spread per toxicity tier
        "tox_weights": {"flow": 0.6, "ofi": 0.4},
        # Take ladder depth by regime class
        "take_levels": {"attack": 2, "mild": 1},
        # Edge and size skew
        "alpha_edge_scale": 0.5,
        "alpha_size_scale": 0.3,
        # L2 quoting
        "l2_spread_mult": 1.5,          # L2 edge = base_edge * l2_spread_mult
        "l2_size_fraction": 0.5,        # L2 size as fraction of base_size
        "wide_spread_threshold": 4,
        "tight_spread_threshold": 1,
        "flatten_size": 5,
    },
}


# ============================================================
# SHARED BOOK / SIGNAL HELPERS
# ============================================================

def get_best_bid_ask(order_depth: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    if not order_depth.buy_orders or not order_depth.sell_orders:
        return None, None
    return max(order_depth.buy_orders), min(order_depth.sell_orders)


def get_mid(order_depth: OrderDepth) -> Optional[float]:
    bb, ba = get_best_bid_ask(order_depth)
    if bb is None or ba is None:
        return None
    return (bb + ba) / 2


def get_spread(order_depth: OrderDepth) -> Optional[float]:
    bb, ba = get_best_bid_ask(order_depth)
    if bb is None or ba is None:
        return None
    return float(ba - bb)


def get_wall_mid(order_depth: OrderDepth) -> Optional[float]:
    """Mid between deepest-volume bid wall and deepest-volume ask wall."""
    if not order_depth.buy_orders or not order_depth.sell_orders:
        return None
    bid_wall = max(order_depth.buy_orders, key=lambda p: order_depth.buy_orders[p])
    ask_wall = max(order_depth.sell_orders, key=lambda p: abs(order_depth.sell_orders[p]))
    return (bid_wall + ask_wall) / 2


def get_vwap_mid(order_depth: OrderDepth) -> Optional[float]:
    if not order_depth.buy_orders or not order_depth.sell_orders:
        return None
    bv = sum(order_depth.buy_orders.values())
    av = sum(abs(v) for v in order_depth.sell_orders.values())
    if bv == 0 or av == 0:
        return None
    bid_vwap = sum(p * v for p, v in order_depth.buy_orders.items()) / bv
    ask_vwap = sum(p * abs(v) for p, v in order_depth.sell_orders.items()) / av
    return (bid_vwap + ask_vwap) / 2


def get_microprice(order_depth: OrderDepth) -> Optional[float]:
    """L1 microprice = (ask*bid_sz + bid*ask_sz) / (bid_sz + ask_sz)."""
    bb, ba = get_best_bid_ask(order_depth)
    if bb is None or ba is None:
        return None
    bid_sz = order_depth.buy_orders[bb]
    ask_sz = abs(order_depth.sell_orders[ba])
    total = bid_sz + ask_sz
    if total == 0:
        return (bb + ba) / 2
    return (ba * bid_sz + bb * ask_sz) / total


def get_l1_imbalance(order_depth: OrderDepth) -> float:
    """L1 imbalance in [-1,1]: +1 = all volume on bid side."""
    bb, ba = get_best_bid_ask(order_depth)
    if bb is None or ba is None:
        return 0.0
    bid_sz = order_depth.buy_orders[bb]
    ask_sz = abs(order_depth.sell_orders[ba])
    total = bid_sz + ask_sz
    return (bid_sz - ask_sz) / total if total else 0.0


def clip_hist(hist: List[float], value: float, window: int) -> List[float]:
    """Append value and trim to rolling window length."""
    hist.append(value)
    return hist[-window:] if len(hist) > window else hist


def compute_rolling_std(hist: List[float]) -> float:
    if len(hist) < 2:
        return 0.0
    mean = sum(hist) / len(hist)
    return math.sqrt(sum((x - mean) ** 2 for x in hist) / len(hist))


def compute_trade_flow(product: str, state: TradingState, prev_mid: float, window: int) -> float:
    """Flow imbalance in [-1,1]: +1 = pure aggressive buy flow."""
    recent = state.market_trades.get(product, [])[-window:]
    if not recent:
        return 0.0
    buy_vol = sum(t.quantity for t in recent if t.price >= prev_mid)
    sell_vol = sum(t.quantity for t in recent if t.price < prev_mid)
    total = buy_vol + sell_vol
    return (buy_vol - sell_vol) / total if total else 0.0


def compute_ofi(order_depth: OrderDepth, trader_state: dict, product: str) -> float:
    """
    Best-level OFI, normalized by current total L1 size.
    Positive = net buy queue strengthening. Negative = net sell queue strengthening.
    Also updates prev_best_* keys in trader_state for next tick.
    """
    bb, ba = get_best_bid_ask(order_depth)
    if bb is None:
        return 0.0
    bid_sz = order_depth.buy_orders[bb]
    ask_sz = abs(order_depth.sell_orders[ba])

    pb = trader_state.get(f"{product}_prev_best_bid", bb)
    pa = trader_state.get(f"{product}_prev_best_ask", ba)
    pb_sz = trader_state.get(f"{product}_prev_best_bid_size", bid_sz)
    pa_sz = trader_state.get(f"{product}_prev_best_ask_size", ask_sz)

    # Bid contribution: price rose → new queue (positive), fell → old queue gone (negative)
    ofi_bid = bid_sz if bb > pb else (-pb_sz if bb < pb else bid_sz - pb_sz)
    # Ask contribution: price fell → new queue (positive buy pressure), rose → old gone
    ofi_ask = ask_sz if ba < pa else (-pa_sz if ba > pa else ask_sz - pa_sz)

    trader_state[f"{product}_prev_best_bid"] = bb
    trader_state[f"{product}_prev_best_ask"] = ba
    trader_state[f"{product}_prev_best_bid_size"] = bid_sz
    trader_state[f"{product}_prev_best_ask_size"] = ask_sz

    return (ofi_bid - ofi_ask) / max(bid_sz + ask_sz, 1)


def compute_tox_score(flow: float, ofi_norm: float, tox_weights: dict) -> float:
    """Combined toxicity: +1 = aggressive buy-side, -1 = aggressive sell-side."""
    return tox_weights["flow"] * flow + tox_weights["ofi"] * ofi_norm


def safe_passive_quotes(bid_raw: int, ask_raw: int, bb: int, ba: int) -> Tuple[int, int]:
    """
    Constrain to at most 1 tick inside current spread, then enforce bid < ask.
    Falls back to (best_bid, best_ask) if quotes would cross after constraining.
    """
    bid_px = min(bid_raw, bb + 1)
    ask_px = max(ask_raw, ba - 1)
    if bid_px >= ask_px:
        bid_px, ask_px = bb, ba
    return bid_px, ask_px


def build_shared_features(state: TradingState, trader_state: dict) -> dict:
    """Placeholder for future cross-product logic."""
    return {"timestamp": state.timestamp}


# ============================================================
# REGIME / STATE HELPERS
# ============================================================

def get_inventory_bucket(pos_sim: int, limit: int) -> str:
    """Five discrete inventory-pressure buckets."""
    ratio = abs(pos_sim) / max(limit, 1)
    if ratio < 0.2:  return "low"
    if ratio < 0.5:  return "medium"
    if ratio < 0.7:  return "high"
    if ratio < 0.9:  return "very_high"
    return "extreme"


def get_spread_bucket(spread: float, tight_thr: float, wide_thr: float) -> str:
    if spread <= tight_thr: return "tight"
    if spread >= wide_thr:  return "wide"
    return "normal"


def directional_conf(alpha_norm: float) -> float:
    """Maps alpha_norm to directional confidence in (0,1). 0.5 = neutral."""
    return 0.5 + 0.5 * math.tanh(alpha_norm * 2.0)


def classify_emeralds_regime(inv_bucket: str, spread_bucket: str) -> str:
    """
    Three EMERALDS execution modes:
    - inv_extreme   : only flatten, no new takes or passive adds
    - anchor_wide   : wide spread + healthy inventory → 2-level takes, L2 quotes
    - anchor_normal : standard 1-level take, L1+L2 passive
    """
    if inv_bucket == "extreme":
        return "inv_extreme"
    if spread_bucket == "wide" and inv_bucket in ("low", "medium"):
        return "anchor_wide"
    return "anchor_normal"


def classify_tomatoes_regime(
    alpha_norm: float,
    tox_score: float,
    spread_bucket: str,
    inv_bucket: str,
    pos_direction: int,
    params: dict,
) -> str:
    """
    10-state TOMATOES policy machine. Priority order (high → low):
    1. inv_extreme           → inv_long_stress / inv_short_stress
    2. severe toxicity       → severe_toxic_buy / severe_toxic_sell
    3. moderate toxicity     → toxic_buy / toxic_sell
    4. strong alpha          → attack_long / attack_short
    5. mild alpha            → bullish_mm / bearish_mm
    6. wide spread + neutral → wide_spread_opp
    7. default               → neutral_mm
    """
    if inv_bucket == "extreme":
        return "inv_long_stress" if pos_direction >= 0 else "inv_short_stress"

    if abs(tox_score) >= params["severe_tox_threshold"]:
        return "severe_toxic_buy" if tox_score > 0 else "severe_toxic_sell"

    if abs(tox_score) >= params["tox_threshold"]:
        return "toxic_buy" if tox_score > 0 else "toxic_sell"

    if alpha_norm >= params["attack_alpha_threshold"]:
        return "attack_long"
    if alpha_norm <= -params["attack_alpha_threshold"]:
        return "attack_short"

    if alpha_norm >= params["mild_alpha_threshold"]:
        return "bullish_mm"
    if alpha_norm <= -params["mild_alpha_threshold"]:
        return "bearish_mm"

    if spread_bucket == "wide" and inv_bucket in ("low", "medium"):
        return "wide_spread_opp"

    return "neutral_mm"


# ============================================================
# EMERALDS: Anchored Market Making with Two-Level Quoting
# ============================================================

def trade_emeralds(
    state: TradingState, shared: dict, params: dict, trader_state: dict
) -> List[Order]:
    product = "EMERALDS"
    od = state.order_depths[product]
    pos = state.position.get(product, 0)
    limit = params["position_limit"]

    bb, ba = get_best_bid_ask(od)
    if bb is None:
        return []

    mid = get_mid(od)
    micro = get_microprice(od)
    spread = get_spread(od) or 1.0

    # --- Fair value: 10000 anchor + optional microprice nudge ---
    fair = 10000.0
    if micro is not None and mid is not None and params["micro_beta"] > 0:
        fair += params["micro_beta"] * (micro - mid)
    fair_rounded = round(fair)

    # --- Returns-based vol (optional spread widening) ---
    prev_mid = trader_state.get(f"{product}_prev_mid", mid or fair)
    vol_adj = 0.0
    if params["enable_vol_adjust"] and params["vol_window"] > 0 and mid is not None:
        ret_hist: List[float] = trader_state.get(f"{product}_return_hist", [])
        ret_hist = clip_hist(ret_hist, mid - prev_mid, params["vol_window"])
        trader_state[f"{product}_return_hist"] = ret_hist
        if len(ret_hist) >= 2:
            vol_adj = round(params["vol_spread_multiplier"] * compute_rolling_std(ret_hist))

    # --- Pre-take regime classification ---
    inv_bucket = get_inventory_bucket(pos, limit)
    spread_bucket = get_spread_bucket(spread, params["tight_spread_threshold"], params["wide_spread_threshold"])
    regime = classify_emeralds_regime(inv_bucket, spread_bucket)

    orders: List[Order] = []
    pos_sim = pos

    # ----------------------------------------------------------------
    # STEP 1: Regime-aware take ladder (before passive recomputation)
    # anchor_wide → 2 levels; anchor_normal → 1 level; inv_extreme → 0
    # ----------------------------------------------------------------
    n_take = 0 if regime == "inv_extreme" else (2 if regime == "anchor_wide" else 1)
    take_edge = params["take_edge"]

    if n_take > 0:
        taken = 0
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price > fair_rounded - take_edge or taken >= n_take:
                break
            qty = min(abs(od.sell_orders[ask_price]), limit - pos_sim)
            if qty > 0:
                orders.append(Order(product, ask_price, qty))
                pos_sim += qty
                taken += 1

        taken = 0
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price < fair_rounded + take_edge or taken >= n_take:
                break
            qty = min(od.buy_orders[bid_price], limit + pos_sim)
            if qty > 0:
                orders.append(Order(product, bid_price, -qty))
                pos_sim -= qty
                taken += 1

    # ----------------------------------------------------------------
    # STEP 2: Recompute reservation + quotes using post-take pos_sim
    # ----------------------------------------------------------------
    inv_bucket_post = get_inventory_bucket(pos_sim, limit)
    reservation = fair - params["k_inv"] * (pos_sim / limit) * params["base_half_spread"]

    l1_hs = params["l1_half_spread"] + vol_adj
    l2_hs = params["base_half_spread"] + vol_adj

    # L1: queue-priority quote (at most 1 tick inside spread)
    bid_l1, ask_l1 = safe_passive_quotes(
        round(reservation - l1_hs), round(reservation + l1_hs), bb, ba
    )

    # L2: deeper quote (strictly worse than L1, enabled when spread not tight)
    enable_l2 = (
        params.get("enable_multi_level", True)
        and spread_bucket != "tight"
        and inv_bucket_post not in ("extreme",)
    )
    bid_l2 = ask_l2 = None
    if enable_l2:
        bid_l2 = min(round(reservation - l2_hs), bid_l1 - 1)
        ask_l2 = max(round(reservation + l2_hs), ask_l1 + 1)

    # ----------------------------------------------------------------
    # STEP 3: Explicit flatten at fair when extreme inventory
    # ----------------------------------------------------------------
    abs_pos = abs(pos_sim)
    if abs_pos >= int(0.9 * limit) and pos_sim != 0:
        fqty = min(params["flatten_size"], abs_pos)
        if pos_sim > 0:
            fqty = min(fqty, limit + pos_sim)
            if fqty > 0:
                orders.append(Order(product, fair_rounded, -fqty))
                pos_sim -= fqty
        else:
            fqty = min(fqty, limit - pos_sim)
            if fqty > 0:
                orders.append(Order(product, fair_rounded, fqty))
                pos_sim += fqty
        inv_bucket_post = get_inventory_bucket(pos_sim, limit)

    # ----------------------------------------------------------------
    # STEP 4: Inventory-bucket sizing for L1 and L2
    #
    # L1 (queue-priority): stays active on reducing side even when stressed.
    # L2 (deeper):         disabled on the inventory-adding side when high/above.
    # ----------------------------------------------------------------
    l1s = params["l1_size"]
    l2s = max(1, params["base_size"] - l1s)
    ib = inv_bucket_post
    ps = pos_sim  # post-flatten position

    if ib == "extreme":
        l1b, l1a = (0, l1s) if ps > 0 else (l1s, 0)
        l2b, l2a = 0, 0
    elif ib == "very_high":
        if ps > 0: l1b, l1a, l2b, l2a = round(l1s * 0.1), l1s, 0, l2s
        else:      l1b, l1a, l2b, l2a = l1s, round(l1s * 0.1), l2s, 0
    elif ib == "high":
        if ps > 0: l1b, l1a, l2b, l2a = round(l1s * 0.25), l1s, 0, l2s
        else:      l1b, l1a, l2b, l2a = l1s, round(l1s * 0.25), l2s, 0
    elif ib == "medium":
        if ps > 0: l1b, l1a, l2b, l2a = round(l1s * 0.5), l1s, round(l2s * 0.25), l2s
        else:      l1b, l1a, l2b, l2a = l1s, round(l1s * 0.5), l2s, round(l2s * 0.25)
    else:  # low
        l1b = l1a = l1s
        l2b = l2a = l2s

    # Emit L1
    buy_rem, sell_rem = limit - pos_sim, limit + pos_sim
    l1b = max(0, min(l1b, buy_rem))
    l1a = max(0, min(l1a, sell_rem))
    if l1b > 0:
        orders.append(Order(product, bid_l1, l1b))
        buy_rem -= l1b
    if l1a > 0:
        orders.append(Order(product, ask_l1, -l1a))
        sell_rem -= l1a

    # Emit L2 (if enabled and not duplicate price)
    if enable_l2 and bid_l2 is not None:
        l2b = max(0, min(l2b, buy_rem))
        l2a = max(0, min(l2a, sell_rem))
        if l2b > 0:
            orders.append(Order(product, bid_l2, l2b))
        if l2a > 0:
            orders.append(Order(product, ask_l2, -l2a))

    # --- Diagnostics ---
    trader_state[f"{product}_prev_mid"] = mid or fair
    trader_state[f"{product}_last_quote_bid"] = bid_l1
    trader_state[f"{product}_last_quote_ask"] = ask_l1
    trader_state[f"{product}_last_fair"] = fair
    trader_state[f"{product}_last_reservation"] = reservation
    trader_state[f"{product}_last_regime"] = regime
    trader_state[f"{product}_last_inv_bucket"] = ib
    trader_state[f"{product}_last_spread_bucket"] = spread_bucket

    return orders


# ============================================================
# TOMATOES: State-Machine Alpha-Driven Market Making
# ============================================================

def trade_tomatoes(
    state: TradingState, shared: dict, params: dict, trader_state: dict
) -> List[Order]:
    product = "TOMATOES"
    od = state.order_depths[product]
    pos = state.position.get(product, 0)
    limit = params["position_limit"]

    bb, ba = get_best_bid_ask(od)
    if bb is None:
        return []

    mid = get_mid(od)
    spread = get_spread(od) or 1.0
    wall = get_wall_mid(od)
    vwap = get_vwap_mid(od)
    micro = get_microprice(od)
    imbalance = get_l1_imbalance(od)

    # --- Composite fair value ---
    fw = params["fair_weights"]
    fair_sum = weight_sum = 0.0
    for val, key in [(wall, "wall_mid"), (vwap, "vwap_mid"), (micro, "microprice")]:
        if val is not None:
            fair_sum += fw[key] * val
            weight_sum += fw[key]
    if weight_sum == 0:
        return []
    fair = fair_sum / weight_sum

    # Momentum correction: nudge fair slightly in direction of recent price change
    prev_mid = trader_state.get(f"{product}_prev_mid", mid or fair)
    if mid is not None:
        fair += params["gamma_return_fair"] * (mid - prev_mid)
    fair_rounded = round(fair)

    # --- Alpha signals ---
    # micro_dev normalized by spread → comparable scale to imbalance/flow in [-0.5, 0.5]
    micro_dev_norm = ((micro - mid) / max(spread, 1.0)) if (micro is not None and mid is not None) else 0.0
    flow = compute_trade_flow(product, state, prev_mid, params["flow_window"])
    ofi_norm = compute_ofi(od, trader_state, product)

    aw = params["alpha_weights"]
    alpha = (
        aw["micro_dev"] * micro_dev_norm
        + aw["imbalance"] * imbalance
        + aw["flow"] * flow
        + aw["ofi"] * ofi_norm
    )
    # Smooth normalization: α/(1+|α|) maps ℝ → (-1,1) without discontinuous clamp
    alpha_norm = alpha / (1.0 + abs(alpha))
    dir_conf = directional_conf(alpha_norm)  # (0,1): > 0.5 = bullish

    # --- Returns-based volatility ---
    vol_adj = 0.0
    if params["vol_window"] > 0 and mid is not None:
        ret_hist: List[float] = trader_state.get(f"{product}_return_hist", [])
        ret_hist = clip_hist(ret_hist, mid - prev_mid, params["vol_window"])
        trader_state[f"{product}_return_hist"] = ret_hist
        if len(ret_hist) >= 2:
            vol_adj = params["vol_spread_multiplier"] * compute_rolling_std(ret_hist)

    # --- Toxicity score ---
    tox_score = compute_tox_score(flow, ofi_norm, params["tox_weights"])

    # --- Classify regime (state machine) ---
    inv_bucket = get_inventory_bucket(pos, limit)
    spread_bucket = get_spread_bucket(spread, params["tight_spread_threshold"], params["wide_spread_threshold"])
    pos_dir = 1 if pos > 0 else (-1 if pos < 0 else 0)
    regime = classify_tomatoes_regime(alpha_norm, tox_score, spread_bucket, inv_bucket, pos_dir, params)

    orders: List[Order] = []
    pos_sim = pos

    # ----------------------------------------------------------------
    # STEP 1: State-gated take ladder
    # Only directional regimes get take permissions; toxic/stress/neutral → 0
    # ----------------------------------------------------------------
    n_buy = n_sell = 0
    lvls = params["take_levels"]
    if regime == "attack_long":
        n_buy = lvls["attack"]
    elif regime == "bullish_mm":
        n_buy = lvls["mild"]
    elif regime == "attack_short":
        n_sell = lvls["attack"]
    elif regime == "bearish_mm":
        n_sell = lvls["mild"]

    take_edge = params["take_edge"]
    if n_buy > 0:
        taken = 0
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price > fair_rounded - take_edge or taken >= n_buy:
                break
            qty = min(abs(od.sell_orders[ask_price]), limit - pos_sim)
            if qty > 0:
                orders.append(Order(product, ask_price, qty))
                pos_sim += qty
                taken += 1

    if n_sell > 0:
        taken = 0
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price < fair_rounded + take_edge or taken >= n_sell:
                break
            qty = min(od.buy_orders[bid_price], limit + pos_sim)
            if qty > 0:
                orders.append(Order(product, bid_price, -qty))
                pos_sim -= qty
                taken += 1

    # ----------------------------------------------------------------
    # STEP 2: Recompute reservation + edges using post-take position
    # ----------------------------------------------------------------
    inv_bucket_post = get_inventory_bucket(pos_sim, limit)
    reservation = fair - params["k_inv"] * (pos_sim / limit) * params["base_half_spread"]

    # Tiered toxicity spread widening (3 tiers: none / moderate / severe)
    if regime.startswith("severe_toxic"):
        tox_adj = float(params["tox_spread_add"] * 3)
    elif regime.startswith("toxic"):
        tox_adj = float(params["tox_spread_add"] * 2)
    else:
        tox_adj = 0.0

    # Asymmetric edges: bullish → bid closer (smaller bid_edge), ask further
    base_edge = params["base_half_spread"] + vol_adj + tox_adj
    c = params["alpha_edge_scale"]
    bid_edge = max(base_edge - c * max(alpha_norm, 0.0) + c * max(-alpha_norm, 0.0), 1.0)
    ask_edge = max(base_edge + c * max(alpha_norm, 0.0) - c * max(-alpha_norm, 0.0), 1.0)

    # L1: book-constrained + crossing-safe
    bid_l1, ask_l1 = safe_passive_quotes(
        round(reservation - bid_edge), round(reservation + ask_edge), bb, ba
    )

    # L2: enabled in passive/neutral regimes with room in the spread
    enable_l2 = (
        regime in ("neutral_mm", "wide_spread_opp", "bullish_mm", "bearish_mm")
        and spread_bucket in ("normal", "wide")
        and inv_bucket_post not in ("very_high", "extreme")
    )
    bid_l2 = ask_l2 = None
    if enable_l2:
        l2m = params["l2_spread_mult"]
        bid_l2 = min(round(reservation - bid_edge * l2m), bid_l1 - 1)
        ask_l2 = max(round(reservation + ask_edge * l2m), ask_l1 + 1)

    # ----------------------------------------------------------------
    # STEP 3: Flatten extreme inventory
    # ----------------------------------------------------------------
    abs_pos = abs(pos_sim)
    if abs_pos >= int(0.9 * limit) and pos_sim != 0:
        fqty = min(params["flatten_size"], abs_pos)
        if pos_sim > 0:
            fqty = min(fqty, limit + pos_sim)
            if fqty > 0:
                orders.append(Order(product, fair_rounded, -fqty))
                pos_sim -= fqty
        else:
            fqty = min(fqty, limit - pos_sim)
            if fqty > 0:
                orders.append(Order(product, fair_rounded, fqty))
                pos_sim += fqty
        inv_bucket_post = get_inventory_bucket(pos_sim, limit)

    # ----------------------------------------------------------------
    # STEP 4: Regime + inventory-bucket passive sizing
    #
    # Layer 1: regime sets base buy/sell quantities (suppression / enhancement)
    # Layer 2: alpha_norm skews buy vs sell (skipped for stress/toxic regimes)
    # Layer 3: inventory bucket applies residual one-sided reduction
    # ----------------------------------------------------------------
    base_size = float(params["base_size"])

    # Layer 1: regime-based size policy
    if regime == "inv_long_stress":
        base_buy, base_sell = 0.0, base_size * 1.5     # only sell to unwind
    elif regime == "inv_short_stress":
        base_buy, base_sell = base_size * 1.5, 0.0     # only buy to unwind
    elif regime == "severe_toxic_buy":
        base_buy, base_sell = base_size, 0.0            # suppress asks entirely
    elif regime == "severe_toxic_sell":
        base_buy, base_sell = 0.0, base_size
    elif regime == "toxic_buy":
        base_buy, base_sell = base_size, base_size * 0.25   # 75% ask reduction
    elif regime == "toxic_sell":
        base_buy, base_sell = base_size * 0.25, base_size
    else:
        base_buy = base_sell = base_size

    # Layer 2: alpha skew (not applied in stress/toxic regimes)
    neutral_regime = regime not in (
        "inv_long_stress", "inv_short_stress",
        "severe_toxic_buy", "severe_toxic_sell",
    )
    sc = params["alpha_size_scale"]
    if neutral_regime:
        buy_size = base_buy * (1.0 + sc * alpha_norm)
        sell_size = base_sell * (1.0 - sc * alpha_norm)
    else:
        buy_size, sell_size = base_buy, base_sell

    # Layer 3: inventory bucket residual adjustment (not for stress regimes)
    ib = inv_bucket_post
    ps = pos_sim
    if regime not in ("inv_long_stress", "inv_short_stress"):
        if ib in ("extreme", "very_high"):
            if ps > 0: buy_size = 0.0
            else:      sell_size = 0.0
        elif ib == "high":
            if ps > 0: buy_size *= 0.25
            else:      sell_size *= 0.25
        elif ib == "medium":
            if ps > 0: buy_size *= 0.6
            else:      sell_size *= 0.6

    # Emit L1
    buy_rem, sell_rem = limit - pos_sim, limit + pos_sim
    l1b = max(0, min(round(buy_size), buy_rem))
    l1a = max(0, min(round(sell_size), sell_rem))
    if l1b > 0:
        orders.append(Order(product, bid_l1, l1b))
        buy_rem -= l1b
    if l1a > 0:
        orders.append(Order(product, ask_l1, -l1a))
        sell_rem -= l1a

    # Emit L2 (same suppression logic, fixed fraction of base_size)
    if enable_l2 and bid_l2 is not None:
        l2_frac = params["l2_size_fraction"]
        l2b_raw = round(base_size * l2_frac)
        l2a_raw = round(base_size * l2_frac)
        # Mirror regime suppressions to L2
        if regime in ("inv_long_stress", "severe_toxic_buy"):   l2b_raw = 0
        if regime in ("inv_short_stress", "severe_toxic_sell"):  l2a_raw = 0
        if ib in ("extreme", "very_high") and ps > 0:  l2b_raw = 0
        if ib in ("extreme", "very_high") and ps < 0:  l2a_raw = 0
        if ib == "high" and ps > 0:  l2b_raw = 0
        if ib == "high" and ps < 0:  l2a_raw = 0
        l2b = max(0, min(l2b_raw, buy_rem))
        l2a = max(0, min(l2a_raw, sell_rem))
        if l2b > 0:
            orders.append(Order(product, bid_l2, l2b))
        if l2a > 0:
            orders.append(Order(product, ask_l2, -l2a))

    # --- Diagnostics ---
    trader_state[f"{product}_prev_mid"] = mid or fair
    trader_state[f"{product}_last_quote_bid"] = bid_l1
    trader_state[f"{product}_last_quote_ask"] = ask_l1
    trader_state[f"{product}_last_fair"] = fair
    trader_state[f"{product}_last_reservation"] = reservation
    trader_state[f"{product}_last_alpha"] = alpha_norm
    trader_state[f"{product}_last_dir_conf"] = dir_conf
    trader_state[f"{product}_last_tox_score"] = tox_score
    trader_state[f"{product}_last_regime"] = regime
    trader_state[f"{product}_last_inv_bucket"] = ib
    trader_state[f"{product}_last_spread_bucket"] = spread_bucket

    return orders


# ============================================================
# MAIN TRADER
# ============================================================

class Trader:
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        trader_state = json.loads(state.traderData) if state.traderData else {}
        shared = build_shared_features(state, trader_state)

        orders: Dict[str, List[Order]] = {}
        conversions = 0

        if "EMERALDS" in state.order_depths:
            orders["EMERALDS"] = trade_emeralds(
                state, shared, PARAMS["EMERALDS"], trader_state
            )
        if "TOMATOES" in state.order_depths:
            orders["TOMATOES"] = trade_tomatoes(
                state, shared, PARAMS["TOMATOES"], trader_state
            )

        return orders, conversions, json.dumps(trader_state)
