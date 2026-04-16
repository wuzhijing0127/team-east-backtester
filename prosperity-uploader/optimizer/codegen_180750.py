"""Code generator for the 180750 strategy template (simplified MM + pure buy-and-hold).

This is the confirmed best-performer on the platform. Different structure from v4o:
- ASH: Single half_spread, position-adaptive take_edge (0 when reducing, full when adding)
- PEPPER: Pure buy-and-hold until late-stage sell-off at configurable timestamp
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

DEFAULTS_180750: dict[str, Any] = {
    # ASH
    "ash_position_limit": 50,
    "ash_anchor_fair": 10000,
    "ash_micro_beta": 0.0,
    "ash_take_edge": 2,
    "ash_half_spread": 6,
    "ash_k_inv": 2.5,
    "ash_base_size": 15,
    "ash_flatten_size": 10,
    "ash_tier_medium": 0.4,
    "ash_tier_high": 0.7,
    "ash_tier_extreme": 0.9,
    # PEPPER
    "pepper_limit": 50,
    "pepper_end_sell_start": 980000,
}

SEARCH_PARAMS_180750: dict[str, dict[str, Any]] = {
    "ash_take_edge":        {"default": 2,      "type": "int",   "range": [0, 1, 2, 3, 4, 5]},
    "ash_half_spread":      {"default": 6,      "type": "int",   "range": [3, 4, 5, 6, 7, 8, 10, 12]},
    "ash_k_inv":            {"default": 2.5,    "type": "float", "range": [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0]},
    "ash_base_size":        {"default": 15,     "type": "int",   "range": [5, 8, 10, 12, 15, 18, 20, 25, 30]},
    "ash_flatten_size":     {"default": 10,     "type": "int",   "range": [5, 10, 15, 20]},
    "ash_tier_medium":      {"default": 0.4,    "type": "float", "range": [0.3, 0.4, 0.5, 0.6]},
    "ash_tier_high":        {"default": 0.7,    "type": "float", "range": [0.5, 0.6, 0.7, 0.8]},
    "ash_tier_extreme":     {"default": 0.9,    "type": "float", "range": [0.8, 0.85, 0.9, 0.95, 1.0]},
    "pepper_end_sell_start":{"default": 980000, "type": "int",   "range": [900000, 920000, 940000, 960000, 980000, 990000, 999000]},
}


def merge_params_180750(overrides: dict[str, Any]) -> dict[str, Any]:
    params = dict(DEFAULTS_180750)
    for k, v in overrides.items():
        if k not in DEFAULTS_180750:
            raise ValueError(f"Unknown 180750 parameter: {k}")
        params[k] = v
    return params


def params_to_name_180750(params: dict[str, Any]) -> str:
    diffs = []
    for k, v in sorted(params.items()):
        if k in DEFAULTS_180750 and v != DEFAULTS_180750[k]:
            short = k.replace("ash_", "a_").replace("pepper_", "p_")
            diffs.append(f"{short}_{v}")
    if not diffs:
        return "s180750_baseline"
    return "s180750_" + "_".join(diffs[:5])


def generate_strategy_180750(
    params: dict[str, Any],
    name: str | None = None,
    output_dir: str | Path = "generated",
) -> Path:
    if name is None:
        name = params_to_name_180750(params)
    p = params
    code = _build_code_180750(p)
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    file_path = out_path / f"{name}.py"
    file_path.write_text(code, encoding="utf-8")
    return file_path


def _compact_diff_180750(p: dict[str, Any]) -> str:
    diffs = []
    for k, v in sorted(p.items()):
        if k in DEFAULTS_180750 and v != DEFAULTS_180750[k]:
            diffs.append(f"{k}={v}")
    return ", ".join(diffs) if diffs else "defaults (180750)"


def _build_code_180750(p: dict[str, Any]) -> str:
    return f'''\
# Auto-generated from 180750 template — optimizer variant
# Params: {_compact_diff_180750(p)}

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
    bid_vol = od.buy_orders[bid]
    ask_vol = abs(od.sell_orders[ask])
    total = bid_vol + ask_vol
    if total == 0:
        return (bid + ask) / 2
    return (bid * ask_vol + ask * bid_vol) / total


def inventory_size_multiplier(position, limit, params):
    frac = abs(position) / limit if limit else 0
    if frac >= params["tier_extreme"]:
        add_mult = 0.0
    elif frac >= params["tier_high"]:
        add_mult = 0.25
    elif frac >= params["tier_medium"]:
        add_mult = 0.5
    else:
        add_mult = 1.0
    if position > 0:
        return add_mult, 1.0
    elif position < 0:
        return 1.0, add_mult
    return 1.0, 1.0


ASH_PARAMS = {{
    "position_limit": {p["ash_position_limit"]},
    "anchor_fair": {p["ash_anchor_fair"]},
    "micro_beta": {p["ash_micro_beta"]},
    "take_edge": {p["ash_take_edge"]},
    "half_spread": {p["ash_half_spread"]},
    "k_inv": {p["ash_k_inv"]},
    "base_size": {p["ash_base_size"]},
    "flatten_size": {p["ash_flatten_size"]},
    "tier_medium": {p["ash_tier_medium"]},
    "tier_high": {p["ash_tier_high"]},
    "tier_extreme": {p["ash_tier_extreme"]},
}}

PEPPER_LIMIT = {p["pepper_limit"]}
PEPPER_END_SELL_START = {p["pepper_end_sell_start"]}


def trade_ash(state, ts):
    product = "ASH_COATED_OSMIUM"
    p = ASH_PARAMS
    od = state.order_depths.get(product)
    if od is None:
        return []
    position = state.position.get(product, 0)
    limit = p["position_limit"]
    best_bid, best_ask = get_best_bid_ask(od)
    fair = p["anchor_fair"]
    micro = get_microprice(od)
    mid = get_mid(od)
    if micro is not None and mid is not None:
        fair = fair + p["micro_beta"] * (micro - mid)
    fair_r = round(fair)
    orders = []
    pos = position
    buy_take_edge = p["take_edge"]
    sell_take_edge = p["take_edge"]
    if pos < 0:
        buy_take_edge = 0
    if pos > 0:
        sell_take_edge = 0
    if best_ask is not None:
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price <= fair_r - buy_take_edge:
                vol = abs(od.sell_orders[ask_price])
                qty = min(vol, limit - pos)
                if qty > 0:
                    orders.append(Order(product, ask_price, qty))
                    pos += qty
            else:
                break
    if best_bid is not None:
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price >= fair_r + sell_take_edge:
                vol = od.buy_orders[bid_price]
                qty = min(vol, limit + pos)
                if qty > 0:
                    orders.append(Order(product, bid_price, -qty))
                    pos -= qty
            else:
                break
    reservation = fair - p["k_inv"] * (pos / limit)
    res_r = round(reservation)
    bid_price = res_r - p["half_spread"]
    ask_price = res_r + p["half_spread"]
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
            qty = min(p["flatten_size"], pos)
            orders.append(Order(product, best_bid, -qty))
        elif pos < 0 and best_ask is not None:
            qty = min(p["flatten_size"], -pos)
            orders.append(Order(product, best_ask, qty))
    return orders


def trade_pepper(state, ts):
    product = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(product)
    if od is None:
        return []
    position = state.position.get(product, 0)
    limit = PEPPER_LIMIT
    best_bid, best_ask = get_best_bid_ask(od)
    t = state.timestamp
    orders = []
    pos = position
    if t < PEPPER_END_SELL_START:
        if best_ask is None:
            return []
        for ask_price in sorted(od.sell_orders.keys()):
            vol = abs(od.sell_orders[ask_price])
            qty = min(vol, limit - pos)
            if qty > 0:
                orders.append(Order(product, ask_price, qty))
                pos += qty
            if pos >= limit:
                break
        remaining = limit - pos
        if remaining > 0:
            orders.append(Order(product, best_ask - 1, remaining))
    else:
        if best_bid is not None and pos > 0:
            for bid_price in sorted(od.buy_orders.keys(), reverse=True):
                vol = od.buy_orders[bid_price]
                qty = min(vol, pos)
                if qty > 0:
                    orders.append(Order(product, bid_price, -qty))
                    pos -= qty
                if pos <= 0:
                    break
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
