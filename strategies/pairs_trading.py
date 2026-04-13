"""
Pairs / Basket Arbitrage Strategy
==================================
Trades the spread between a basket (ETF) and its constituents,
or between any two correlated products.

Inspired by Frankfurt Hedgehogs' ETF arbitrage. Generalized to:
- Arbitrary number of legs with configurable weights
- Running spread premium estimation (EMA)
- Threshold-based entry/exit
- Optional delta hedging of individual legs
"""

import json
import math
from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List, Tuple, Optional

# ============================================================
# TUNABLE PARAMETERS
# ============================================================

# Define pairs/baskets as groups. Each group has:
#   "basket": the product that represents the basket/ETF
#   "legs": dict of {product: weight} that compose the basket's NAV
#   "premium_ema_alpha": how fast to adapt the running premium estimate
#   "initial_premium": starting estimate of basket - NAV
#   "entry_threshold": min |spread - premium| to open a trade
#   "exit_threshold": close when |spread - premium| falls below this
#   "hedge_factor": fraction of constituent hedge to apply (0-1)
#   "position_limit": limit for the basket product
#   "leg_limits": per-leg position limits

PAIR_GROUPS = [
    {
        "name": "BASKET1",
        "basket": "PICNIC_BASKET1",
        "legs": {
            "CROISSANTS": 6,
            "JAMS": 3,
            "DJEMBES": 1,
        },
        "position_limit": 60,
        "leg_limits": {
            "CROISSANTS": 250,
            "JAMS": 350,
            "DJEMBES": 60,
        },
        "initial_premium": 5,
        "premium_ema_alpha": 0.001,
        "premium_window": 60000,       # max samples for premium tracking
        "entry_threshold": 80,
        "exit_threshold": 10,
        "hedge_factor": 0.5,
        "close_at_zero": True,         # close position when spread crosses zero
    },
]

# For simple two-product pairs (no basket), use this format:
# {
#     "name": "PAIR_AB",
#     "basket": "PRODUCT_A",
#     "legs": {"PRODUCT_B": 1.0},  # weight = hedge ratio
#     "position_limit": 50,
#     "leg_limits": {"PRODUCT_B": 50},
#     "initial_premium": 0,
#     "premium_ema_alpha": 0.01,
#     "entry_threshold": 5,
#     "exit_threshold": 1,
#     "hedge_factor": 1.0,
#     "close_at_zero": True,
# }


def get_mid(od: OrderDepth) -> Optional[float]:
    if not od.buy_orders or not od.sell_orders:
        return None
    return (max(od.buy_orders) + min(od.sell_orders)) / 2


def get_best_bid_ask(od: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


class Trader:
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        orders: Dict[str, List[Order]] = {}
        conversions = 0
        trader_state = json.loads(state.traderData) if state.traderData else {}

        for group in PAIR_GROUPS:
            basket = group["basket"]
            if basket not in state.order_depths:
                continue

            # Check all legs are available
            all_available = all(
                leg in state.order_depths for leg in group["legs"]
            )
            if not all_available:
                continue

            # --- Compute NAV (net asset value of constituents) ---
            basket_od = state.order_depths[basket]
            basket_mid = get_mid(basket_od)
            if basket_mid is None:
                continue

            nav = 0
            leg_mids = {}
            skip = False
            for leg, weight in group["legs"].items():
                leg_mid = get_mid(state.order_depths[leg])
                if leg_mid is None:
                    skip = True
                    break
                leg_mids[leg] = leg_mid
                nav += weight * leg_mid
            if skip:
                continue

            # --- Compute spread and running premium ---
            raw_spread = basket_mid - nav
            pkey = f"{group['name']}_premium"
            pcount_key = f"{group['name']}_pcount"

            prev_premium = trader_state.get(pkey, group["initial_premium"])
            count = trader_state.get(pcount_key, 0)

            alpha = group["premium_ema_alpha"]
            premium = alpha * raw_spread + (1 - alpha) * prev_premium
            trader_state[pkey] = premium
            trader_state[pcount_key] = count + 1

            # Spread relative to estimated premium
            spread = raw_spread - premium

            # --- Trading logic ---
            basket_pos = state.position.get(basket, 0)
            basket_limit = group["position_limit"]

            basket_bid, basket_ask = get_best_bid_ask(basket_od)

            basket_orders: List[Order] = []

            entry_thr = group["entry_threshold"]
            exit_thr = group["exit_threshold"]
            close_at_zero = group.get("close_at_zero", False)

            if spread > entry_thr:
                # Basket overpriced → sell basket, buy legs
                qty = min(basket_limit + basket_pos, basket_limit)
                if qty > 0 and basket_bid is not None:
                    basket_orders.append(Order(basket, basket_bid, -qty))

            elif spread < -entry_thr:
                # Basket underpriced → buy basket, sell legs
                qty = min(basket_limit - basket_pos, basket_limit)
                if qty > 0 and basket_ask is not None:
                    basket_orders.append(Order(basket, basket_ask, qty))

            elif close_at_zero and abs(spread) < exit_thr:
                # Flatten basket position
                if basket_pos > 0 and basket_bid is not None:
                    basket_orders.append(Order(basket, basket_bid, -basket_pos))
                elif basket_pos < 0 and basket_ask is not None:
                    basket_orders.append(Order(basket, basket_ask, -basket_pos))

            if basket in orders:
                orders[basket].extend(basket_orders)
            else:
                orders[basket] = basket_orders

            # --- Hedge legs ---
            hedge_factor = group["hedge_factor"]
            for leg, weight in group["legs"].items():
                leg_pos = state.position.get(leg, 0)
                leg_limit = group["leg_limits"].get(leg, 50)
                leg_od = state.order_depths[leg]
                leg_bid, leg_ask = get_best_bid_ask(leg_od)

                # Desired hedge: opposite of basket position * weight * factor
                desired_leg_pos = round(-basket_pos * weight * hedge_factor)
                delta = desired_leg_pos - leg_pos
                delta = max(-leg_limit - leg_pos, min(leg_limit - leg_pos, delta))

                leg_orders: List[Order] = []
                if delta > 0 and leg_ask is not None:
                    leg_orders.append(Order(leg, leg_ask, delta))
                elif delta < 0 and leg_bid is not None:
                    leg_orders.append(Order(leg, leg_bid, delta))

                if leg in orders:
                    orders[leg].extend(leg_orders)
                else:
                    orders[leg] = leg_orders

        return orders, conversions, json.dumps(trader_state)
