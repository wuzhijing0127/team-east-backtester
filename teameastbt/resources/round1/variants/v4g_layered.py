# v4g_layered
# v4b with a denser L2 closer in (spread 2, size 6). Tests if stacked passive layers compound.
"""
Round 1 Strategy v4 — Playbook-aligned
=======================================
Playbook principles applied:
  - Protect the main edge: never add logic that reduces primary PnL source
  - Product-specific logic: trending vs mean-reverting handled differently
  - Core + overlay structure for PEPPER (core=40 always long, overlay=±10)
  - Asymmetric signals: use data-derived asymmetry for ASH takes

ASH_COATED_OSMIUM: Pure MM (mean-reverting, fair=10,000)
  v4 changes from v3:
  - Asymmetric take_edge: buy at -3 (dips revert 80%), sell at +5 (tops revert only 71%)
    Data: dev<-10 → 80% reversion; dev>+10 → 71% reversion

INTARIAN_PEPPER_ROOT: Always-long drift rider (always uptrend, +1000/day)
  v4 changes from v3:
  - Removed drift regime detection entirely — drift is ALWAYS up, downtrend/neutral
    blocks fight the known trend (playbook: never fight trend)
  - Dip-boosted buy_edge: when price >5 below fair (97.5% up probability),
    buy up to fair+3 to fill faster on high-conviction dips
  - Always in "uptrend" mode, core=40 long protected by min_long_frac=0.80
"""

import json
import math
from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List, Tuple, Optional


# ============================================================
# TUNABLE PARAMETERS
# ============================================================

ASH_PARAMS = {
    "position_limit": 50,
    "anchor_fair": 10000,
    "micro_beta": 0.0,
    # v4: asymmetric take_edge — data shows dips revert more reliably than tops
    "take_edge_buy": 3,         # buy when ask ≤ fair-3 (dips: 80% revert at dev<-10)
    "take_edge_sell": 5,        # sell when bid ≥ fair+5 (tops: only 71% revert at dev>+10)
    "k_inv": 2.5,
    "flatten_size": 10,
    "tier_medium": 0.4,
    "tier_high": 0.7,
    "tier_extreme": 1.0,        # v3: no hard cap — reservation skew handles inventory
    # Spread-adaptive quoting
    "wide_spread_thr": 8,       # v3: lower wide threshold
    "narrow_spread_thr": 4,     # v4b: improve by 1 instead of joining
    # Multi-level quoting
    "L1_size": 8,               # v4b: smaller quotes, less signaling
    "L2_spread": 2,             # v4g: very tight L2
    "L2_size": 6,               # v4g: bigger L2
    "L3_spread": 8,             # wide layer — backstop for sweeps
    "L3_size": 5,               # wide layer size
}

PEPPER_PARAMS = {
    "position_limit": 50,
    "fair_slope": 0.001,
    "day_base_map": {-2: 9998, -1: 10998, 0: 11998},
    # Always-uptrend thresholds (no regime detection — drift is always +1000/day)
    "buy_edge": 0,                # normal: buy at fair exactly
    "dip_buy_edge": 3,            # v4: when dev < -dip_threshold, buy up to fair+3
    "dip_threshold": 5,           # v4: dev < -5 → 97.5% probability of upward move
    "take_profit_edge": 4,        # sell when bid > fair + this
    "min_long_frac": 0.80,        # core position = 40 units always long
    # Passive quoting
    "bid_spread": 4,
    "ask_spread": 8,
    "base_size": 10,
    # Safety
    "fair_sanity_max_dev": 20,
    "inventory_tiers": {
        "medium": 0.4,
        "high": 0.6,
        "extreme": 0.85,
    },
    # Spread-adaptive quoting
    "wide_spread_thr": 8,
    "narrow_spread_thr": 25,
}


# ============================================================
# HELPERS
# ============================================================

def get_best_bid_ask(od: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def get_mid(od: OrderDepth) -> Optional[float]:
    bid, ask = get_best_bid_ask(od)
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def get_microprice(od: OrderDepth) -> Optional[float]:
    bid, ask = get_best_bid_ask(od)
    if bid is None or ask is None:
        return None
    bid_vol = od.buy_orders[bid]
    ask_vol = abs(od.sell_orders[ask])
    total = bid_vol + ask_vol
    if total == 0:
        return (bid + ask) / 2
    return (bid * ask_vol + ask * bid_vol) / total


def inventory_size_multiplier(position: int, limit: int, tiers: dict) -> Tuple[float, float]:
    frac = abs(position) / limit if limit else 0

    if frac >= tiers.get("extreme", 0.9):
        add_mult = 0.0
    elif frac >= tiers.get("high", 0.7):
        add_mult = 0.25
    elif frac >= tiers.get("medium", 0.4):
        add_mult = 0.5
    else:
        add_mult = 1.0

    if position > 0:
        return add_mult, 1.0
    elif position < 0:
        return 1.0, add_mult
    else:
        return 1.0, 1.0


def spread_adaptive_quote(
    fair_r: int,
    best_bid: int,
    best_ask: int,
    base_spread: int,
    wide_thr: int,
    narrow_thr: int,
    bias: int = 0,
) -> Tuple[int, int]:
    """
    Choose bid/ask prices based on current market spread width.

    bias: >0 means we want to be long (tighten bid, widen ask)
          <0 means we want to be short (widen bid, tighten ask)

    Returns (bid_price, ask_price).
    """
    mkt_spread = best_ask - best_bid

    if mkt_spread <= narrow_thr:
        # Narrow market — join best, don't undercut
        bid_price = best_bid
        ask_price = best_ask
    elif mkt_spread >= wide_thr:
        # Wide market — improve best by 1 to grab queue priority
        bid_price = best_bid + 1
        ask_price = best_ask - 1
    else:
        # Normal — quote around fair but stay competitive with best
        bid_price = max(fair_r - base_spread, best_bid + 1)
        ask_price = min(fair_r + base_spread, best_ask - 1)
        # If fair-based price is worse than best, at least match best
        bid_price = max(bid_price, best_bid)
        ask_price = min(ask_price, best_ask)

    # Apply directional bias: shift the eager side 1 tick tighter
    if bias > 0:
        bid_price = min(bid_price + 1, best_ask - 1)  # tighten bid
    elif bias < 0:
        ask_price = max(ask_price - 1, best_bid + 1)  # tighten ask

    # Final safety — never cross
    bid_price = min(bid_price, best_ask - 1)
    ask_price = max(ask_price, best_bid + 1)

    return bid_price, ask_price


# ============================================================
# ASH_COATED_OSMIUM — Anchored Market Maker
# ============================================================

def trade_ash(state: TradingState, ts: dict) -> List[Order]:
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
    if micro is not None:
        mid = get_mid(od)
        if mid is not None:
            fair = fair + p["micro_beta"] * (micro - mid)

    fair_r = round(fair)
    orders: List[Order] = []
    pos = position

    # Aggressive takes — asymmetric: buy dips more readily (80% revert), sell tops conservatively (71%)
    if best_ask is not None:
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price <= fair_r - p["take_edge_buy"]:
                vol = abs(od.sell_orders[ask_price])
                qty = min(vol, limit - pos)
                if qty > 0:
                    orders.append(Order(product, ask_price, qty))
                    pos += qty
            else:
                break

    if best_bid is not None:
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price >= fair_r + p["take_edge_sell"]:
                vol = od.buy_orders[bid_price]
                qty = min(vol, limit + pos)
                if qty > 0:
                    orders.append(Order(product, bid_price, -qty))
                    pos -= qty
            else:
                break

    # Multi-level passive quotes with reservation skew
    reservation = fair - p["k_inv"] * (pos / limit)
    res_r = round(reservation)

    tiers = {"medium": p["tier_medium"], "high": p["tier_high"], "extreme": p["tier_extreme"]}
    buy_mult, sell_mult = inventory_size_multiplier(pos, limit, tiers)

    if best_bid is not None and best_ask is not None:
        inv_bias = -1 if pos > limit * 0.3 else (1 if pos < -limit * 0.3 else 0)
        remaining_buy = limit - pos
        remaining_sell = limit + pos

        # --- L1: Tight layer — spread-adaptive, grabs queue priority ---
        l1_buy = min(round(p["L1_size"] * buy_mult), remaining_buy)
        l1_sell = min(round(p["L1_size"] * sell_mult), remaining_sell)
        l1_bid, l1_ask = spread_adaptive_quote(
            res_r, best_bid, best_ask,
            3, p["wide_spread_thr"], p["narrow_spread_thr"],
            bias=inv_bias,
        )
        if l1_buy > 0:
            orders.append(Order(product, l1_bid, l1_buy))
            remaining_buy -= l1_buy
        if l1_sell > 0:
            orders.append(Order(product, l1_ask, -l1_sell))
            remaining_sell -= l1_sell

        # --- L2: Medium layer — reservation-based ---
        l2_bid = min(res_r - p["L2_spread"], l1_bid - 1)
        l2_ask = max(res_r + p["L2_spread"], l1_ask + 1)
        l2_bid = min(l2_bid, best_ask - 1)
        l2_ask = max(l2_ask, best_bid + 1)
        l2_buy = min(round(p["L2_size"] * buy_mult), remaining_buy)
        l2_sell = min(round(p["L2_size"] * sell_mult), remaining_sell)
        if l2_buy > 0:
            orders.append(Order(product, l2_bid, l2_buy))
            remaining_buy -= l2_buy
        if l2_sell > 0:
            orders.append(Order(product, l2_ask, -l2_sell))
            remaining_sell -= l2_sell

        # --- L3: Wide layer — backstop for big sweeps ---
        l3_bid = min(res_r - p["L3_spread"], l2_bid - 1)
        l3_ask = max(res_r + p["L3_spread"], l2_ask + 1)
        l3_bid = min(l3_bid, best_ask - 1)
        l3_ask = max(l3_ask, best_bid + 1)
        l3_buy = min(round(p["L3_size"] * buy_mult), remaining_buy)
        l3_sell = min(round(p["L3_size"] * sell_mult), remaining_sell)
        if l3_buy > 0:
            orders.append(Order(product, l3_bid, l3_buy))
        if l3_sell > 0:
            orders.append(Order(product, l3_ask, -l3_sell))

    # Flatten near limits
    if abs(pos) >= p["tier_extreme"] * limit:
        if pos > 0 and best_bid is not None:
            orders.append(Order(product, fair_r, -min(p["flatten_size"], pos)))
        elif pos < 0 and best_ask is not None:
            orders.append(Order(product, fair_r, min(p["flatten_size"], -pos)))

    return orders


# ============================================================
# INTARIAN_PEPPER_ROOT — Drift Rider (Hardened)
# ============================================================

def trade_pepper(state: TradingState, ts: dict) -> List[Order]:
    """
    PEPPER always drifts +1000/day — never fight the trend.
    Playbook: core=40 long always, overlay=±10 active trades.
    No drift regime detection — it's a known constant, not a signal.
    """
    product = "INTARIAN_PEPPER_ROOT"
    p = PEPPER_PARAMS
    od = state.order_depths.get(product)
    if od is None:
        return []

    position = state.position.get(product, 0)
    limit = p["position_limit"]
    best_bid, best_ask = get_best_bid_ask(od)
    mid = get_mid(od)

    if mid is None or best_bid is None or best_ask is None:
        return []

    timestamp = state.timestamp

    # Determine day base from first tick (anchored to known schedule)
    day_base = ts.get("pepper_day_base")
    if day_base is None:
        best_dist = float('inf')
        for _, base in p["day_base_map"].items():
            expected = base + p["fair_slope"] * timestamp
            dist = abs(mid - expected)
            if dist < best_dist:
                best_dist = dist
                day_base = base
        ts["pepper_day_base"] = day_base

    fair = day_base + p["fair_slope"] * timestamp
    fair_r = round(fair)
    dev = mid - fair  # signed: negative = price below fair (dip)

    # Safety: if market has drifted way off model, do nothing
    if abs(dev) >= p["fair_sanity_max_dev"]:
        return []

    orders: List[Order] = []
    pos = position
    min_hold = round(limit * p["min_long_frac"])  # core = 40

    # ===========================================================
    # AGGRESSIVE BUY: always long with drift
    # Dip boost: when dev < -dip_threshold (97.5% up probability),
    # pay up to dip_buy_edge above fair to fill faster
    # ===========================================================
    buy_edge = p["dip_buy_edge"] if dev < -p["dip_threshold"] else p["buy_edge"]
    for ask_price in sorted(od.sell_orders.keys()):
        if ask_price <= fair_r + buy_edge:
            vol = abs(od.sell_orders[ask_price])
            qty = min(vol, limit - pos)
            if qty > 0:
                orders.append(Order(product, ask_price, qty))
                pos += qty
        else:
            break

    # ===========================================================
    # TAKE PROFIT: sell overlay above fair + edge, never below core
    # ===========================================================
    for bid_price in sorted(od.buy_orders.keys(), reverse=True):
        if bid_price > fair_r + p["take_profit_edge"]:
            vol = od.buy_orders[bid_price]
            max_sell = max(0, pos - min_hold)
            qty = min(vol, max_sell)
            if qty > 0:
                orders.append(Order(product, bid_price, -qty))
                pos -= qty
        else:
            break

    # ===========================================================
    # PASSIVE: always bid to rebuild/maintain position toward max
    # ===========================================================
    remaining_buy = limit - pos
    if remaining_buy > 0:
        bid_price, _ = spread_adaptive_quote(
            fair_r, best_bid, best_ask,
            p["bid_spread"], p["wide_spread_thr"], p["narrow_spread_thr"],
            bias=1,  # long bias → tighten bid
        )
        orders.append(Order(product, bid_price, remaining_buy))

    # Post a small ask only when well above core — keeps some overlay income
    if pos > round(limit * 0.5):
        sell_qty = min(p["base_size"], pos - min_hold)
        if sell_qty > 0:
            _, ask_price = spread_adaptive_quote(
                fair_r, best_bid, best_ask,
                p["ask_spread"], p["wide_spread_thr"], p["narrow_spread_thr"],
                bias=1,  # long bias → widen ask (don't sell cheaply)
            )
            orders.append(Order(product, ask_price, -sell_qty))

    return orders


# ============================================================
# MAIN TRADER
# ============================================================

class Trader:
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        orders: Dict[str, List[Order]] = {}
        conversions = 0
        ts = json.loads(state.traderData) if state.traderData else {}

        for product in state.order_depths:
            if product == "ASH_COATED_OSMIUM":
                orders[product] = trade_ash(state, ts)
            elif product == "INTARIAN_PEPPER_ROOT":
                orders[product] = trade_pepper(state, ts)

        return orders, conversions, json.dumps(ts)