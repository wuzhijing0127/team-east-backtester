"""Code generator for the next-gen ASH framework.

Generates self-contained .py files with:
- PEPPER = 80 buy-and-hold (locked, proven base)
- ASH = fully parameterized always-skewed market maker from ASHConfig
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from optimizer.ash_config import ASHConfig, config_diff


def generate_ash_v2(
    config: ASHConfig,
    name: str | None = None,
    output_dir: str | Path = "generated",
    pepper_limit: int = 80,
) -> Path:
    """Generate a complete strategy file from an ASHConfig.

    The file is self-contained — all engine logic is inlined.
    PEPPER is locked at buy-and-hold with the given limit.
    """
    if name is None:
        diff = config_diff(config)
        if diff:
            parts = [f"{k[:8]}_{v}" for k, v in sorted(diff.items())][:5]
            name = "ashv2_" + "_".join(parts)
        else:
            name = "ashv2_baseline"

    code = _build_code(config, pepper_limit)
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    file_path = out_path / f"{name}.py"
    file_path.write_text(code, encoding="utf-8")
    return file_path


def _build_code(c: ASHConfig, pepper_limit: int) -> str:
    diff = config_diff(c)
    diff_str = ", ".join(f"{k}={v}" for k, v in diff.items()) if diff else "defaults"

    return f'''\
# Auto-generated ASH v2 — always-skewed, fully parameterized
# Config: {diff_str}

import json
from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List, Tuple, Optional

# ── ASH Config ────────────────────────────────────────────────
C = {{
    "anchor_fair": {c.anchor_fair},
    "micro_beta": {c.micro_beta},
    "imbalance_beta": {c.imbalance_beta},
    "base_skew": {c.base_skew},
    "inventory_skew_k": {c.inventory_skew_k},
    "signal_skew_k": {c.signal_skew_k},
    "bid_half_spread": {c.bid_half_spread},
    "ask_half_spread": {c.ask_half_spread},
    "join_improve_mode": {c.join_improve_mode},
    "take_buy_edge": {c.take_buy_edge},
    "take_sell_edge": {c.take_sell_edge},
    "take_buy_when_short_edge": {c.take_buy_when_short_edge},
    "take_sell_when_long_edge": {c.take_sell_when_long_edge},
    "quote_size_bid": {c.quote_size_bid},
    "quote_size_ask": {c.quote_size_ask},
    "position_limit": {c.position_limit},
    "tier_medium": {c.tier_medium},
    "tier_high": {c.tier_high},
    "tier_extreme": {c.tier_extreme},
    "bid_mult_normal": {c.bid_mult_normal},
    "ask_mult_normal": {c.ask_mult_normal},
    "bid_mult_medium": {c.bid_mult_medium},
    "ask_mult_medium": {c.ask_mult_medium},
    "bid_mult_high": {c.bid_mult_high},
    "ask_mult_high": {c.ask_mult_high},
    "bid_mult_extreme": {c.bid_mult_extreme},
    "ask_mult_extreme": {c.ask_mult_extreme},
    "flatten_enabled": {c.flatten_enabled},
    "flatten_trigger": {c.flatten_trigger},
    "flatten_size": {c.flatten_size},
    "flatten_aggression": {c.flatten_aggression},
}}

PEPPER_LIMIT = {pepper_limit}


# ── Book extraction ───────────────────────────────────────────
def get_book(od):
    bb = max(od.buy_orders) if od.buy_orders else None
    ba = min(od.sell_orders) if od.sell_orders else None
    mid = micro = None
    spread = 0
    bv = av = 0
    if bb is not None and ba is not None:
        mid = (bb + ba) / 2
        spread = ba - bb
        bv1 = od.buy_orders[bb]
        av1 = abs(od.sell_orders[ba])
        bv = sum(v for v in od.buy_orders.values())
        av = sum(abs(v) for v in od.sell_orders.values())
        t = bv1 + av1
        micro = (bb * av1 + ba * bv1) / t if t > 0 else mid
    imb = (bv - av) / (bv + av) if (bv + av) > 0 else 0.0
    return bb, ba, mid, micro, spread, bv, av, imb


# ── A. Signal ─────────────────────────────────────────────────
def compute_fair(c, mid, micro, imb, spread):
    fair = float(c["anchor_fair"])
    if micro is not None and mid is not None:
        fair += c["micro_beta"] * (micro - mid)
    fair += c["imbalance_beta"] * imb * (spread or 1)
    return fair


def compute_reservation(c, fair, pos, limit):
    res = fair + c["base_skew"]
    res += c["inventory_skew_k"] * (-pos / limit) if limit > 0 else 0
    return res


# ── B. Take engine ────────────────────────────────────────────
def take_orders(product, od, fair_r, pos, limit, c):
    orders = []
    eff_buy = c["take_buy_when_short_edge"] if pos < 0 else c["take_buy_edge"]
    eff_sell = c["take_sell_when_long_edge"] if pos > 0 else c["take_sell_edge"]
    for ap in sorted(od.sell_orders.keys()):
        if ap <= fair_r - eff_buy:
            vol = abs(od.sell_orders[ap])
            qty = min(vol, limit - pos)
            if qty > 0:
                orders.append(Order(product, ap, qty))
                pos += qty
        else:
            break
    for bp in sorted(od.buy_orders.keys(), reverse=True):
        if bp >= fair_r + eff_sell:
            vol = od.buy_orders[bp]
            qty = min(vol, limit + pos)
            if qty > 0:
                orders.append(Order(product, bp, -qty))
                pos -= qty
        else:
            break
    return orders, pos


# ── C. Passive quote engine ───────────────────────────────────
def passive_orders(product, bb, ba, res_r, pos, limit, bid_m, ask_m, c):
    if bb is None or ba is None:
        return []
    raw_bid = res_r - c["bid_half_spread"]
    raw_ask = res_r + c["ask_half_spread"]
    mode = c["join_improve_mode"]
    if mode == 1:
        bp = max(raw_bid, bb)
        ap = min(raw_ask, ba)
    elif mode == 2:
        bp = max(raw_bid, bb + 1)
        ap = min(raw_ask, ba - 1)
    else:
        bp = raw_bid
        ap = raw_ask
    bp = min(bp, ba - 1)
    ap = max(ap, bb + 1)
    if bp >= ap:
        m = (bp + ap) // 2
        bp = m - 1
        ap = m + 1
    orders = []
    bq = min(round(c["quote_size_bid"] * bid_m), limit - pos)
    aq = min(round(c["quote_size_ask"] * ask_m), limit + pos)
    if bq > 0:
        orders.append(Order(product, bp, bq))
    if aq > 0:
        orders.append(Order(product, ap, -aq))
    return orders


# ── D. Risk engine ────────────────────────────────────────────
def inv_mults(pos, limit, c):
    frac = abs(pos) / limit if limit > 0 else 0
    if frac >= c["tier_extreme"]:
        bm, am = c["bid_mult_extreme"], c["ask_mult_extreme"]
    elif frac >= c["tier_high"]:
        bm, am = c["bid_mult_high"], c["ask_mult_high"]
    elif frac >= c["tier_medium"]:
        bm, am = c["bid_mult_medium"], c["ask_mult_medium"]
    else:
        bm, am = c["bid_mult_normal"], c["ask_mult_normal"]
    if pos > 0:
        return bm, c["ask_mult_normal"]
    elif pos < 0:
        return c["bid_mult_normal"], am
    return c["bid_mult_normal"], c["ask_mult_normal"]


def flatten(product, bb, ba, fair_r, pos, limit, c):
    if not c["flatten_enabled"]:
        return []
    if abs(pos) < c["flatten_trigger"] * limit:
        return []
    orders = []
    if pos > 0 and bb is not None:
        qty = min(c["flatten_size"], pos)
        p = fair_r if c["flatten_aggression"] == 1 else bb
        orders.append(Order(product, p, -qty))
    elif pos < 0 and ba is not None:
        qty = min(c["flatten_size"], -pos)
        p = fair_r if c["flatten_aggression"] == 1 else ba
        orders.append(Order(product, p, qty))
    return orders


# ── ASH trade logic ───────────────────────────────────────────
def trade_ash(state, ts):
    product = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(product)
    if od is None:
        return []
    pos = state.position.get(product, 0)
    limit = C["position_limit"]
    bb, ba, mid, micro, spread, bv, av, imb = get_book(od)
    if bb is None or ba is None:
        return []
    fair = compute_fair(C, mid, micro, imb, spread)
    fair_r = round(fair)
    res = compute_reservation(C, fair, pos, limit)
    res_r = round(res)
    orders = []
    # Takes
    tk, pos = take_orders(product, od, fair_r, pos, limit, C)
    orders.extend(tk)
    # Inventory multipliers
    bid_m, ask_m = inv_mults(pos, limit, C)
    # Passive quotes
    pv = passive_orders(product, bb, ba, res_r, pos, limit, bid_m, ask_m, C)
    orders.extend(pv)
    # Flattening
    fl = flatten(product, bb, ba, fair_r, pos, limit, C)
    orders.extend(fl)
    return orders


# ── PEPPER: pure buy-and-hold (locked) ────────────────────────
def trade_pepper(state, ts):
    product = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(product)
    if od is None:
        return []
    pos = state.position.get(product, 0)
    limit = PEPPER_LIMIT
    bb = max(od.buy_orders) if od.buy_orders else None
    ba = min(od.sell_orders) if od.sell_orders else None
    orders = []
    if ba is not None:
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
        if bb is not None and ba is not None:
            bp = bb + 1
            if bp < ba:
                orders.append(Order(product, bp, remaining))
        elif bb is not None:
            orders.append(Order(product, bb + 1, remaining))
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
