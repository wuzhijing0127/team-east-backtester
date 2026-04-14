"""
Adaptive Market Making Strategy — EMERALDS + TOMATOES
======================================================
EMERALDS: anchored market making (fair=10000, optional microprice nudge)
TOMATOES: composite-fair + multi-signal alpha-driven adaptive market making

Key design decisions:
- Passive quotes are recomputed AFTER aggressive takes so reservation reflects
  post-take inventory.
- TOMATOES aggressive takes are gated by alpha_norm direction.
- micro_dev is spread-normalized so all alpha components are comparably scaled.
- alpha is soft-normalized (alpha / (1+|alpha|)) before use in size skew.
- Returns-based (not level-based) rolling std drives volatility adjustment.
- OFI (order flow imbalance) supplements flow for a more robust toxicity signal.
- Extreme inventory (>= 90% of limit) triggers an explicit flatten order at fair.
- TOMATOES passive quotes are book-constrained and crossing-safe.
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
        # Fair value
        "micro_beta": 0.0,              # 0.0 = pure 10000 anchor; raise to 0.1-0.2 to enable microprice nudge
        # Inventory / quoting
        "k_inv": 2.5,                   # reservation skew: reservation = fair - k_inv*(pos/limit)*base_hs
        "base_half_spread": 5,          # half-spread in ticks
        "take_edge": 2,                 # min edge (ticks) for aggressive takes: take if ask <= fair - edge
        "base_size": 10,                # base passive order size
        # Volatility adjustment (disabled by default for EMERALDS)
        "vol_window": 12,               # rolling window for returns-based vol estimate
        "enable_vol_adjust": False,     # set True to widen spread proportionally to vol
        "vol_spread_multiplier": 0.3,   # extra half-spread ticks per unit of returns std
        # Extreme inventory
        "flatten_size": 5,              # units to place at fair when |pos| >= 90% of limit
    },
    "TOMATOES": {
        "position_limit": 50,
        # Composite fair value
        "fair_weights": {
            "wall_mid":   0.5,          # deepest bid/ask wall midpoint
            "vwap_mid":   0.3,          # volume-weighted midpoint
            "microprice": 0.2,          # L1-size-weighted midpoint
        },
        # Alpha signals
        "alpha_weights": {
            "micro_dev":  1.0,          # spread-normalized microprice deviation
            "imbalance":  0.5,          # L1 book imbalance (bid_size - ask_size) / total
            "flow":       0.5,          # recent trade flow imbalance
            "ofi":        0.3,          # best-level order flow imbalance (dynamic)
        },
        "gamma_return_fair": 0.05,      # momentum correction: fair += gamma * (mid - prev_mid)
        # Inventory / quoting
        "k_inv": 3.0,
        "base_half_spread": 4,
        "take_edge": 3,                 # higher than EMERALDS: only take on clear edge + alpha
        "alpha_take_threshold": 0.1,    # min |alpha_norm| to allow an alpha-gated aggressive take
        "base_size": 8,
        # Volatility adjustment
        "vol_window": 20,
        "vol_spread_multiplier": 0.5,
        # Trade flow toxicity
        "flow_window": 10,              # recent trades to classify for flow
        "tox_threshold": 0.5,           # |tox_score| above this triggers toxicity response
        "tox_spread_add": 1,            # extra half-spread ticks when toxic
        "tox_weights": {
            "flow": 0.6,                # weight of trade-flow in toxicity score
            "ofi":  0.4,                # weight of OFI in toxicity score
        },
        # Asymmetric edge / size skew
        "alpha_edge_scale": 0.5,        # how much alpha_norm shifts bid/ask edge asymmetrically
        "alpha_size_scale": 0.3,        # how much alpha_norm skews passive buy vs sell size
        # Extreme inventory
        "flatten_size": 5,
    },
}


# ============================================================
# SHARED HELPER FUNCTIONS
# ============================================================

def get_best_bid_ask(order_depth: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    if not order_depth.buy_orders or not order_depth.sell_orders:
        return None, None
    return max(order_depth.buy_orders), min(order_depth.sell_orders)


def get_mid(order_depth: OrderDepth) -> Optional[float]:
    best_bid, best_ask = get_best_bid_ask(order_depth)
    if best_bid is None or best_ask is None:
        return None
    return (best_bid + best_ask) / 2


def get_spread(order_depth: OrderDepth) -> Optional[float]:
    best_bid, best_ask = get_best_bid_ask(order_depth)
    if best_bid is None or best_ask is None:
        return None
    return float(best_ask - best_bid)


def get_wall_mid(order_depth: OrderDepth) -> Optional[float]:
    """Mid between the deepest-volume bid wall and deepest-volume ask wall."""
    if not order_depth.buy_orders or not order_depth.sell_orders:
        return None
    bid_wall = max(order_depth.buy_orders.keys(), key=lambda p: order_depth.buy_orders[p])
    ask_wall = max(order_depth.sell_orders.keys(), key=lambda p: abs(order_depth.sell_orders[p]))
    return (bid_wall + ask_wall) / 2


def get_vwap_mid(order_depth: OrderDepth) -> Optional[float]:
    if not order_depth.buy_orders or not order_depth.sell_orders:
        return None
    bid_vwap = (
        sum(p * v for p, v in order_depth.buy_orders.items())
        / sum(order_depth.buy_orders.values())
    )
    ask_vwap = (
        sum(p * abs(v) for p, v in order_depth.sell_orders.items())
        / sum(abs(v) for v in order_depth.sell_orders.values())
    )
    return (bid_vwap + ask_vwap) / 2


def get_microprice(order_depth: OrderDepth) -> Optional[float]:
    """L1 microprice: pressure-weighted mid = (ask*bid_sz + bid*ask_sz) / total_sz."""
    best_bid, best_ask = get_best_bid_ask(order_depth)
    if best_bid is None or best_ask is None:
        return None
    bid_size = order_depth.buy_orders[best_bid]
    ask_size = abs(order_depth.sell_orders[best_ask])
    total = bid_size + ask_size
    if total == 0:
        return (best_bid + best_ask) / 2
    return (best_ask * bid_size + best_bid * ask_size) / total


def get_l1_imbalance(order_depth: OrderDepth) -> float:
    """L1 imbalance in [-1, 1]: +1 = all volume on bid side (buy pressure)."""
    best_bid, best_ask = get_best_bid_ask(order_depth)
    if best_bid is None or best_ask is None:
        return 0.0
    bid_size = order_depth.buy_orders[best_bid]
    ask_size = abs(order_depth.sell_orders[best_ask])
    total = bid_size + ask_size
    if total == 0:
        return 0.0
    return (bid_size - ask_size) / total


def clip_hist(hist: List[float], value: float, window: int) -> List[float]:
    """Append value to rolling history and trim to window length."""
    hist.append(value)
    return hist[-window:] if len(hist) > window else hist


def compute_rolling_std(hist: List[float]) -> float:
    """Population std of a history list. Returns 0.0 if fewer than 2 elements."""
    if len(hist) < 2:
        return 0.0
    mean = sum(hist) / len(hist)
    variance = sum((x - mean) ** 2 for x in hist) / len(hist)
    return math.sqrt(variance)


def compute_trade_flow(
    product: str, state: TradingState, prev_mid: float, flow_window: int
) -> float:
    """
    Trade flow imbalance in [-1, 1].
    +1 = all recent trades were aggressive buys (lifted the ask).
    -1 = all recent trades were aggressive sells (hit the bid).
    Classify by comparing trade price to prev_mid.
    """
    recent = state.market_trades.get(product, [])[-flow_window:]
    if not recent:
        return 0.0
    buy_vol = sum(t.quantity for t in recent if t.price >= prev_mid)
    sell_vol = sum(t.quantity for t in recent if t.price < prev_mid)
    total = buy_vol + sell_vol
    if total == 0:
        return 0.0
    return (buy_vol - sell_vol) / total


def compute_ofi(
    order_depth: OrderDepth, trader_state: dict, product: str
) -> float:
    """
    Best-level Order Flow Imbalance (OFI), normalized by current total best-level size.

    OFI tracks whether the best bid/ask queues are strengthening or weakening:
    - Bid price rose   → new buy queue appeared above  → positive bid contribution
    - Bid price fell   → old buy queue disappeared     → negative bid contribution
    - Bid price same   → net change in bid queue size
    - Ask side mirrors (lower ask = more aggressive selling pressure = positive ask contrib)

    Net OFI = bid_contribution - ask_contribution:  positive = net buy pressure.
    Normalized to roughly [-2, 2] by total best-level volume; in practice usually [-1, 1].
    """
    best_bid, best_ask = get_best_bid_ask(order_depth)
    if best_bid is None or best_ask is None:
        # Update state with no-data defaults and return 0
        return 0.0

    bid_size = order_depth.buy_orders[best_bid]
    ask_size = abs(order_depth.sell_orders[best_ask])

    prev_bid = trader_state.get(f"{product}_prev_best_bid", best_bid)
    prev_ask = trader_state.get(f"{product}_prev_best_ask", best_ask)
    prev_bid_sz = trader_state.get(f"{product}_prev_best_bid_size", bid_size)
    prev_ask_sz = trader_state.get(f"{product}_prev_best_ask_size", ask_size)

    # Bid side contribution
    if best_bid > prev_bid:
        ofi_bid = bid_size          # new, higher best bid queue
    elif best_bid < prev_bid:
        ofi_bid = -prev_bid_sz      # previous best bid queue disappeared
    else:
        ofi_bid = bid_size - prev_bid_sz  # same price, queue changed size

    # Ask side contribution (lower ask = increased sell pressure = acts like positive buy-side OFI negatively)
    if best_ask < prev_ask:
        ofi_ask = ask_size          # new, lower best ask queue
    elif best_ask > prev_ask:
        ofi_ask = -prev_ask_sz      # previous best ask queue disappeared
    else:
        ofi_ask = ask_size - prev_ask_sz

    raw_ofi = ofi_bid - ofi_ask
    ofi_norm = raw_ofi / max(bid_size + ask_size, 1)

    # Persist best-level snapshot for next tick
    trader_state[f"{product}_prev_best_bid"] = best_bid
    trader_state[f"{product}_prev_best_ask"] = best_ask
    trader_state[f"{product}_prev_best_bid_size"] = bid_size
    trader_state[f"{product}_prev_best_ask_size"] = ask_size

    return ofi_norm


def compute_tox_score(flow: float, ofi_norm: float, tox_weights: dict) -> float:
    """
    Combined toxicity score (approximately [-1, 1]).
    Positive = aggressive buy pressure  → our passive asks are at adverse-selection risk.
    Negative = aggressive sell pressure → our passive bids are at adverse-selection risk.
    """
    return tox_weights["flow"] * flow + tox_weights["ofi"] * ofi_norm


def safe_passive_quotes(
    bid_raw: int, ask_raw: int, best_bid: int, best_ask: int
) -> Tuple[int, int]:
    """
    1. Constrain passive quotes to at most 1 tick inside the current spread.
    2. Ensure bid_px < ask_px (no accidental crossing / marketable passive orders).
    Falls back to quoting just outside the current spread if a crossing would occur.
    """
    bid_px = min(bid_raw, best_bid + 1)
    ask_px = max(ask_raw, best_ask - 1)
    if bid_px >= ask_px:
        # Quotes crossed after book constraint — fall back to just outside spread
        bid_px = best_bid
        ask_px = best_ask
    return bid_px, ask_px


def build_shared_features(state: TradingState, trader_state: dict) -> dict:
    """Shared cross-product features. Extend here for future cross-product logic."""
    return {"timestamp": state.timestamp}


# ============================================================
# EMERALDS: Anchored Market Making
# ============================================================

def trade_emeralds(
    state: TradingState,
    shared: dict,
    params: dict,
    trader_state: dict,
) -> List[Order]:
    product = "EMERALDS"
    od = state.order_depths[product]
    pos = state.position.get(product, 0)
    limit = params["position_limit"]

    best_bid, best_ask = get_best_bid_ask(od)
    if best_bid is None or best_ask is None:
        return []

    mid = get_mid(od)
    micro = get_microprice(od)

    # --- Fair value: static 10000 anchor + optional tiny microprice correction ---
    # micro_beta = 0.0 (default) means pure anchor. Set to 0.1-0.2 for a small book nudge.
    fair = 10000.0
    if micro is not None and mid is not None and params["micro_beta"] > 0:
        fair += params["micro_beta"] * (micro - mid)
    fair_rounded = round(fair)

    # --- Returns-based volatility (used only when enable_vol_adjust = True) ---
    # Using price changes, not price levels, so std(returns) is stationary.
    prev_mid = trader_state.get(f"{product}_prev_mid", mid if mid is not None else fair)
    vol_adj = 0.0
    if params["enable_vol_adjust"] and params["vol_window"] > 0 and mid is not None:
        ret = mid - prev_mid
        ret_hist: List[float] = trader_state.get(f"{product}_return_hist", [])
        ret_hist = clip_hist(ret_hist, ret, params["vol_window"])
        trader_state[f"{product}_return_hist"] = ret_hist
        if len(ret_hist) >= 2:
            vol_adj = round(params["vol_spread_multiplier"] * compute_rolling_std(ret_hist))

    orders: List[Order] = []
    pos_sim = pos  # simulated position, updated as we generate orders

    # ----------------------------------------------------------------
    # STEP 1: Aggressive takes
    # Run takes first so reservation / passive quotes reflect post-take inventory.
    # ----------------------------------------------------------------
    take_edge = params["take_edge"]

    for ask_price in sorted(od.sell_orders.keys()):
        if ask_price > fair_rounded - take_edge:
            break
        qty = min(abs(od.sell_orders[ask_price]), limit - pos_sim)
        if qty > 0:
            orders.append(Order(product, ask_price, qty))
            pos_sim += qty

    for bid_price in sorted(od.buy_orders.keys(), reverse=True):
        if bid_price < fair_rounded + take_edge:
            break
        qty = min(od.buy_orders[bid_price], limit + pos_sim)
        if qty > 0:
            orders.append(Order(product, bid_price, -qty))
            pos_sim -= qty

    # ----------------------------------------------------------------
    # STEP 2: Recompute reservation and quotes using post-take position
    # ----------------------------------------------------------------
    # reservation skews our quoted mid against current inventory to mean-revert.
    reservation = fair - params["k_inv"] * (pos_sim / limit) * params["base_half_spread"]
    half_spread = params["base_half_spread"] + vol_adj

    bid_px_raw = round(reservation - half_spread)
    ask_px_raw = round(reservation + half_spread)
    bid_px, ask_px = safe_passive_quotes(bid_px_raw, ask_px_raw, best_bid, best_ask)

    # ----------------------------------------------------------------
    # STEP 3: Explicit inventory flatten when at/near position limit
    # Supplements passive quoting with a direct order at fair_rounded.
    # ----------------------------------------------------------------
    abs_pos_sim = abs(pos_sim)
    if abs_pos_sim >= int(0.9 * limit) and pos_sim != 0:
        flatten_qty = min(params["flatten_size"], abs_pos_sim)
        if pos_sim > 0:
            # Long: place a sell at fair to actively offload inventory
            flatten_qty = min(flatten_qty, limit + pos_sim)
            if flatten_qty > 0:
                orders.append(Order(product, fair_rounded, -flatten_qty))
                pos_sim -= flatten_qty
        else:
            # Short: place a buy at fair
            flatten_qty = min(flatten_qty, limit - pos_sim)
            if flatten_qty > 0:
                orders.append(Order(product, fair_rounded, flatten_qty))
                pos_sim += flatten_qty
        abs_pos_sim = abs(pos_sim)  # recalc for sizing below

    # ----------------------------------------------------------------
    # STEP 4: Passive sizes — three-tier inventory control
    # ----------------------------------------------------------------
    buy_size = params["base_size"]
    sell_size = params["base_size"]

    if abs_pos_sim >= int(0.9 * limit):
        # Extreme: only quote the reducing side
        if pos_sim > 0:
            buy_size = 0
        else:
            sell_size = 0
    elif abs_pos_sim >= int(0.7 * limit):
        # Elevated: aggressively reduce the inventory-adding side
        if pos_sim > 0:
            buy_size = round(buy_size * 0.25)
        else:
            sell_size = round(sell_size * 0.25)
    else:
        # Normal: halve the inventory-adding side
        if pos_sim > 0:
            buy_size = round(buy_size * 0.5)
        elif pos_sim < 0:
            sell_size = round(sell_size * 0.5)

    buy_size = max(0, min(buy_size, limit - pos_sim))
    sell_size = max(0, min(sell_size, limit + pos_sim))

    if buy_size > 0:
        orders.append(Order(product, bid_px, buy_size))
    if sell_size > 0:
        orders.append(Order(product, ask_px, -sell_size))

    # ----------------------------------------------------------------
    # Diagnostics — stored for offline markout analysis
    # ----------------------------------------------------------------
    trader_state[f"{product}_prev_mid"] = mid if mid is not None else fair
    trader_state[f"{product}_last_quote_bid"] = bid_px
    trader_state[f"{product}_last_quote_ask"] = ask_px
    trader_state[f"{product}_last_fair"] = fair
    trader_state[f"{product}_last_reservation"] = reservation

    return orders


# ============================================================
# TOMATOES: Order-Book Alpha-Driven Adaptive MM
# ============================================================

def trade_tomatoes(
    state: TradingState,
    shared: dict,
    params: dict,
    trader_state: dict,
) -> List[Order]:
    product = "TOMATOES"
    od = state.order_depths[product]
    pos = state.position.get(product, 0)
    limit = params["position_limit"]

    best_bid, best_ask = get_best_bid_ask(od)
    if best_bid is None or best_ask is None:
        return []

    mid = get_mid(od)
    spread = get_spread(od) or 1.0
    wall = get_wall_mid(od)
    vwap = get_vwap_mid(od)
    micro = get_microprice(od)
    imbalance = get_l1_imbalance(od)

    # --- Composite fair value: weighted average of available components ---
    # Weights normalize automatically if a component is unavailable.
    fw = params["fair_weights"]
    fair_sum, weight_sum = 0.0, 0.0
    for val, key in [(wall, "wall_mid"), (vwap, "vwap_mid"), (micro, "microprice")]:
        if val is not None:
            fair_sum += fw[key] * val
            weight_sum += fw[key]
    if weight_sum == 0:
        return []
    fair = fair_sum / weight_sum

    # --- Short-term momentum correction to fair ---
    # Tilts fair slightly toward the direction of recent price change.
    prev_mid = trader_state.get(f"{product}_prev_mid", mid if mid is not None else fair)
    if mid is not None:
        recent_return = mid - prev_mid
        fair += params["gamma_return_fair"] * recent_return

    fair_rounded = round(fair)

    # --- Alpha signals ---
    # micro_dev normalized by spread so it's dimensionally consistent with imbalance/flow in [-0.5, 0.5].
    micro_dev_norm = (
        (micro - mid) / max(spread, 1.0)
        if (micro is not None and mid is not None)
        else 0.0
    )
    flow = compute_trade_flow(product, state, prev_mid, params["flow_window"])
    ofi_norm = compute_ofi(od, trader_state, product)  # also updates prev_best_* in trader_state

    aw = params["alpha_weights"]
    alpha = (
        aw["micro_dev"] * micro_dev_norm
        + aw["imbalance"] * imbalance
        + aw["flow"] * flow
        + aw["ofi"] * ofi_norm
    )
    # Soft normalization: maps alpha from ℝ into (-1, 1) smoothly, avoids clamp discontinuity.
    alpha_norm = alpha / (1.0 + abs(alpha))

    # --- Returns-based volatility ---
    # std(returns) is stationary; std(levels) drifts with price and is not useful for spread sizing.
    vol_adj = 0.0
    if params["vol_window"] > 0 and mid is not None:
        ret = mid - prev_mid
        ret_hist: List[float] = trader_state.get(f"{product}_return_hist", [])
        ret_hist = clip_hist(ret_hist, ret, params["vol_window"])
        trader_state[f"{product}_return_hist"] = ret_hist
        if len(ret_hist) >= 2:
            # Not rounded: vol_adj is a continuous float added to bid/ask edge floats
            vol_adj = params["vol_spread_multiplier"] * compute_rolling_std(ret_hist)

    # --- Toxicity score: combined flow + OFI ---
    tox_score = compute_tox_score(flow, ofi_norm, params["tox_weights"])
    toxic_buy = tox_score > params["tox_threshold"]    # buy-side pressure: passive asks at risk
    toxic_sell = tox_score < -params["tox_threshold"]  # sell-side pressure: passive bids at risk
    tox_adj = float(params["tox_spread_add"]) if (toxic_buy or toxic_sell) else 0.0

    orders: List[Order] = []
    pos_sim = pos

    # ----------------------------------------------------------------
    # STEP 1: Alpha-gated aggressive takes
    # Only take when alpha direction agrees with the take direction.
    # This prevents TOMATOES from fighting against its own alpha signal.
    # ----------------------------------------------------------------
    take_edge = params["take_edge"]
    alpha_thresh = params["alpha_take_threshold"]
    # Precompute gates: alpha must confirm the direction to a minimum threshold
    can_buy_agg = alpha_norm > alpha_thresh      # bullish signal → OK to lift asks
    can_sell_agg = alpha_norm < -alpha_thresh    # bearish signal → OK to hit bids

    for ask_price in sorted(od.sell_orders.keys()):
        if ask_price > fair_rounded - take_edge:
            break
        if not can_buy_agg:
            break
        qty = min(abs(od.sell_orders[ask_price]), limit - pos_sim)
        if qty > 0:
            orders.append(Order(product, ask_price, qty))
            pos_sim += qty

    for bid_price in sorted(od.buy_orders.keys(), reverse=True):
        if bid_price < fair_rounded + take_edge:
            break
        if not can_sell_agg:
            break
        qty = min(od.buy_orders[bid_price], limit + pos_sim)
        if qty > 0:
            orders.append(Order(product, bid_price, -qty))
            pos_sim -= qty

    # ----------------------------------------------------------------
    # STEP 2: Recompute reservation and asymmetric edges using post-take position
    # ----------------------------------------------------------------
    reservation = fair - params["k_inv"] * (pos_sim / limit) * params["base_half_spread"]

    # Asymmetric edge construction:
    # alpha_norm > 0 (bullish) → bid edge smaller (quote closer) + ask edge larger (quote further)
    # alpha_norm < 0 (bearish) → ask edge smaller + bid edge larger
    base_edge = params["base_half_spread"] + vol_adj + tox_adj
    c = params["alpha_edge_scale"]
    bid_edge = base_edge - c * max(alpha_norm, 0.0) + c * max(-alpha_norm, 0.0)
    ask_edge = base_edge + c * max(alpha_norm, 0.0) - c * max(-alpha_norm, 0.0)
    bid_edge = max(bid_edge, 1.0)  # floor: never quote less than 1 tick from reservation
    ask_edge = max(ask_edge, 1.0)

    bid_px_raw = round(reservation - bid_edge)
    ask_px_raw = round(reservation + ask_edge)
    # Book-constrained + crossing-safe (TOMATOES-specific — EMERALDS already had this)
    bid_px, ask_px = safe_passive_quotes(bid_px_raw, ask_px_raw, best_bid, best_ask)

    # ----------------------------------------------------------------
    # STEP 3: Explicit inventory flatten when at/near position limit
    # ----------------------------------------------------------------
    abs_pos_sim = abs(pos_sim)
    if abs_pos_sim >= int(0.9 * limit) and pos_sim != 0:
        flatten_qty = min(params["flatten_size"], abs_pos_sim)
        if pos_sim > 0:
            flatten_qty = min(flatten_qty, limit + pos_sim)
            if flatten_qty > 0:
                orders.append(Order(product, fair_rounded, -flatten_qty))
                pos_sim -= flatten_qty
        else:
            flatten_qty = min(flatten_qty, limit - pos_sim)
            if flatten_qty > 0:
                orders.append(Order(product, fair_rounded, flatten_qty))
                pos_sim += flatten_qty
        abs_pos_sim = abs(pos_sim)

    # ----------------------------------------------------------------
    # STEP 4: Passive sizes — alpha_norm + toxicity + inventory
    # ----------------------------------------------------------------
    alpha_scale = params["alpha_size_scale"]
    # alpha_norm > 0: skew toward more buys, fewer sells; < 0: reverse
    buy_size = float(params["base_size"]) * (1.0 + alpha_scale * alpha_norm)
    sell_size = float(params["base_size"]) * (1.0 - alpha_scale * alpha_norm)

    # Toxic flow: reduce the exposed side to limit adverse selection
    if toxic_buy:
        sell_size *= 0.5    # buyers are hitting our asks; reduce ask qty
    if toxic_sell:
        buy_size *= 0.5     # sellers are hitting our bids; reduce bid qty

    # Three-tier inventory control (same structure as EMERALDS)
    if abs_pos_sim >= int(0.9 * limit):
        if pos_sim > 0:
            buy_size = 0.0
        else:
            sell_size = 0.0
    elif abs_pos_sim >= int(0.7 * limit):
        if pos_sim > 0:
            buy_size *= 0.25
        else:
            sell_size *= 0.25
    else:
        if pos_sim > 0:
            buy_size *= 0.5
        elif pos_sim < 0:
            sell_size *= 0.5

    buy_size = max(0, min(round(buy_size), limit - pos_sim))
    sell_size = max(0, min(round(sell_size), limit + pos_sim))

    if buy_size > 0:
        orders.append(Order(product, bid_px, buy_size))
    if sell_size > 0:
        orders.append(Order(product, ask_px, -sell_size))

    # ----------------------------------------------------------------
    # Diagnostics — stored for offline markout / toxicity analysis
    # ----------------------------------------------------------------
    trader_state[f"{product}_prev_mid"] = mid if mid is not None else fair
    trader_state[f"{product}_last_quote_bid"] = bid_px
    trader_state[f"{product}_last_quote_ask"] = ask_px
    trader_state[f"{product}_last_fair"] = fair
    trader_state[f"{product}_last_reservation"] = reservation
    trader_state[f"{product}_last_alpha"] = alpha_norm
    trader_state[f"{product}_last_tox_score"] = tox_score

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
