"""
Mean Reversion Strategy
========================
Trades deviations from a moving fair value using configurable
EMA windows and z-score thresholds.

Works for any mean-reverting product. Supports:
- Dual EMA (fast/slow) for fair value + trend filter
- Z-score entry/exit thresholds
- Position-proportional sizing
- Aggressive taking + passive posting
"""

import json
import math
from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List, Tuple

# ============================================================
# TUNABLE PARAMETERS
# ============================================================
PARAMS = {
    "TOMATOES": {
        "position_limit": 50,
        "fast_ema_alpha": 0.15,       # fast EMA — tracks price closely
        "slow_ema_alpha": 0.02,       # slow EMA — anchors fair value
        "use_dual_ema": False,        # True: fair = slow EMA; signal = fast - slow
        "vol_ema_alpha": 0.05,        # EMA for tracking rolling volatility
        "entry_z": 1.0,               # z-score to open a position
        "exit_z": 0.3,                # z-score to close a position
        "max_z": 3.0,                 # cap z-score for sizing (avoid outlier blow-ups)
        "aggressive_take": True,      # take mispriced book orders
        "passive_spread": 2,          # distance from fair for passive orders
        "passive_size": 20,           # max passive order size
        "position_scale": True,       # scale size by z-score magnitude
    },
}

DEFAULT_PARAMS = {
    "position_limit": 50,
    "fast_ema_alpha": 0.1,
    "slow_ema_alpha": 0.02,
    "use_dual_ema": False,
    "vol_ema_alpha": 0.05,
    "entry_z": 1.0,
    "exit_z": 0.3,
    "max_z": 3.0,
    "aggressive_take": True,
    "passive_spread": 2,
    "passive_size": 20,
    "position_scale": True,
}


def get_params(product: str) -> dict:
    base = dict(DEFAULT_PARAMS)
    base.update(PARAMS.get(product, {}))
    return base


def ema_update(prev: float, new: float, alpha: float) -> float:
    return alpha * new + (1 - alpha) * prev


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

            if not od.buy_orders or not od.sell_orders:
                continue

            best_bid = max(od.buy_orders)
            best_ask = min(od.sell_orders)
            mid = (best_bid + best_ask) / 2

            # --- Update EMAs ---
            key_fast = f"{product}_fast_ema"
            key_slow = f"{product}_slow_ema"
            key_vol = f"{product}_vol_ema"

            fast_ema = ema_update(trader_state.get(key_fast, mid), mid, p["fast_ema_alpha"])
            slow_ema = ema_update(trader_state.get(key_slow, mid), mid, p["slow_ema_alpha"])

            # Rolling volatility: EMA of |price - fast_ema|
            deviation = abs(mid - fast_ema)
            vol = ema_update(trader_state.get(key_vol, deviation + 1e-6), deviation, p["vol_ema_alpha"])
            vol = max(vol, 0.5)  # floor to avoid division by tiny numbers

            trader_state[key_fast] = fast_ema
            trader_state[key_slow] = slow_ema
            trader_state[key_vol] = vol

            # --- Compute signal ---
            if p["use_dual_ema"]:
                fair = slow_ema
                signal = fast_ema - slow_ema  # positive = price above slow
            else:
                fair = fast_ema
                signal = mid - fair  # positive = price above fair

            z_score = signal / vol
            z_score = max(-p["max_z"], min(p["max_z"], z_score))  # clamp

            fair_rounded = round(fair)
            product_orders: List[Order] = []
            pos = position

            # --- Determine target position from z-score ---
            if abs(z_score) >= p["entry_z"]:
                # Mean reversion: sell when z high, buy when z low
                if p["position_scale"]:
                    frac = min(abs(z_score) / p["max_z"], 1.0)
                else:
                    frac = 1.0
                if z_score > 0:
                    target = -round(frac * limit)
                else:
                    target = round(frac * limit)
            elif abs(z_score) < p["exit_z"]:
                target = 0
            else:
                target = pos  # hold current position in dead zone

            desired_delta = target - pos

            # --- 1. Aggressive takes ---
            if p["aggressive_take"]:
                if desired_delta > 0:
                    # Want to buy — take cheap asks
                    for ask_price in sorted(od.sell_orders.keys()):
                        if ask_price <= fair_rounded:
                            vol_available = abs(od.sell_orders[ask_price])
                            qty = min(vol_available, desired_delta, limit - pos)
                            if qty > 0:
                                product_orders.append(Order(product, ask_price, qty))
                                pos += qty
                                desired_delta -= qty
                        else:
                            break

                elif desired_delta < 0:
                    # Want to sell — take expensive bids
                    for bid_price in sorted(od.buy_orders.keys(), reverse=True):
                        if bid_price >= fair_rounded:
                            vol_available = od.buy_orders[bid_price]
                            qty = min(vol_available, -desired_delta, limit + pos)
                            if qty > 0:
                                product_orders.append(Order(product, bid_price, -qty))
                                pos -= qty
                                desired_delta += qty
                        else:
                            break

            # --- 2. Passive orders for remaining desired delta ---
            spread = p["passive_spread"]
            psize = p["passive_size"]

            if desired_delta > 0:
                qty = min(desired_delta, psize, limit - pos)
                if qty > 0:
                    product_orders.append(Order(product, fair_rounded - spread, qty))
            elif desired_delta < 0:
                qty = min(-desired_delta, psize, limit + pos)
                if qty > 0:
                    product_orders.append(Order(product, fair_rounded + spread, -qty))

            # --- 3. Always post some passive liquidity for earning spread ---
            remaining_buy = limit - pos - max(desired_delta, 0)
            remaining_sell = limit + pos - max(-desired_delta, 0)

            if remaining_buy > 0:
                qty = min(psize, remaining_buy)
                if qty > 0:
                    product_orders.append(Order(product, fair_rounded - spread - 1, qty))
            if remaining_sell > 0:
                qty = min(psize, remaining_sell)
                if qty > 0:
                    product_orders.append(Order(product, fair_rounded + spread + 1, -qty))

            orders[product] = product_orders

        return orders, conversions, json.dumps(trader_state)
