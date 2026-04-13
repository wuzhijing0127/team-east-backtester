"""
Composite Strategy — Route Each Product to Its Best Sub-Strategy
=================================================================
This is the main entry point you'd submit to the competition.
It maps each product to the appropriate strategy logic and
merges all orders into a single return.

All tunable parameters are centralized in the CONFIG dict below.
Adjust them here without touching the strategy logic.
"""

import json
import math
from datamodel import Order, OrderDepth, Trade, TradingState
from typing import Dict, List, Tuple, Optional


# ============================================================
# MASTER CONFIG — One place to tune everything
# ============================================================
CONFIG = {
    # ---- Market Making (stable products) ----
    "market_make": {
        "products": ["EMERALDS", "RAINFOREST_RESIN"],
        "params": {
            "EMERALDS": {
                "position_limit": 50,
                "fair_value": 10000,
                "half_spread": 4,
                "take_edge": 1,
            },
            "RAINFOREST_RESIN": {
                "position_limit": 50,
                "fair_value": 10000,
                "half_spread": 3,
                "take_edge": 1,
            },
        },
        "defaults": {
            "position_limit": 50,
            "fair_value": None,      # None = use wall_mid
            "half_spread": 3,
            "take_edge": 1,
        },
    },

    # ---- Mean Reversion (volatile, mean-reverting products) ----
    "mean_revert": {
        "products": ["TOMATOES", "KELP"],
        "params": {
            "TOMATOES": {
                "position_limit": 50,
                "ema_alpha": 0.05,
                "entry_z": 1.0,
                "exit_z": 0.3,
                "vol_alpha": 0.05,
                "passive_spread": 2,
                "passive_size": 20,
            },
            "KELP": {
                "position_limit": 50,
                "ema_alpha": 0.08,
                "entry_z": 0.8,
                "exit_z": 0.2,
                "vol_alpha": 0.05,
                "passive_spread": 2,
                "passive_size": 15,
            },
        },
        "defaults": {
            "position_limit": 50,
            "ema_alpha": 0.1,
            "entry_z": 1.0,
            "exit_z": 0.3,
            "vol_alpha": 0.05,
            "passive_spread": 2,
            "passive_size": 20,
        },
    },

    # ---- Momentum (trend-following products) ----
    "momentum": {
        "products": ["SQUID_INK"],
        "params": {
            "SQUID_INK": {
                "position_limit": 50,
                "fast_window": 5,
                "slow_window": 20,
                "entry_threshold": 0.5,
                "trailing_stop": 5.0,
            },
        },
        "defaults": {
            "position_limit": 50,
            "fast_window": 5,
            "slow_window": 20,
            "entry_threshold": 0.5,
            "trailing_stop": 5.0,
        },
    },

    # ---- Informed Tracker ----
    "informed": {
        "products": [],   # Add products here if informed traders detected
        "params": {},
        "tracked_traders": ["Olivia"],
        "reaction_window": 500,
        "defaults": {
            "position_limit": 50,
            "signal_strength": 1.0,
            "mm_half_spread": 3,
        },
    },

    # ---- Pairs / Basket Arbitrage ----
    "pairs": {
        "groups": [
            # Uncomment and configure when basket products are available:
            # {
            #     "basket": "PICNIC_BASKET1",
            #     "legs": {"CROISSANTS": 6, "JAMS": 3, "DJEMBES": 1},
            #     "basket_limit": 60,
            #     "leg_limits": {"CROISSANTS": 250, "JAMS": 350, "DJEMBES": 60},
            #     "initial_premium": 5,
            #     "premium_alpha": 0.001,
            #     "entry_threshold": 80,
            #     "exit_threshold": 10,
            #     "hedge_factor": 0.5,
            # },
        ],
    },
}


# ============================================================
# HELPERS
# ============================================================

def ema(prev: float, new: float, alpha: float) -> float:
    return alpha * new + (1 - alpha) * prev


def get_mid(od: OrderDepth) -> Optional[float]:
    if not od.buy_orders or not od.sell_orders:
        return None
    return (max(od.buy_orders) + min(od.sell_orders)) / 2


def get_wall_mid(od: OrderDepth) -> Optional[float]:
    if not od.buy_orders or not od.sell_orders:
        return None
    bid_wall = max(od.buy_orders.keys(), key=lambda p: od.buy_orders[p])
    ask_wall = min(od.sell_orders.keys(), key=lambda p: abs(od.sell_orders[p]))
    return (bid_wall + ask_wall) / 2


# ============================================================
# STRATEGY IMPLEMENTATIONS
# ============================================================

def run_market_make(product: str, state: TradingState, ts: dict) -> List[Order]:
    """Stable-price market making with aggressive takes."""
    cfg = CONFIG["market_make"]
    p = dict(cfg["defaults"])
    p.update(cfg["params"].get(product, {}))

    od = state.order_depths[product]
    pos = state.position.get(product, 0)
    limit = p["position_limit"]

    fair = p["fair_value"]
    if fair is None:
        fair = get_wall_mid(od)
        if fair is None:
            fair = get_mid(od)
    if fair is None:
        return []

    fair = round(fair)
    orders: List[Order] = []
    running_pos = pos

    # Take profitable crosses
    edge = p["take_edge"]
    for ask_price in sorted(od.sell_orders.keys()):
        if ask_price < fair - edge + 1:
            vol = abs(od.sell_orders[ask_price])
            qty = min(vol, limit - running_pos)
            if qty > 0:
                orders.append(Order(product, ask_price, qty))
                running_pos += qty
        else:
            break

    for bid_price in sorted(od.buy_orders.keys(), reverse=True):
        if bid_price > fair + edge - 1:
            vol = od.buy_orders[bid_price]
            qty = min(vol, limit + running_pos)
            if qty > 0:
                orders.append(Order(product, bid_price, -qty))
                running_pos -= qty
        else:
            break

    # Passive quotes with inventory skew
    hs = p["half_spread"]
    buy_qty = limit - running_pos
    sell_qty = limit + running_pos

    if buy_qty > 0:
        orders.append(Order(product, fair - hs, buy_qty))
    if sell_qty > 0:
        orders.append(Order(product, fair + hs, -sell_qty))

    # Flatten at fair
    if running_pos > 0:
        orders.append(Order(product, fair, -running_pos))
    elif running_pos < 0:
        orders.append(Order(product, fair, -running_pos))

    return orders


def run_mean_revert(product: str, state: TradingState, ts: dict) -> List[Order]:
    """EMA + z-score mean reversion."""
    cfg = CONFIG["mean_revert"]
    p = dict(cfg["defaults"])
    p.update(cfg["params"].get(product, {}))

    od = state.order_depths[product]
    pos = state.position.get(product, 0)
    limit = p["position_limit"]
    mid = get_mid(od)
    if mid is None:
        return []

    # Update EMA and volatility
    ema_key = f"{product}_mr_ema"
    vol_key = f"{product}_mr_vol"

    fair = ema(ts.get(ema_key, mid), mid, p["ema_alpha"])
    dev = abs(mid - fair)
    vol = ema(ts.get(vol_key, dev + 1e-6), dev, p["vol_alpha"])
    vol = max(vol, 0.5)

    ts[ema_key] = fair
    ts[vol_key] = vol

    z = (mid - fair) / vol
    z = max(-3, min(3, z))
    fair_r = round(fair)

    orders: List[Order] = []
    running_pos = pos

    # Target position
    if abs(z) >= p["entry_z"]:
        frac = min(abs(z) / 3.0, 1.0)
        target = -round(frac * limit) if z > 0 else round(frac * limit)
    elif abs(z) < p["exit_z"]:
        target = 0
    else:
        target = pos

    delta = target - running_pos

    # Aggressive takes toward target
    if delta > 0:
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price <= fair_r:
                vol_avail = abs(od.sell_orders[ask_price])
                qty = min(vol_avail, delta, limit - running_pos)
                if qty > 0:
                    orders.append(Order(product, ask_price, qty))
                    running_pos += qty
                    delta -= qty
            else:
                break

    elif delta < 0:
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price >= fair_r:
                vol_avail = od.buy_orders[bid_price]
                qty = min(vol_avail, -delta, limit + running_pos)
                if qty > 0:
                    orders.append(Order(product, bid_price, -qty))
                    running_pos -= qty
                    delta += qty
            else:
                break

    # Passive quotes
    sp = p["passive_spread"]
    sz = p["passive_size"]

    buy_qty = min(sz, limit - running_pos)
    sell_qty = min(sz, limit + running_pos)

    if buy_qty > 0:
        orders.append(Order(product, fair_r - sp, buy_qty))
    if sell_qty > 0:
        orders.append(Order(product, fair_r + sp, -sell_qty))

    return orders


def run_momentum(product: str, state: TradingState, ts: dict) -> List[Order]:
    """Dual EMA crossover momentum with trailing stop."""
    cfg = CONFIG["momentum"]
    p = dict(cfg["defaults"])
    p.update(cfg["params"].get(product, {}))

    od = state.order_depths[product]
    pos = state.position.get(product, 0)
    limit = p["position_limit"]
    mid = get_mid(od)
    if mid is None:
        return []

    best_bid = max(od.buy_orders)
    best_ask = min(od.sell_orders)

    # EMAs
    a_fast = 2.0 / (p["fast_window"] + 1)
    a_slow = 2.0 / (p["slow_window"] + 1)

    fk = f"{product}_mo_fast"
    sk = f"{product}_mo_slow"
    pk = f"{product}_mo_peak"
    dk = f"{product}_mo_dir"

    fast = ema(ts.get(fk, mid), mid, a_fast)
    slow = ema(ts.get(sk, mid), mid, a_slow)
    ts[fk] = fast
    ts[sk] = slow

    gap = fast - slow
    prev_dir = ts.get(dk, 0)

    if gap > p["entry_threshold"]:
        direction = 1
    elif gap < -p["entry_threshold"]:
        direction = -1
    else:
        direction = 0 if abs(gap) < p["entry_threshold"] * 0.3 else prev_dir

    # Trailing stop
    trail = p["trailing_stop"]
    if trail > 0 and pos != 0:
        peak = ts.get(pk, mid)
        if pos > 0:
            peak = max(peak, mid)
            if mid < peak - trail:
                direction = 0
        else:
            peak = min(peak, mid)
            if mid > peak + trail:
                direction = 0
        ts[pk] = peak
    elif direction != 0:
        ts[pk] = mid

    ts[dk] = direction

    target = direction * limit if direction != 0 else 0
    delta = target - pos

    orders: List[Order] = []

    if delta > 0:
        for ask_price in sorted(od.sell_orders.keys()):
            vol = abs(od.sell_orders[ask_price])
            qty = min(vol, delta, limit - pos)
            if qty > 0:
                orders.append(Order(product, ask_price, qty))
                pos += qty
                delta -= qty
            if delta <= 0:
                break
        if delta > 0:
            orders.append(Order(product, best_ask, min(delta, limit - pos)))

    elif delta < 0:
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            vol = od.buy_orders[bid_price]
            qty = min(vol, -delta, limit + pos)
            if qty > 0:
                orders.append(Order(product, bid_price, -qty))
                pos -= qty
                delta += qty
            if delta >= 0:
                break
        if delta < 0:
            orders.append(Order(product, best_bid, max(delta, -(limit + pos))))

    return orders


# ============================================================
# MAIN TRADER CLASS
# ============================================================

class Trader:
    """Routes each product to its configured strategy."""

    def _build_routing_table(self) -> Dict[str, str]:
        """Map product → strategy name from CONFIG."""
        table = {}
        for strat_name in ["market_make", "mean_revert", "momentum", "informed"]:
            for product in CONFIG[strat_name].get("products", []):
                table[product] = strat_name
        return table

    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        orders: Dict[str, List[Order]] = {}
        conversions = 0
        ts = json.loads(state.traderData) if state.traderData else {}

        routing = self._build_routing_table()

        for product in state.order_depths:
            strategy = routing.get(product)

            if strategy == "market_make":
                orders[product] = run_market_make(product, state, ts)
            elif strategy == "mean_revert":
                orders[product] = run_mean_revert(product, state, ts)
            elif strategy == "momentum":
                orders[product] = run_momentum(product, state, ts)
            else:
                # Default: simple EMA market making for unknown products
                orders[product] = run_mean_revert(product, state, ts)

        # --- Pairs trading (handled separately as it spans products) ---
        for group in CONFIG["pairs"]["groups"]:
            basket = group["basket"]
            if basket not in state.order_depths:
                continue
            if not all(leg in state.order_depths for leg in group["legs"]):
                continue

            basket_od = state.order_depths[basket]
            basket_mid = get_mid(basket_od)
            if basket_mid is None:
                continue

            nav = 0
            skip = False
            for leg, w in group["legs"].items():
                lm = get_mid(state.order_depths[leg])
                if lm is None:
                    skip = True
                    break
                nav += w * lm
            if skip:
                continue

            pkey = f"{basket}_prem"
            prev_prem = ts.get(pkey, group.get("initial_premium", 0))
            alpha = group.get("premium_alpha", 0.001)
            premium = ema(prev_prem, basket_mid - nav, alpha)
            ts[pkey] = premium

            spread = (basket_mid - nav) - premium
            basket_pos = state.position.get(basket, 0)
            blimit = group.get("basket_limit", 60)

            basket_bid = max(basket_od.buy_orders) if basket_od.buy_orders else None
            basket_ask = min(basket_od.sell_orders) if basket_od.sell_orders else None

            entry = group.get("entry_threshold", 80)
            exit_thr = group.get("exit_threshold", 10)

            b_orders: List[Order] = []
            if spread > entry and basket_bid:
                qty = min(blimit + basket_pos, blimit)
                if qty > 0:
                    b_orders.append(Order(basket, basket_bid, -qty))
            elif spread < -entry and basket_ask:
                qty = min(blimit - basket_pos, blimit)
                if qty > 0:
                    b_orders.append(Order(basket, basket_ask, qty))
            elif abs(spread) < exit_thr:
                if basket_pos > 0 and basket_bid:
                    b_orders.append(Order(basket, basket_bid, -basket_pos))
                elif basket_pos < 0 and basket_ask:
                    b_orders.append(Order(basket, basket_ask, -basket_pos))

            orders[basket] = b_orders

        return orders, conversions, json.dumps(ts)
