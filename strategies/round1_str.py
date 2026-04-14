"""
Round 1 Strategy v2 — Hardened
===============================
ASH_COATED_OSMIUM:  Anchored market maker (fair ≈ 10,000, stationary)
INTARIAN_PEPPER_ROOT: Drift-rider with improvements:
  1. Use actual day_base_map instead of estimating from mid
  2. Sanity check fair vs market before aggressive buys
  3. Drift detection (don't assume uptrend blindly)
  4. Tighter buy threshold (fair, not fair+1) to reduce overpaying
  5. Smarter re-entry: wait for price to return toward fair after selling
"""

import json
import math
from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List, Tuple, Optional


# ============================================================
# TUNABLE PARAMETERS
# ============================================================

ASH_PARAMS = {
    "position_limit": 50,
    "anchor_fair": 10000,
    "micro_beta": 0.1,
    "take_edge": 2,
    "half_spread": 5,
    "k_inv": 2.5,
    "base_size": 15,
    "flatten_size": 10,
    "tier_medium": 0.4,
    "tier_high": 0.7,
    "tier_extreme": 0.9,
}

PEPPER_PARAMS = {
    "position_limit": 50,
    "fair_slope": 0.001,
    "day_base_map": {-2: 9998, -1: 10998, 0: 11998},
    # Aggressive buy/sell thresholds
    "buy_edge": 0,                # buy at fair + buy_edge (0 = at fair exactly)
    "take_profit_edge": 4,        # sell when bid > fair + this
    "min_long_frac": 0.3,         # always keep at least this fraction long
    # Passive quoting
    "bid_spread": 4,              # passive bid distance from fair
    "ask_spread": 8,              # passive ask distance from fair
    "base_size": 10,
    # Drift detection
    "drift_ema_alpha": 0.01,      # EMA for tracking drift rate
    "drift_threshold": 0.0003,    # min drift rate to consider "trending"
    # Safety
    "fair_sanity_max_dev": 20,    # max allowed |mid - fair| before distrusting fair
    "inventory_tiers": {
        "medium": 0.4,
        "high": 0.6,
        "extreme": 0.85,
    },
}


# ============================================================
# HELPERS
# ============================================================

def get_best_bid_ask(od: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def get_mid(od: OrderDepth) -> Optional[float]:
    bid, ask = get_best_bid_ask(od)
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def get_microprice(od: OrderDepth) -> Optional[float]:
    bid, ask = get_best_bid_ask(od)
    if bid is None or ask is None:
        return None
    bid_vol = od.buy_orders[bid]
    ask_vol = abs(od.sell_orders[ask])
    total = bid_vol + ask_vol
    if total == 0:
        return (bid + ask) / 2
    return (bid * ask_vol + ask * bid_vol) / total


def inventory_size_multiplier(position: int, limit: int, tiers: dict) -> Tuple[float, float]:
    frac = abs(position) / limit if limit else 0

    if frac >= tiers.get("extreme", 0.9):
        add_mult = 0.0
    elif frac >= tiers.get("high", 0.7):
        add_mult = 0.25
    elif frac >= tiers.get("medium", 0.4):
        add_mult = 0.5
    else:
        add_mult = 1.0

    if position > 0:
        return add_mult, 1.0
    elif position < 0:
        return 1.0, add_mult
    else:
        return 1.0, 1.0


# ============================================================
# ASH_COATED_OSMIUM — Anchored Market Maker
# ============================================================

def trade_ash(state: TradingState, ts: dict) -> List[Order]:
    product = "ASH_COATED_OSMIUM"
    p = ASH_PARAMS
    od = state.order_depths.get(product)
    if od is None:
        return []

    position = state.position.get(product, 0)
    limit = p["position_limit"]
    best_bid, best_ask = get_best_bid_ask(od)

    fair = p["anchor_fair"]
    micro = get_microprice(od)
    if micro is not None:
        mid = get_mid(od)
        if mid is not None:
            fair = fair + p["micro_beta"] * (micro - mid)

    fair_r = round(fair)
    orders: List[Order] = []
    pos = position

    # Aggressive takes
    take_edge = p["take_edge"]
    if best_ask is not None:
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price <= fair_r - take_edge:
                vol = abs(od.sell_orders[ask_price])
                qty = min(vol, limit - pos)
                if qty > 0:
                    orders.append(Order(product, ask_price, qty))
                    pos += qty
            else:
                break

    if best_bid is not None:
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price >= fair_r + take_edge:
                vol = od.buy_orders[bid_price]
                qty = min(vol, limit + pos)
                if qty > 0:
                    orders.append(Order(product, bid_price, -qty))
                    pos -= qty
            else:
                break

    # Passive quotes with reservation pricing
    reservation = fair - p["k_inv"] * (pos / limit)
    res_r = round(reservation)
    hs = p["half_spread"]
    bid_price = res_r - hs
    ask_price = res_r + hs

    buy_mult, sell_mult = inventory_size_multiplier(pos, limit, {
        "medium": p["tier_medium"], "high": p["tier_high"], "extreme": p["tier_extreme"]
    })
    buy_qty = min(round(p["base_size"] * buy_mult), limit - pos)
    sell_qty = min(round(p["base_size"] * sell_mult), limit + pos)

    if best_ask is not None:
        bid_price = min(bid_price, best_ask - 1)
    if best_bid is not None:
        ask_price = max(ask_price, best_bid + 1)

    if buy_qty > 0:
        orders.append(Order(product, bid_price, buy_qty))
    if sell_qty > 0:
        orders.append(Order(product, ask_price, -sell_qty))

    # Flatten near limits
    if abs(pos) >= p["tier_extreme"] * limit:
        if pos > 0 and best_bid is not None:
            orders.append(Order(product, fair_r, -min(p["flatten_size"], pos)))
        elif pos < 0 and best_ask is not None:
            orders.append(Order(product, fair_r, min(p["flatten_size"], -pos)))

    return orders


# ============================================================
# INTARIAN_PEPPER_ROOT — Drift Rider (Hardened)
# ============================================================

def trade_pepper(state: TradingState, ts: dict) -> List[Order]:
    product = "INTARIAN_PEPPER_ROOT"
    p = PEPPER_PARAMS
    od = state.order_depths.get(product)
    if od is None:
        return []

    position = state.position.get(product, 0)
    limit = p["position_limit"]
    best_bid, best_ask = get_best_bid_ask(od)
    mid = get_mid(od)

    if mid is None or best_bid is None or best_ask is None:
        return []

    timestamp = state.timestamp

    # ==========================================================
    # FIX 1: Use actual day_base_map, not mid estimation
    # ==========================================================
    day_base = ts.get("pepper_day_base")
    if day_base is None:
        # Match to closest known day base
        best_dist = float('inf')
        for day, base in p["day_base_map"].items():
            expected = base + p["fair_slope"] * timestamp
            dist = abs(mid - expected)
            if dist < best_dist:
                best_dist = dist
                day_base = base
        ts["pepper_day_base"] = day_base

    fair = day_base + p["fair_slope"] * timestamp
    fair_r = round(fair)

    # ==========================================================
    # FIX 2: Sanity check fair vs market
    # ==========================================================
    fair_dev = abs(mid - fair)
    fair_trusted = fair_dev < p["fair_sanity_max_dev"]

    # ==========================================================
    # FIX 3: Drift detection — don't assume uptrend blindly
    # ==========================================================
    prev_mid = ts.get("pepper_prev_mid", mid)
    ts["pepper_prev_mid"] = mid

    drift_rate = mid - prev_mid  # instantaneous price change
    drift_ema = ts.get("pepper_drift_ema", p["fair_slope"] * 100)  # init with expected
    drift_ema = p["drift_ema_alpha"] * drift_rate + (1 - p["drift_ema_alpha"]) * drift_ema
    ts["pepper_drift_ema"] = drift_ema

    # Classify regime
    if drift_ema > p["drift_threshold"]:
        regime = 1    # uptrend — be long
    elif drift_ema < -p["drift_threshold"]:
        regime = -1   # downtrend — be short
    else:
        regime = 0    # no clear trend — be neutral / MM only

    orders: List[Order] = []
    pos = position

    if regime == 1 and fair_trusted:
        # ==========================================================
        # UPTREND: ride the drift long
        # ==========================================================

        # FIX 4: Buy at fair (not fair+1) to reduce overpaying
        buy_edge = p["buy_edge"]
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price <= fair_r + buy_edge:
                vol = abs(od.sell_orders[ask_price])
                qty = min(vol, limit - pos)
                if qty > 0:
                    orders.append(Order(product, ask_price, qty))
                    pos += qty
            else:
                break

        # Take profit on spikes
        take_edge = p["take_profit_edge"]
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price > fair_r + take_edge:
                vol = od.buy_orders[bid_price]
                min_hold = round(limit * p["min_long_frac"])
                max_sell = max(0, pos - min_hold)
                qty = min(vol, max_sell)
                if qty > 0:
                    orders.append(Order(product, bid_price, -qty))
                    pos -= qty
            else:
                break

        # Passive: aggressive bid to rebuild, conservative ask
        remaining_buy = limit - pos
        if remaining_buy > 0:
            bid_price = min(fair_r - p["bid_spread"], best_ask - 1)
            orders.append(Order(product, bid_price, remaining_buy))

        # FIX 5: Only post ask when well above target
        min_hold = round(limit * p["min_long_frac"])
        if pos > round(limit * 0.5):
            sell_qty = min(p["base_size"], pos - min_hold)
            if sell_qty > 0:
                ask_price = max(fair_r + p["ask_spread"], best_bid + 1)
                orders.append(Order(product, ask_price, -sell_qty))

    elif regime == -1 and fair_trusted:
        # ==========================================================
        # DOWNTREND: mirror logic — be short
        # ==========================================================

        # Aggressively sell at or above fair
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price >= fair_r - p["buy_edge"]:
                vol = od.buy_orders[bid_price]
                qty = min(vol, limit + pos)
                if qty > 0:
                    orders.append(Order(product, bid_price, -qty))
                    pos -= qty
            else:
                break

        # Buy back on dips (take profit)
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price < fair_r - p["take_profit_edge"]:
                vol = abs(od.sell_orders[ask_price])
                min_short = round(limit * p["min_long_frac"])
                max_buy = max(0, -pos - min_short)
                qty = min(vol, max_buy)
                if qty > 0:
                    orders.append(Order(product, ask_price, qty))
                    pos += qty
            else:
                break

        # Passive: aggressive ask, conservative bid
        remaining_sell = limit + pos
        if remaining_sell > 0:
            ask_price = max(fair_r + p["bid_spread"], best_bid + 1)
            orders.append(Order(product, ask_price, -remaining_sell))

        min_short = round(limit * p["min_long_frac"])
        if -pos > round(limit * 0.5):
            buy_qty = min(p["base_size"], -pos - min_short)
            if buy_qty > 0:
                bid_price = min(fair_r - p["ask_spread"], best_ask - 1)
                orders.append(Order(product, bid_price, buy_qty))

    else:
        # ==========================================================
        # NEUTRAL / UNTRUSTED FAIR: conservative MM around mid
        # ==========================================================
        tiers = p["inventory_tiers"]
        buy_mult, sell_mult = inventory_size_multiplier(pos, limit, tiers)

        hs = 5
        bid_price = min(fair_r - hs, best_ask - 1)
        ask_price = max(fair_r + hs, best_bid + 1)

        buy_qty = min(round(p["base_size"] * buy_mult), limit - pos)
        sell_qty = min(round(p["base_size"] * sell_mult), limit + pos)

        if buy_qty > 0:
            orders.append(Order(product, bid_price, buy_qty))
        if sell_qty > 0:
            orders.append(Order(product, ask_price, -sell_qty))

    return orders


# ============================================================
# MAIN TRADER
# ============================================================

class Trader:
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        orders: Dict[str, List[Order]] = {}
        conversions = 0
        ts = json.loads(state.traderData) if state.traderData else {}

        for product in state.order_depths:
            if product == "ASH_COATED_OSMIUM":
                orders[product] = trade_ash(state, ts)
            elif product == "INTARIAN_PEPPER_ROOT":
                orders[product] = trade_pepper(state, ts)

        return orders, conversions, json.dumps(ts)
