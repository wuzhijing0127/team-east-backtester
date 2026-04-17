"""Signal-enhanced codegen — microprice, inventory-skewed quoting, regime detection.

Each feature is independently toggleable for clean ablation testing.
Base: champion config (bs=20, hs=1, position-aware taking)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


DEFAULTS_SIG: dict[str, Any] = {
    # ASH core (champion)
    "ash_position_limit": 50,
    "ash_anchor_fair": 10000,
    "ash_half_spread": 1,
    "ash_k_inv": 2.5,
    "ash_base_size": 20,
    "ash_flatten_size": 10,
    "ash_tier_medium": 0.4,
    "ash_tier_high": 0.7,
    "ash_tier_extreme": 0.9,
    "ash_take_edge_neutral": 1,
    "ash_take_edge_loaded": 0,
    "ash_take_neutral_threshold": 0.3,

    # Feature 1: Microprice signal
    "micro_enabled": False,
    "micro_k": 0.5,            # fair += k * (microprice - mid)
    "micro_take_gate": False,   # only take if aligned with imbalance

    # Feature 2: Inventory-skewed quoting
    "inv_quote_skew_enabled": False,
    "inv_quote_skew_alpha": 1.0,  # skew = alpha * pos / limit

    # Feature 3: Regime detection (EMA crossover)
    "regime_enabled": False,
    "regime_ema_fast": 5,       # fast EMA window (ticks in traderData)
    "regime_ema_slow": 20,      # slow EMA window
    "regime_spread_adjust": 1,  # widen/tighten spread by this in trends

    # PEPPER (locked)
    "pepper_limit": 80,
}


def generate_signals(
    params: dict[str, Any],
    name: str,
    output_dir: str | Path = "generated",
) -> Path:
    p = {**DEFAULTS_SIG, **params}
    code = _build_code(p)
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    file_path = out_path / f"{name}.py"
    file_path.write_text(code, encoding="utf-8")
    return file_path


def _build_code(p: dict[str, Any]) -> str:
    # Fair value computation
    if p["micro_enabled"]:
        fair_logic = f'''\
    micro = get_microprice(od)
    mid_val = get_mid(od)
    if micro is not None and mid_val is not None:
        fair += {p["micro_k"]} * (micro - mid_val)'''
    else:
        fair_logic = ""

    # Take gating
    if p["micro_enabled"] and p["micro_take_gate"]:
        take_gate_buy = """\
    # Gate: only buy-take if microprice suggests upward pressure
    micro_bias = 0
    if micro is not None and mid_val is not None:
        micro_bias = 1 if micro > mid_val else (-1 if micro < mid_val else 0)"""
        take_condition_buy = "and (micro_bias >= 0)"
        take_condition_sell = "and (micro_bias <= 0)"
    else:
        take_gate_buy = ""
        take_condition_buy = ""
        take_condition_sell = ""

    # Inventory-skewed quoting
    if p["inv_quote_skew_enabled"]:
        skew_logic = f'''\
    skew = {p["inv_quote_skew_alpha"]} * pos / limit if limit > 0 else 0
    bid_price = res_r - p["half_spread"] - round(skew)
    ask_price = res_r + p["half_spread"] - round(skew)'''
    else:
        skew_logic = '''\
    bid_price = res_r - p["half_spread"]
    ask_price = res_r + p["half_spread"]'''

    # Regime detection
    if p["regime_enabled"]:
        regime_state_logic = f'''\
    # Regime: EMA crossover
    mid_now = get_mid(od)
    prev_prices = ts.get("ash_prices", [])
    if mid_now is not None:
        prev_prices.append(mid_now)
        if len(prev_prices) > {p["regime_ema_slow"]} + 5:
            prev_prices = prev_prices[-({p["regime_ema_slow"]} + 5):]
        ts["ash_prices"] = prev_prices

    regime = 0  # 0=neutral, 1=trending up, -1=trending down
    if len(prev_prices) >= {p["regime_ema_slow"]}:
        ema_fast = sum(prev_prices[-{p["regime_ema_fast"]}:]) / {p["regime_ema_fast"]}
        ema_slow = sum(prev_prices[-{p["regime_ema_slow"]}:]) / {p["regime_ema_slow"]}
        if ema_fast > ema_slow + 0.5:
            regime = 1
        elif ema_fast < ema_slow - 0.5:
            regime = -1'''
        regime_spread_adjust = f'''\
    # Adjust spread based on regime
    hs_bid = p["half_spread"]
    hs_ask = p["half_spread"]
    if regime == 1:  # trending up: tighten bid, widen ask
        hs_bid = max(1, hs_bid - {p["regime_spread_adjust"]})
        hs_ask = hs_ask + {p["regime_spread_adjust"]}
    elif regime == -1:  # trending down: widen bid, tighten ask
        hs_bid = hs_bid + {p["regime_spread_adjust"]}
        hs_ask = max(1, hs_ask - {p["regime_spread_adjust"]})'''
    else:
        regime_state_logic = ""
        regime_spread_adjust = ""

    # Use regime-adjusted spreads if enabled
    if p["regime_enabled"] and not p["inv_quote_skew_enabled"]:
        quote_spread_logic = '''\
    bid_price = res_r - hs_bid
    ask_price = res_r + hs_ask'''
    elif p["regime_enabled"] and p["inv_quote_skew_enabled"]:
        quote_spread_logic = f'''\
    skew = {p["inv_quote_skew_alpha"]} * pos / limit if limit > 0 else 0
    bid_price = res_r - hs_bid - round(skew)
    ask_price = res_r + hs_ask - round(skew)'''
    else:
        quote_spread_logic = skew_logic

    return f'''\
# Signal-enhanced ASH — microprice={p["micro_enabled"]}, inv_skew={p["inv_quote_skew_enabled"]}, regime={p["regime_enabled"]}
# micro_k={p["micro_k"]}, skew_alpha={p["inv_quote_skew_alpha"]}, regime_fast={p["regime_ema_fast"]}/slow={p["regime_ema_slow"]}

import json
from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List, Tuple, Optional


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
    t = bv + av
    if t == 0:
        return (bid + ask) / 2
    return (bid * av + ask * bv) / t

def inventory_size_multiplier(position, limit, params):
    frac = abs(position) / limit if limit else 0
    if frac >= params["tier_extreme"]:
        m = 0.0
    elif frac >= params["tier_high"]:
        m = 0.25
    elif frac >= params["tier_medium"]:
        m = 0.5
    else:
        m = 1.0
    if position > 0:
        return m, 1.0
    elif position < 0:
        return 1.0, m
    return 1.0, 1.0

ASH_PARAMS = {{
    "position_limit": {p["ash_position_limit"]},
    "anchor_fair": {p["ash_anchor_fair"]},
    "half_spread": {p["ash_half_spread"]},
    "k_inv": {p["ash_k_inv"]},
    "base_size": {p["ash_base_size"]},
    "flatten_size": {p["ash_flatten_size"]},
    "tier_medium": {p["ash_tier_medium"]},
    "tier_high": {p["ash_tier_high"]},
    "tier_extreme": {p["ash_tier_extreme"]},
    "take_edge_neutral": {p["ash_take_edge_neutral"]},
    "take_edge_loaded": {p["ash_take_edge_loaded"]},
    "take_neutral_threshold": {p["ash_take_neutral_threshold"]},
}}

PEPPER_LIMIT = {p["pepper_limit"]}

def trade_ash(state, ts):
    product = "ASH_COATED_OSMIUM"
    p = ASH_PARAMS
    od = state.order_depths.get(product)
    if od is None:
        return []
    position = state.position.get(product, 0)
    limit = p["position_limit"]
    best_bid, best_ask = get_best_bid_ask(od)
    if best_bid is None or best_ask is None:
        return []

    fair = float(p["anchor_fair"])
{fair_logic}
{regime_state_logic}
{take_gate_buy}

    fair_r = round(fair)
    orders = []
    pos = position

    # Position-aware taking
    inv_frac = abs(pos) / limit if limit > 0 else 0
    if inv_frac < p["take_neutral_threshold"]:
        buy_te = p["take_edge_neutral"]
        sell_te = p["take_edge_neutral"]
    else:
        buy_te = p["take_edge_loaded"]
        sell_te = p["take_edge_loaded"]
    if pos < 0:
        buy_te = max(buy_te, p["take_edge_neutral"])
    if pos > 0:
        sell_te = max(sell_te, p["take_edge_neutral"])

    if best_ask is not None:
        for ap in sorted(od.sell_orders.keys()):
            if ap <= fair_r - buy_te {take_condition_buy}:
                vol = abs(od.sell_orders[ap])
                qty = min(vol, limit - pos)
                if qty > 0:
                    orders.append(Order(product, ap, qty))
                    pos += qty
            else:
                break
    if best_bid is not None:
        for bp in sorted(od.buy_orders.keys(), reverse=True):
            if bp >= fair_r + sell_te {take_condition_sell}:
                vol = od.buy_orders[bp]
                qty = min(vol, limit + pos)
                if qty > 0:
                    orders.append(Order(product, bp, -qty))
                    pos -= qty
            else:
                break

    reservation = fair - p["k_inv"] * (pos / limit) if limit > 0 else fair
    res_r = round(reservation)

{regime_spread_adjust}
{quote_spread_logic}

    if best_bid is not None:
        bid_price = min(best_bid + 1, bid_price)
    if best_ask is not None:
        ask_price = max(best_ask - 1, ask_price)
    if best_ask is not None:
        bid_price = min(bid_price, best_ask - 1)
    if best_bid is not None:
        ask_price = max(ask_price, best_bid + 1)

    buy_mult, sell_mult = inventory_size_multiplier(pos, limit, p)
    buy_qty = min(round(p["base_size"] * buy_mult), limit - pos)
    sell_qty = min(round(p["base_size"] * sell_mult), limit + pos)
    if buy_qty > 0:
        orders.append(Order(product, bid_price, buy_qty))
    if sell_qty > 0:
        orders.append(Order(product, ask_price, -sell_qty))

    if abs(pos) >= p["tier_extreme"] * limit:
        if pos > 0 and best_bid is not None:
            orders.append(Order(product, best_bid, -min(p["flatten_size"], pos)))
        elif pos < 0 and best_ask is not None:
            orders.append(Order(product, best_ask, min(p["flatten_size"], -pos)))
    return orders

def trade_pepper(state, ts):
    product = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(product)
    if od is None:
        return []
    pos = state.position.get(product, 0)
    limit = PEPPER_LIMIT
    best_bid, best_ask = get_best_bid_ask(od)
    orders = []
    if best_ask is not None:
        for ap in sorted(od.sell_orders.keys()):
            vol = abs(od.sell_orders[ap])
            qty = min(vol, limit - pos)
            if qty > 0:
                orders.append(Order(product, ap, qty))
                pos += qty
            if pos >= limit:
                break
    remaining = limit - pos
    if remaining > 0:
        if best_bid is not None and best_ask is not None:
            bp = best_bid + 1
            if bp < best_ask:
                orders.append(Order(product, bp, remaining))
        elif best_bid is not None:
            orders.append(Order(product, best_bid + 1, remaining))
    return orders

class Trader:
    def run(self, state):
        orders = {{}}
        ts = json.loads(state.traderData) if state.traderData else {{}}
        for product in state.order_depths:
            if product == "ASH_COATED_OSMIUM":
                orders[product] = trade_ash(state, ts)
            elif product == "INTARIAN_PEPPER_ROOT":
                orders[product] = trade_pepper(state, ts)
        return orders, 0, json.dumps(ts)
'''
