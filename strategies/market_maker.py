"""
Adaptive Market Making Strategy
================================
Generic market maker that quotes around a fair value estimate.
Works for any product — just tune the PARAMS dict.

Key ideas (from Frankfurt Hedgehogs' StaticTrader):
- Compute fair value from order book walls (deepest liquidity), not just best bid/ask
- Skew quotes based on inventory to mean-revert position toward zero
- Take profitable crosses immediately, then post passive orders
"""

import json
import math
from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List, Optional, Tuple

# ============================================================
# TUNABLE PARAMETERS — one dict per product
# ============================================================
PARAMS = {
    "EMERALDS": {
        "position_limit": 50,
        "fair_value_method": "static",    # "static" | "wall_mid" | "ema"
        "static_fair_value": 10000,       # used when method = "static"
        "ema_alpha": 0.1,                 # used when method = "ema"
        "half_spread": 4,                 # distance from fair value to quote
        "take_edge": 1,                   # min edge to aggressively take liquidity
        "inventory_skew": 1.0,            # how aggressively to skew for inventory (0=none, 1=linear, 2=quadratic)
        "passive_order_size": None,       # None = use full remaining capacity
        "flatten_at_fair": True,          # post flattening order exactly at fair value
    },
    # Example for a volatile product — override what differs
    "TOMATOES": {
        "position_limit": 50,
        "fair_value_method": "ema",
        "static_fair_value": None,
        "ema_alpha": 0.05,
        "half_spread": 2,
        "take_edge": 1,
        "inventory_skew": 1.0,
        "passive_order_size": 20,
        "flatten_at_fair": False,
    },
}

# Fallback defaults for any product not explicitly listed
DEFAULT_PARAMS = {
    "position_limit": 50,
    "fair_value_method": "wall_mid",
    "static_fair_value": None,
    "ema_alpha": 0.1,
    "half_spread": 3,
    "take_edge": 1,
    "inventory_skew": 1.0,
    "passive_order_size": None,
    "flatten_at_fair": False,
}


def get_params(product: str) -> dict:
    base = dict(DEFAULT_PARAMS)
    base.update(PARAMS.get(product, {}))
    return base


# ============================================================
# HELPERS
# ============================================================

def get_wall_mid(order_depth: OrderDepth) -> Optional[float]:
    """Mid-price between the deepest bid wall and deepest ask wall.
    More stable than best-bid/best-ask mid when the book is noisy."""
    if not order_depth.buy_orders or not order_depth.sell_orders:
        return None
    # Deepest = largest absolute volume
    bid_wall = max(order_depth.buy_orders.keys(),
                   key=lambda p: order_depth.buy_orders[p])
    ask_wall = min(order_depth.sell_orders.keys(),
                   key=lambda p: abs(order_depth.sell_orders[p]))
    return (bid_wall + ask_wall) / 2


def get_mid(order_depth: OrderDepth) -> Optional[float]:
    if not order_depth.buy_orders or not order_depth.sell_orders:
        return None
    return (max(order_depth.buy_orders) + min(order_depth.sell_orders)) / 2


# ============================================================
# TRADER
# ============================================================

class Trader:
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        orders: Dict[str, List[Order]] = {}
        conversions = 0

        trader_state = json.loads(state.traderData) if state.traderData else {}

        for product in state.order_depths:
            p = get_params(product)
            od = state.order_depths[product]
            position = state.position.get(product, 0)
            limit = p["position_limit"]

            # --- Compute fair value ---
            method = p["fair_value_method"]
            if method == "static":
                fair = p["static_fair_value"]
            elif method == "wall_mid":
                fair = get_wall_mid(od)
                if fair is None:
                    fair = get_mid(od)
            else:  # ema
                mid = get_mid(od)
                if mid is None:
                    continue
                ema_key = f"{product}_ema"
                prev = trader_state.get(ema_key, mid)
                alpha = p["ema_alpha"]
                fair = alpha * mid + (1 - alpha) * prev
                trader_state[ema_key] = fair

            if fair is None:
                continue

            fair_rounded = round(fair)
            product_orders: List[Order] = []
            pos = position  # track running position through order generation

            # --- 1. Aggressive takes: buy cheap sells, sell expensive bids ---
            take_edge = p["take_edge"]

            for ask_price in sorted(od.sell_orders.keys()):
                if ask_price < fair_rounded - take_edge + 1:
                    vol = abs(od.sell_orders[ask_price])
                    qty = min(vol, limit - pos)
                    if qty > 0:
                        product_orders.append(Order(product, ask_price, qty))
                        pos += qty
                else:
                    break

            for bid_price in sorted(od.buy_orders.keys(), reverse=True):
                if bid_price > fair_rounded + take_edge - 1:
                    vol = od.buy_orders[bid_price]
                    qty = min(vol, limit + pos)
                    if qty > 0:
                        product_orders.append(Order(product, bid_price, -qty))
                        pos -= qty
                else:
                    break

            # --- 2. Inventory skew ---
            skew = p["inventory_skew"]
            # Shift quotes away from the side we're overweight on
            inventory_ratio = pos / limit if limit else 0  # [-1, 1]
            bid_adjust = -round(skew * inventory_ratio)  # positive when short
            ask_adjust = -round(skew * inventory_ratio)   # negative when long

            # --- 3. Passive quotes ---
            hs = p["half_spread"]
            bid_price = fair_rounded - hs + bid_adjust
            ask_price = fair_rounded + hs + ask_adjust

            max_buy = limit - pos
            max_sell = limit + pos

            passive_size = p["passive_order_size"]
            buy_qty = min(passive_size, max_buy) if passive_size else max_buy
            sell_qty = min(passive_size, max_sell) if passive_size else max_sell

            if buy_qty > 0:
                product_orders.append(Order(product, bid_price, buy_qty))
            if sell_qty > 0:
                product_orders.append(Order(product, ask_price, -sell_qty))

            # --- 4. Flatten at fair (optional) ---
            if p["flatten_at_fair"] and pos != 0:
                if pos > 0:
                    product_orders.append(Order(product, fair_rounded, -pos))
                else:
                    product_orders.append(Order(product, fair_rounded, -pos))

            orders[product] = product_orders

        return orders, conversions, json.dumps(trader_state)
