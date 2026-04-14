"""
Trend-Following + Mean-Reversion Hybrid Strategy
==================================================
EMERALDS: Standard market-making (pegged at 10,000)
TOMATOES: Detect intraday trend with slow EMA, trade WITH it,
          use fast EMA deviations for entry timing (buy dips in
          uptrends, sell rallies in downtrends).
"""

import json
from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List


# ============================================================
# TUNABLE PARAMETERS
# ============================================================
TOMATO_PARAMS = {
    # -- Trend detection --
    "slow_ema_alpha": 0.005,      # slow EMA to capture intraday trend direction
    "fast_ema_alpha": 0.12,       # fast EMA to track price closely (for entry timing)
    "trend_threshold": 0.3,       # min slope (slow EMA change per tick) to declare a trend

    # -- Position sizing --
    "position_limit": 50,
    "trend_position_frac": 1.0,   # fraction of limit to hold in trend direction
    "neutral_position_frac": 0.0, # fraction when no trend detected

    # -- Entry timing (mean-reversion within trend) --
    "dip_entry_z": 0.8,           # z-score threshold: enter on dips/rallies within trend
    "vol_ema_alpha": 0.08,        # volatility tracking
    "max_z": 3.0,

    # -- Order placement --
    "aggressive_take": True,      # cross spread to enter
    "passive_spread": 3,          # passive order distance from fair
    "passive_size": 15,           # passive order size
    "counter_trend_spread": 5,    # wider spread for counter-trend passive orders
}


class Trader:
    def run(self, state: TradingState) -> tuple[Dict[str, List[Order]], int, str]:
        orders: Dict[str, List[Order]] = {}
        conversions = 0

        if state.traderData:
            ts = json.loads(state.traderData)
        else:
            ts = {}

        for product in state.order_depths:
            if product == "EMERALDS":
                orders[product] = self.trade_emeralds(state, product)
            elif product == "TOMATOES":
                orders[product] = self.trade_tomatoes(state, product, ts)

        return orders, conversions, json.dumps(ts)

    def trade_emeralds(self, state: TradingState, product: str) -> List[Order]:
        position = state.position.get(product, 0)
        limit = 50
        orders = []
        buy_qty = limit - position
        sell_qty = limit + position
        if buy_qty > 0:
            orders.append(Order(product, 9996, buy_qty))
        if sell_qty > 0:
            orders.append(Order(product, 10004, -sell_qty))
        return orders

    def trade_tomatoes(self, state: TradingState, product: str, ts: dict) -> List[Order]:
        p = TOMATO_PARAMS
        od = state.order_depths[product]
        position = state.position.get(product, 0)
        limit = p["position_limit"]

        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        if best_bid is None or best_ask is None:
            return []

        mid = (best_bid + best_ask) / 2

        # ---- Update EMAs ----
        fast_ema = p["fast_ema_alpha"] * mid + (1 - p["fast_ema_alpha"]) * ts.get("t_fast", mid)
        slow_ema = p["slow_ema_alpha"] * mid + (1 - p["slow_ema_alpha"]) * ts.get("t_slow", mid)
        prev_slow = ts.get("t_slow", mid)

        ts["t_fast"] = fast_ema
        ts["t_slow"] = slow_ema

        # ---- Volatility ----
        dev = abs(mid - fast_ema)
        vol = p["vol_ema_alpha"] * dev + (1 - p["vol_ema_alpha"]) * ts.get("t_vol", dev + 1e-6)
        vol = max(vol, 0.5)
        ts["t_vol"] = vol

        # ---- Trend detection ----
        # Slope of slow EMA (change per tick)
        slope = slow_ema - prev_slow

        # Accumulate slope over a window for a smoother trend signal
        slope_ema = 0.01 * slope + (1 - 0.01) * ts.get("t_slope", 0)
        ts["t_slope"] = slope_ema

        thr = p["trend_threshold"]
        if slope_ema > thr:
            trend = 1    # uptrend
        elif slope_ema < -thr:
            trend = -1   # downtrend
        else:
            trend = 0    # no clear trend

        # ---- Entry timing: z-score of fast EMA vs slow EMA ----
        signal = (fast_ema - slow_ema) / vol
        signal = max(-p["max_z"], min(p["max_z"], signal))

        # ---- Target position ----
        if trend != 0:
            base_target = round(trend * p["trend_position_frac"] * limit)

            # Dip/rally entry: scale up when price pulls back toward slow EMA
            # In uptrend: signal < 0 means price dipped below slow EMA = good buy
            # In downtrend: signal > 0 means price rallied above slow EMA = good sell
            if trend == 1 and signal < -p["dip_entry_z"]:
                # Strong dip in uptrend — go max long
                target = limit
            elif trend == -1 and signal > p["dip_entry_z"]:
                # Strong rally in downtrend — go max short
                target = -limit
            elif trend == 1 and signal > p["dip_entry_z"]:
                # Price way above slow EMA in uptrend — take some profit
                target = round(base_target * 0.5)
            elif trend == -1 and signal < -p["dip_entry_z"]:
                # Price way below slow EMA in downtrend — take some profit
                target = round(base_target * 0.5)
            else:
                target = base_target
        else:
            # No trend: small mean-reversion
            if abs(signal) > p["dip_entry_z"]:
                frac = min(abs(signal) / p["max_z"], 1.0)
                target = -round(frac * limit * 0.5) if signal > 0 else round(frac * limit * 0.5)
            else:
                target = 0

        # ---- Execute ----
        delta = target - position
        orders: List[Order] = []
        pos = position
        fair = round(fast_ema)

        # Aggressive takes
        if p["aggressive_take"] and delta != 0:
            if delta > 0:
                for ask_price in sorted(od.sell_orders.keys()):
                    if ask_price <= fair + (2 if trend == 1 else 0):
                        vol_avail = abs(od.sell_orders[ask_price])
                        qty = min(vol_avail, delta, limit - pos)
                        if qty > 0:
                            orders.append(Order(product, ask_price, qty))
                            pos += qty
                            delta -= qty
                    else:
                        break
            else:
                for bid_price in sorted(od.buy_orders.keys(), reverse=True):
                    if bid_price >= fair - (2 if trend == -1 else 0):
                        vol_avail = od.buy_orders[bid_price]
                        qty = min(vol_avail, -delta, limit + pos)
                        if qty > 0:
                            orders.append(Order(product, bid_price, -qty))
                            pos -= qty
                            delta += qty
                    else:
                        break

        # Passive orders — tighter on trend side, wider on counter-trend side
        ps = p["passive_spread"]
        cs = p["counter_trend_spread"]
        psz = p["passive_size"]

        if trend >= 0:
            # Favor buying
            buy_spread = ps
            sell_spread = cs if trend == 1 else ps
        else:
            # Favor selling
            buy_spread = cs
            sell_spread = ps

        # Passive for remaining delta
        if delta > 0:
            qty = min(delta, psz, limit - pos)
            if qty > 0:
                orders.append(Order(product, fair - buy_spread, qty))
        elif delta < 0:
            qty = min(-delta, psz, limit + pos)
            if qty > 0:
                orders.append(Order(product, fair + sell_spread, -qty))

        # Background liquidity
        remaining_buy = limit - pos - max(delta, 0)
        remaining_sell = limit + pos - max(-delta, 0)

        if remaining_buy > 0:
            qty = min(psz, remaining_buy)
            if qty > 0:
                orders.append(Order(product, fair - buy_spread - 1, qty))
        if remaining_sell > 0:
            qty = min(psz, remaining_sell)
            if qty > 0:
                orders.append(Order(product, fair + sell_spread + 1, -qty))

        return orders
