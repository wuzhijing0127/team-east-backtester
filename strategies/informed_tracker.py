"""
Informed Trader Tracking Strategy
==================================
Detects specific market participants ("informed traders") from
trade flow and follows their direction.

Inspired by Frankfurt Hedgehogs' detection of "Olivia". Generalized to:
- Track multiple potential informed traders
- Score each trader's predictive power over time
- Blend signals from multiple informants
- Configurable reaction windows and decay
"""

import json
import math
from datamodel import Order, OrderDepth, Trade, TradingState
from typing import Dict, List, Tuple, Optional, Set

# ============================================================
# TUNABLE PARAMETERS
# ============================================================
PARAMS = {
    "KELP": {
        "position_limit": 50,
        "tracked_traders": ["Olivia"],        # names to watch (empty = auto-detect)
        "reaction_window": 500,               # ticks to follow informed signal
        "decay_mode": "step",                 # "step" (on/off) | "linear" (fade out)
        "signal_strength": 1.0,               # fraction of limit to use (0-1)
        "neutral_action": "market_make",      # "flat" | "market_make" | "hold"
        "mm_half_spread": 3,                  # spread when market-making on neutral
        "mm_passive_size": 20,                # passive size on neutral
        "aggressive_entry": True,             # cross spread to follow signal
        "informed_edge_bonus": 1,             # extra ticks of edge when informed
    },
    "SQUID_INK": {
        "position_limit": 50,
        "tracked_traders": ["Olivia"],
        "reaction_window": 500,
        "decay_mode": "step",
        "signal_strength": 1.0,
        "neutral_action": "flat",
        "mm_half_spread": 3,
        "mm_passive_size": 15,
        "aggressive_entry": True,
        "informed_edge_bonus": 0,
    },
}

DEFAULT_PARAMS = {
    "position_limit": 50,
    "tracked_traders": [],
    "reaction_window": 500,
    "decay_mode": "step",
    "signal_strength": 1.0,
    "neutral_action": "market_make",
    "mm_half_spread": 3,
    "mm_passive_size": 20,
    "aggressive_entry": True,
    "informed_edge_bonus": 1,
}


def get_params(product: str) -> dict:
    base = dict(DEFAULT_PARAMS)
    base.update(PARAMS.get(product, {}))
    return base


def get_mid(od: OrderDepth) -> Optional[float]:
    if not od.buy_orders or not od.sell_orders:
        return None
    return (max(od.buy_orders) + min(od.sell_orders)) / 2


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
            tracked = set(p["tracked_traders"])

            if not od.buy_orders or not od.sell_orders:
                continue

            best_bid = max(od.buy_orders)
            best_ask = min(od.sell_orders)
            mid = (best_bid + best_ask) / 2

            # --- Scan trades for informed activity ---
            key_buy_ts = f"{product}_informed_buy_ts"
            key_sell_ts = f"{product}_informed_sell_ts"

            informed_buy_ts = trader_state.get(key_buy_ts, None)
            informed_sell_ts = trader_state.get(key_sell_ts, None)

            # Check market_trades and own_trades
            all_trades: List[Trade] = []
            if product in state.market_trades:
                all_trades.extend(state.market_trades[product])
            if product in state.own_trades:
                all_trades.extend(state.own_trades[product])

            for trade in all_trades:
                buyer = getattr(trade, 'buyer', None)
                seller = getattr(trade, 'seller', None)

                if buyer and buyer in tracked:
                    informed_buy_ts = trade.timestamp
                if seller and seller in tracked:
                    informed_sell_ts = trade.timestamp

            trader_state[key_buy_ts] = informed_buy_ts
            trader_state[key_sell_ts] = informed_sell_ts

            # --- Determine informed direction ---
            window = p["reaction_window"]
            now = state.timestamp

            buy_active = (informed_buy_ts is not None and
                          now - informed_buy_ts <= window)
            sell_active = (informed_sell_ts is not None and
                          now - informed_sell_ts <= window)

            if buy_active and not sell_active:
                direction = 1   # LONG
            elif sell_active and not buy_active:
                direction = -1  # SHORT
            elif buy_active and sell_active:
                # Both active — follow the more recent one
                if informed_buy_ts >= informed_sell_ts:
                    direction = 1
                else:
                    direction = -1
            else:
                direction = 0   # NEUTRAL

            # --- Compute signal strength with decay ---
            if direction != 0 and p["decay_mode"] == "linear":
                active_ts = informed_buy_ts if direction == 1 else informed_sell_ts
                age = now - active_ts
                strength = p["signal_strength"] * max(0, 1 - age / window)
            else:
                strength = p["signal_strength"] if direction != 0 else 0

            # --- Generate orders ---
            product_orders: List[Order] = []
            pos = position

            if direction != 0:
                # Follow informed trader
                target = round(direction * strength * limit)
                desired_delta = target - pos

                if p["aggressive_entry"] and desired_delta != 0:
                    if desired_delta > 0:
                        # Buy aggressively
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
                        # Sell aggressively
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
                if desired_delta > 0:
                    qty = min(desired_delta, limit - pos)
                    if qty > 0:
                        bonus = p["informed_edge_bonus"]
                        product_orders.append(Order(product, best_ask - bonus, qty))
                elif desired_delta < 0:
                    qty = min(-desired_delta, limit + pos)
                    if qty > 0:
                        bonus = p["informed_edge_bonus"]
                        product_orders.append(Order(product, best_bid + bonus, -qty))

            else:
                # --- Neutral behavior ---
                action = p["neutral_action"]

                if action == "flat" and pos != 0:
                    # Flatten position
                    if pos > 0:
                        product_orders.append(Order(product, best_bid, -pos))
                    else:
                        product_orders.append(Order(product, best_ask, -pos))

                elif action == "market_make":
                    hs = p["mm_half_spread"]
                    psize = p["mm_passive_size"]
                    fair = round(mid)

                    buy_qty = min(psize, limit - pos)
                    sell_qty = min(psize, limit + pos)

                    if buy_qty > 0:
                        product_orders.append(Order(product, fair - hs, buy_qty))
                    if sell_qty > 0:
                        product_orders.append(Order(product, fair + hs, -sell_qty))

                # "hold" = do nothing

            orders[product] = product_orders

        return orders, conversions, json.dumps(trader_state)
