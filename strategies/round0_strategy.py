import json
from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List


class Trader:
    def run(self, state: TradingState) -> tuple[Dict[str, List[Order]], int, str]:
        orders: Dict[str, List[Order]] = {}
        conversions = 0

        # Load persistent state
        if state.traderData:
            trader_state = json.loads(state.traderData)
        else:
            trader_state = {}

        for product in state.order_depths:
            if product == "EMERALDS":
                orders[product] = self.trade_emeralds(state, product)
            elif product == "TOMATOES":
                orders[product] = self.trade_tomatoes(state, product, trader_state)

        trader_data = json.dumps(trader_state)
        return orders, conversions, trader_data

    def trade_emeralds(self, state: TradingState, product: str) -> List[Order]:
        """
        EMERALDS: Stable around 10,000. The book almost always has:
          best bid = 9,992   best ask = 10,008

        Strategy: Market-make inside the spread.
          - Buy  at 9,996 (4 ticks better than best bid)
          - Sell at 10,004 (4 ticks better than best ask)
          - Profit = 8 per round trip

        Position management: scale order size based on current position
        to avoid hitting the limit (50) and to mean-revert position to 0.
        """
        position = state.position.get(product, 0)
        limit = 50

        buy_price = 9996
        sell_price = 10004

        # Scale quantity: buy more when position is negative, sell more when positive
        buy_qty = limit - position       # e.g. pos=0 -> buy 50, pos=40 -> buy 10
        sell_qty = limit + position       # e.g. pos=0 -> sell 50, pos=-40 -> sell 10

        product_orders = []

        if buy_qty > 0:
            product_orders.append(Order(product, buy_price, buy_qty))

        if sell_qty > 0:
            product_orders.append(Order(product, sell_price, -sell_qty))

        return product_orders

    def trade_tomatoes(self, state: TradingState, product: str, trader_state: dict) -> List[Order]:
        """
        TOMATOES: Mean-reverts around ~4,978 with range 4,946–5,011.
        Wide spread (~13), volatile.

        Strategy: Adaptive mean-reversion market-making.
          - Track a moving average of mid prices as fair value
          - Place buy orders below fair value, sell orders above
          - Spread of 2 on each side of fair value (tight enough to get filled)
          - Aggressively take any mispriced orders in the book
        """
        order_depth = state.order_depths[product]
        position = state.position.get(product, 0)
        limit = 50

        # Calculate current mid price
        best_bid = max(order_depth.buy_orders.keys()) if order_depth.buy_orders else None
        best_ask = min(order_depth.sell_orders.keys()) if order_depth.sell_orders else None

        if best_bid is None or best_ask is None:
            return []

        mid_price = (best_bid + best_ask) / 2

        # Update moving average (EMA with ~20 tick window)
        history = trader_state.get("tomato_ema", mid_price)
        alpha = 0.05  # ~20 tick half-life
        ema = alpha * mid_price + (1 - alpha) * history
        trader_state["tomato_ema"] = ema

        # Fair value = EMA, rounded to nearest int
        fair_value = round(ema)

        product_orders = []

        # 1) Take any sell orders priced below fair value (cheap — buy them)
        for ask_price in sorted(order_depth.sell_orders.keys()):
            if ask_price < fair_value:
                ask_vol = abs(order_depth.sell_orders[ask_price])
                buy_qty = min(ask_vol, limit - position)
                if buy_qty > 0:
                    product_orders.append(Order(product, ask_price, buy_qty))
                    position += buy_qty

        # 2) Take any buy orders priced above fair value (expensive — sell to them)
        for bid_price in sorted(order_depth.buy_orders.keys(), reverse=True):
            if bid_price > fair_value:
                bid_vol = order_depth.buy_orders[bid_price]
                sell_qty = min(bid_vol, limit + position)
                if sell_qty > 0:
                    product_orders.append(Order(product, bid_price, -sell_qty))
                    position -= sell_qty

        # 3) Place passive orders around fair value
        buy_price = fair_value - 2
        sell_price = fair_value + 2

        # Scale by position: buy more when short, sell more when long
        passive_buy_qty = min(20, limit - position)
        passive_sell_qty = min(20, limit + position)

        if passive_buy_qty > 0:
            product_orders.append(Order(product, buy_price, passive_buy_qty))

        if passive_sell_qty > 0:
            product_orders.append(Order(product, sell_price, -passive_sell_qty))

        return product_orders
