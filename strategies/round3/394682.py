from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List, Tuple
import json
import math


class Trader:
    LIMITS = {
        "HYDROGEL_PACK": 200,
        "VELVETFRUIT_EXTRACT": 200,
    }

    PARAMS = {
        "HYDROGEL_PACK": {
            "anchor": 9991.0,
            "alpha": 0.12,
            "anchor_weight": 0.70,
            "take_width": 9.0,
            "make_width": 7.0,
            "skew": 0.030,
            "base_size": 28,
        },
        "VELVETFRUIT_EXTRACT": {
            "anchor": 5252.0,
            "alpha": 0.18,
            "anchor_weight": 0.25,
            "take_width": 3.0,
            "make_width": 2.0,
            "skew": 0.018,
            "base_size": 35,
        },
    }

    def run(self, state: TradingState):
        saved = self.load_state(state.traderData)
        result: Dict[str, List[Order]] = {}

        for product in self.LIMITS:
            depth = state.order_depths.get(product)
            if depth is None:
                continue

            position = state.position.get(product, 0)
            fair = self.estimate_fair(product, depth, saved)
            orders = self.trade_delta_one(product, depth, fair, position)
            if orders:
                result[product] = orders

        return result, 0, json.dumps(saved, separators=(",", ":"))

    def load_state(self, trader_data: str) -> Dict:
        if not trader_data:
            return {"ema": {}}
        try:
            data = json.loads(trader_data)
        except Exception:
            return {"ema": {}}
        if "ema" not in data:
            data["ema"] = {}
        return data

    def best_bid_ask(self, depth: OrderDepth) -> Tuple[int, int]:
        best_bid = max(depth.buy_orders) if depth.buy_orders else None
        best_ask = min(depth.sell_orders) if depth.sell_orders else None
        return best_bid, best_ask

    def microprice(self, depth: OrderDepth) -> float:
        best_bid, best_ask = self.best_bid_ask(depth)
        if best_bid is None and best_ask is None:
            return None
        if best_bid is None:
            return float(best_ask)
        if best_ask is None:
            return float(best_bid)

        bid_volume = abs(depth.buy_orders[best_bid])
        ask_volume = abs(depth.sell_orders[best_ask])
        if bid_volume + ask_volume == 0:
            return (best_bid + best_ask) / 2.0
        return (best_bid * ask_volume + best_ask * bid_volume) / (bid_volume + ask_volume)

    def estimate_fair(self, product: str, depth: OrderDepth, saved: Dict) -> float:
        params = self.PARAMS[product]
        observed = self.microprice(depth)
        previous = saved["ema"].get(product, params["anchor"])
        if observed is None:
            ema = previous
        else:
            ema = params["alpha"] * observed + (1.0 - params["alpha"]) * previous
        saved["ema"][product] = ema
        return params["anchor_weight"] * params["anchor"] + (1.0 - params["anchor_weight"]) * ema

    def trade_delta_one(self, product: str, depth: OrderDepth, fair: float, position: int) -> List[Order]:
        params = self.PARAMS[product]
        limit = self.LIMITS[product]
        orders: List[Order] = []
        pos = position

        for ask in sorted(depth.sell_orders):
            if ask > fair - params["take_width"]:
                break
            buyable = limit - pos
            if buyable <= 0:
                break
            qty = min(-depth.sell_orders[ask], buyable)
            if qty > 0:
                orders.append(Order(product, ask, qty))
                pos += qty

        for bid in sorted(depth.buy_orders, reverse=True):
            if bid < fair + params["take_width"]:
                break
            sellable = limit + pos
            if sellable <= 0:
                break
            qty = min(depth.buy_orders[bid], sellable)
            if qty > 0:
                orders.append(Order(product, bid, -qty))
                pos -= qty

        best_bid, best_ask = self.best_bid_ask(depth)
        if best_bid is None or best_ask is None:
            return orders

        inventory_adjusted_fair = fair - params["skew"] * pos
        bid_quote = math.floor(inventory_adjusted_fair - params["make_width"])
        ask_quote = math.ceil(inventory_adjusted_fair + params["make_width"])
        bid_quote = min(bid_quote, best_ask - 1)
        ask_quote = max(ask_quote, best_bid + 1)

        buy_size = min(params["base_size"], limit - pos)
        sell_size = min(params["base_size"], limit + pos)
        if buy_size > 0 and bid_quote < best_ask:
            orders.append(Order(product, int(bid_quote), int(buy_size)))
        if sell_size > 0 and ask_quote > best_bid:
            orders.append(Order(product, int(ask_quote), -int(sell_size)))

        return orders