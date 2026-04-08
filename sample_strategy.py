from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List


class Trader:
    """
    Sample strategy template.

    Replace the logic in run() with your own trading strategy.
    This example does nothing — it returns empty orders every tick.
    """

    def run(self, state: TradingState) -> tuple[Dict[str, List[Order]], int, str]:
        orders: Dict[str, List[Order]] = {}
        conversions = 0
        trader_data = ""

        for product in state.order_depths:
            order_depth: OrderDepth = state.order_depths[product]
            product_orders: List[Order] = []

            # ─── YOUR STRATEGY LOGIC HERE ───────────────────────────
            #
            # Available data:
            #   state.order_depths[product]   — current order book (buy_orders, sell_orders)
            #   state.position.get(product, 0) — your current position
            #   state.own_trades.get(product, []) — your fills from last tick
            #   state.market_trades.get(product, []) — other participants' trades
            #   state.observations             — ConversionObservation data (if available)
            #   state.traderData               — string you returned last tick (use for persistence)
            #   state.timestamp                — current timestamp
            #
            # To place orders:
            #   product_orders.append(Order(product, price, quantity))
            #     quantity > 0 = buy, quantity < 0 = sell
            #
            # Example — buy at best bid, sell at best ask:
            #   if order_depth.buy_orders:
            #       best_bid = max(order_depth.buy_orders.keys())
            #       product_orders.append(Order(product, best_bid, 1))
            #   if order_depth.sell_orders:
            #       best_ask = min(order_depth.sell_orders.keys())
            #       product_orders.append(Order(product, best_ask, -1))
            #
            # ─────────────────────────────────────────────────────────

            orders[product] = product_orders

        return orders, conversions, trader_data
