"""Unified codegen for the disciplined test matrix.

Generates self-contained .py files from a config dict with:
- take_schedule: list of [threshold, take_edge] pairs
- quote_mode: "symmetric", "static_asym", "inv_conditional", "neutral_conditional"
- bid_hs / ask_hs: base quote widths
- inv_conditional_quotes: dict with long/short/neutral quote widths
- neutral_quote_schedule: list of [threshold, bid_hs, ask_hs, take_edge]
- pepper_coupling: dict with threshold + modified ASH params
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def generate_matrix_config(config: dict[str, Any], output_dir: str | Path = "generated") -> Path:
    """Generate a strategy file from a matrix config dict."""
    name = config["name"]
    code = _build_code(config)
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    file_path = out_path / f"{name}.py"
    file_path.write_text(code, encoding="utf-8")
    return file_path


def _build_take_logic(config: dict) -> str:
    schedule = config.get("take_schedule", [[0.30, 1], [1.01, 0]])
    lines = []
    for i, (thr, edge) in enumerate(schedule):
        cond = "if" if i == 0 else "elif"
        if thr >= 1.0:
            lines.append(f"    else:\n        buy_te = {edge}\n        sell_te = {edge}")
        else:
            lines.append(f"    {cond} inv_ratio < {thr}:\n        buy_te = {edge}\n        sell_te = {edge}")
    # Counter-inventory bias: when short, buy more aggressively; when long, sell more aggressively
    lines.append("    if pos < 0:\n        buy_te = max(buy_te, buy_te)")
    lines.append("    if pos > 0:\n        sell_te = max(sell_te, sell_te)")
    return "\n".join(lines)


def _build_quote_logic(config: dict) -> str:
    mode = config.get("quote_mode", "symmetric")

    if mode == "symmetric":
        bid_hs = config.get("bid_hs", 1)
        ask_hs = config.get("ask_hs", 1)
        return f"    bid_hs = {bid_hs}\n    ask_hs = {ask_hs}"

    elif mode == "static_asym":
        bid_hs = config.get("bid_hs", 1)
        ask_hs = config.get("ask_hs", 1)
        return f"    bid_hs = {bid_hs}\n    ask_hs = {ask_hs}"

    elif mode == "inv_conditional":
        ic = config["inv_conditional_quotes"]
        lines = [
            f"    if pos > 0:",
            f"        bid_hs = {ic['long']['bid_hs']}",
            f"        ask_hs = {ic['long']['ask_hs']}",
            f"    elif pos < 0:",
            f"        bid_hs = {ic['short']['bid_hs']}",
            f"        ask_hs = {ic['short']['ask_hs']}",
            f"    else:",
            f"        bid_hs = {ic['neutral']['bid_hs']}",
            f"        ask_hs = {ic['neutral']['ask_hs']}",
        ]
        return "\n".join(lines)

    elif mode == "neutral_conditional":
        nc = config["neutral_quote_schedule"]
        lines = []
        for i, (thr, bhs, ahs) in enumerate(nc):
            cond = "if" if i == 0 else "elif"
            if thr >= 1.0:
                lines.append(f"    else:\n        bid_hs = {bhs}\n        ask_hs = {ahs}")
            else:
                lines.append(f"    {cond} inv_ratio < {thr}:\n        bid_hs = {bhs}\n        ask_hs = {ahs}")
        return "\n".join(lines)

    return "    bid_hs = 1\n    ask_hs = 1"


def _build_neutral_schedule_logic(config: dict) -> str:
    """For Family C: joint quote+take schedules."""
    ns = config.get("neutral_schedule")
    if not ns:
        return None

    lines = []
    for i, (thr, bhs, ahs, te) in enumerate(ns):
        cond = "if" if i == 0 else "elif"
        if thr >= 1.0:
            lines.append(f"    else:\n        bid_hs = {bhs}\n        ask_hs = {ahs}\n        buy_te = {te}\n        sell_te = {te}")
        else:
            lines.append(f"    {cond} inv_ratio < {thr}:\n        bid_hs = {bhs}\n        ask_hs = {ahs}\n        buy_te = {te}\n        sell_te = {te}")
    return "\n".join(lines)


def _build_pepper_coupling(config: dict) -> str:
    pc = config.get("pepper_coupling")
    if not pc:
        return ""

    thr = pc["threshold"]
    mode = pc.get("mode", "conservative")

    if mode == "conservative":
        return f"""
    pepper_pos = state.position.get("INTARIAN_PEPPER_ROOT", 0)
    pepper_ratio = abs(pepper_pos) / {config.get('pepper_limit', 80)}
    if pepper_ratio >= {thr}:
        bid_hs = {pc.get('bid_hs', 2)}
        ask_hs = {pc.get('ask_hs', 2)}
        buy_te = {pc.get('take_edge', 0)}
        sell_te = {pc.get('take_edge', 0)}"""

    elif mode == "neutral_tighten":
        return f"""
    pepper_pos = state.position.get("INTARIAN_PEPPER_ROOT", 0)
    pepper_ratio = abs(pepper_pos) / {config.get('pepper_limit', 80)}
    if pepper_ratio >= {thr}:
        neutral_thr = {pc.get('neutral_threshold', 0.15)}
        if inv_ratio >= neutral_thr:
            buy_te = 0
            sell_te = 0"""

    elif mode == "mild_widen":
        return f"""
    pepper_pos = state.position.get("INTARIAN_PEPPER_ROOT", 0)
    pepper_ratio = abs(pepper_pos) / {config.get('pepper_limit', 80)}
    if pepper_ratio >= {thr}:
        bid_hs = {pc.get('bid_hs', 1)}
        ask_hs = {pc.get('ask_hs', 2)}"""

    return ""


def _build_code(config: dict) -> str:
    name = config["name"]
    family = config.get("family", "unknown")
    pepper_limit = config.get("pepper_limit", 80)
    base_size = config.get("ash_base_size", 20)
    k_inv = config.get("ash_k_inv", 2.5)
    position_limit = config.get("ash_position_limit", 50)
    flatten_size = config.get("ash_flatten_size", 10)
    tier_medium = config.get("ash_tier_medium", 0.4)
    tier_high = config.get("ash_tier_high", 0.7)
    tier_extreme = config.get("ash_tier_extreme", 0.9)

    # Build the schedule logic
    ns = config.get("neutral_schedule")
    if ns:
        schedule_logic = _build_neutral_schedule_logic(config)
        # For neutral_schedule, take and quote are set together
        take_logic = ""
        quote_logic = ""
    else:
        schedule_logic = None
        take_logic = _build_take_logic(config)
        quote_logic = _build_quote_logic(config)

    pepper_coupling = _build_pepper_coupling(config)

    # Build the combined decision block
    if schedule_logic:
        decision_block = schedule_logic
    else:
        decision_block = take_logic + "\n" + quote_logic

    if pepper_coupling:
        decision_block += "\n" + pepper_coupling

    return f'''\
# {name} — family: {family}
# Config: {json.dumps({k: v for k, v in config.items() if k not in ("name", "family")}, default=str)[:200]}

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

def inventory_size_multiplier(position, limit, tier_medium, tier_high, tier_extreme):
    frac = abs(position) / limit if limit else 0
    if frac >= tier_extreme:
        m = 0.0
    elif frac >= tier_high:
        m = 0.25
    elif frac >= tier_medium:
        m = 0.5
    else:
        m = 1.0
    if position > 0:
        return m, 1.0
    elif position < 0:
        return 1.0, m
    return 1.0, 1.0


PEPPER_LIMIT = {pepper_limit}


def trade_ash(state, ts):
    product = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(product)
    if od is None:
        return []
    pos = state.position.get(product, 0)
    limit = {position_limit}
    best_bid, best_ask = get_best_bid_ask(od)
    if best_bid is None or best_ask is None:
        return []

    fair_r = {config.get("ash_anchor_fair", 10000)}
    inv_ratio = abs(pos) / limit if limit > 0 else 0
    orders = []

    # Decision logic
{decision_block}

    # Taking
    if best_ask is not None:
        for ap in sorted(od.sell_orders.keys()):
            if ap <= fair_r - buy_te:
                vol = abs(od.sell_orders[ap])
                qty = min(vol, limit - pos)
                if qty > 0:
                    orders.append(Order(product, ap, qty))
                    pos += qty
            else:
                break
    if best_bid is not None:
        for bp in sorted(od.buy_orders.keys(), reverse=True):
            if bp >= fair_r + sell_te:
                vol = od.buy_orders[bp]
                qty = min(vol, limit + pos)
                if qty > 0:
                    orders.append(Order(product, bp, -qty))
                    pos -= qty
            else:
                break

    # Passive quoting
    reservation = fair_r - {k_inv} * (pos / limit) if limit > 0 else fair_r
    res_r = round(reservation)
    bid_price = res_r - bid_hs
    ask_price = res_r + ask_hs
    if best_bid is not None:
        bid_price = min(best_bid + 1, bid_price)
    if best_ask is not None:
        ask_price = max(best_ask - 1, ask_price)
    if best_ask is not None:
        bid_price = min(bid_price, best_ask - 1)
    if best_bid is not None:
        ask_price = max(ask_price, best_bid + 1)

    buy_mult, sell_mult = inventory_size_multiplier(pos, limit, {tier_medium}, {tier_high}, {tier_extreme})
    buy_qty = min(round({base_size} * buy_mult), limit - pos)
    sell_qty = min(round({base_size} * sell_mult), limit + pos)
    if buy_qty > 0:
        orders.append(Order(product, bid_price, buy_qty))
    if sell_qty > 0:
        orders.append(Order(product, ask_price, -sell_qty))

    if abs(pos) >= {tier_extreme} * limit:
        if pos > 0 and best_bid is not None:
            orders.append(Order(product, best_bid, -min({flatten_size}, pos)))
        elif pos < 0 and best_ask is not None:
            orders.append(Order(product, best_ask, min({flatten_size}, -pos)))
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
