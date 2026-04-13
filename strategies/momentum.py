"""
Momentum / Trend Following Strategy
=====================================
Detects short-term trends and rides them. Useful for products
that exhibit momentum (informed-trader driven moves, news-driven).

Key ideas:
- Dual EMA crossover to detect trend direction
- Volume-weighted price tracking for cleaner signals
- Trailing stop logic to lock in profits
- Configurable cooldown to avoid whipsaws
"""

import json
import math
from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List, Tuple

# ============================================================
# TUNABLE PARAMETERS
# ============================================================
PARAMS = {
    "SQUID_INK": {
        "position_limit": 50,
        "fast_window": 5,              # fast EMA period (ticks)
        "slow_window": 20,             # slow EMA period (ticks)
        "entry_threshold": 0.5,        # min EMA gap to enter (in price units)
        "exit_threshold": 0.0,         # close when gap narrows to this
        "trailing_stop": 5.0,          # trailing stop distance (0 = disabled)
        "cooldown_ticks": 0,           # min ticks between direction changes
        "size_mode": "full",           # "full" | "scaled" (scale by signal strength)
        "scale_cap": 3.0,              # max multiplier for signal strength
        "take_liquidity": True,        # aggressively cross the spread
        "passive_offset": 1,           # offset from best price for passive orders
    },
}

DEFAULT_PARAMS = {
    "position_limit": 50,
    "fast_window": 5,
    "slow_window": 20,
    "entry_threshold": 0.5,
    "exit_threshold": 0.0,
    "trailing_stop": 5.0,
    "cooldown_ticks": 0,
    "size_mode": "full",
    "scale_cap": 3.0,
    "take_liquidity": True,
    "passive_offset": 1,
}


def get_params(product: str) -> dict:
    base = dict(DEFAULT_PARAMS)
    base.update(PARAMS.get(product, {}))
    return base


def ema_alpha(window: int) -> float:
    return 2.0 / (window + 1)


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

            # --- Volume-weighted mid (optional refinement) ---
            bid_vol = sum(od.buy_orders.values())
            ask_vol = sum(abs(v) for v in od.sell_orders.values())
            total_vol = bid_vol + ask_vol
            if total_vol > 0:
                vwap_mid = (best_bid * ask_vol + best_ask * bid_vol) / total_vol
            else:
                vwap_mid = mid

            # --- Update EMAs ---
            key_fast = f"{product}_mom_fast"
            key_slow = f"{product}_mom_slow"
            key_peak = f"{product}_mom_peak"
            key_last_dir = f"{product}_mom_dir"
            key_dir_ts = f"{product}_mom_dir_ts"

            a_fast = ema_alpha(p["fast_window"])
            a_slow = ema_alpha(p["slow_window"])

            fast = a_fast * vwap_mid + (1 - a_fast) * trader_state.get(key_fast, vwap_mid)
            slow = a_slow * vwap_mid + (1 - a_slow) * trader_state.get(key_slow, vwap_mid)

            trader_state[key_fast] = fast
            trader_state[key_slow] = slow

            gap = fast - slow  # positive = uptrend, negative = downtrend

            # --- Determine signal ---
            prev_dir = trader_state.get(key_last_dir, 0)  # -1, 0, 1
            dir_ts = trader_state.get(key_dir_ts, 0)
            cooldown = p["cooldown_ticks"]

            if gap > p["entry_threshold"]:
                new_dir = 1
            elif gap < -p["entry_threshold"]:
                new_dir = -1
            elif abs(gap) < p["exit_threshold"]:
                new_dir = 0
            else:
                new_dir = prev_dir  # hold

            # Cooldown: don't flip direction too fast
            if new_dir != prev_dir and new_dir != 0:
                if state.timestamp - dir_ts < cooldown:
                    new_dir = 0  # go flat instead of flipping

            if new_dir != prev_dir:
                trader_state[key_last_dir] = new_dir
                trader_state[key_dir_ts] = state.timestamp

            # --- Trailing stop ---
            trailing = p["trailing_stop"]
            if trailing > 0 and position != 0:
                peak = trader_state.get(key_peak, mid)
                if position > 0:
                    peak = max(peak, mid)
                    if mid < peak - trailing:
                        new_dir = 0  # stop triggered, go flat
                else:
                    peak = min(peak, mid)
                    if mid > peak + trailing:
                        new_dir = 0
                trader_state[key_peak] = peak
            elif new_dir != 0:
                trader_state[key_peak] = mid

            # --- Target position ---
            if p["size_mode"] == "scaled":
                frac = min(abs(gap) / p["scale_cap"], 1.0)
            else:
                frac = 1.0

            if new_dir == 1:
                target = round(frac * limit)
            elif new_dir == -1:
                target = -round(frac * limit)
            else:
                target = 0

            desired_delta = target - position
            product_orders: List[Order] = []
            pos = position

            # --- Execute ---
            if p["take_liquidity"] and desired_delta != 0:
                if desired_delta > 0:
                    for ask_price in sorted(od.sell_orders.keys()):
                        vol = abs(od.sell_orders[ask_price])
                        qty = min(vol, desired_delta, limit - pos)
                        if qty > 0:
                            product_orders.append(Order(product, ask_price, qty))
                            pos += qty
                            desired_delta -= qty
                        if desired_delta <= 0:
                            break
                else:
                    for bid_price in sorted(od.buy_orders.keys(), reverse=True):
                        vol = od.buy_orders[bid_price]
                        qty = min(vol, -desired_delta, limit + pos)
                        if qty > 0:
                            product_orders.append(Order(product, bid_price, -qty))
                            pos -= qty
                            desired_delta += qty
                        if desired_delta >= 0:
                            break

            # Passive remainder
            offset = p["passive_offset"]
            if desired_delta > 0:
                qty = min(desired_delta, limit - pos)
                if qty > 0:
                    product_orders.append(Order(product, best_ask - offset, qty))
            elif desired_delta < 0:
                qty = min(-desired_delta, limit + pos)
                if qty > 0:
                    product_orders.append(Order(product, best_bid + offset, -qty))

            orders[product] = product_orders

        return orders, conversions, json.dumps(trader_state)
