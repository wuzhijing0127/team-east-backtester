# Auto-generated strategy — optimizer variant
# Params: ash_L1_size=8, ash_k_inv=1.0, ash_take_edge_sell=4

import json
import math
from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List, Tuple, Optional


ASH_PARAMS = {
    "position_limit": 50,
    "anchor_fair": 10000,
    "micro_beta": 0.0,
    "take_edge_buy": 2,
    "take_edge_sell": 4,
    "k_inv": 1.0,
    "flatten_size": 10,
    "tier_medium": 0.4,
    "tier_high": 0.7,
    "tier_extreme": 1.0,
    "wide_spread_thr": 8,
    "narrow_spread_thr": 4,
    "L1_size": 8,
    "L1_base_spread": 3,
    "L2_spread": 4,
    "L2_size": 0,
    "L3_spread": 8,
    "L3_size": 0,
    "inv_bias_thr": 0.3,
}

PEPPER_PARAMS = {
    "position_limit": 50,
    "fair_slope": 0.001,
    "day_base_map": {-2: 9998, -1: 10998, 0: 11998},
    "buy_edge": 0,
    "dip_buy_edge": 3,
    "dip_threshold": 3,
    "take_profit_edge": 4,
    "min_long_frac": 0.96,
    "bid_spread": 4,
    "ask_spread": 8,
    "base_size": 10,
    "fair_sanity_max_dev": 20,
    "inventory_tiers": {
        "medium": 0.4,
        "high": 0.6,
        "extreme": 0.85,
    },
    "wide_spread_thr": 8,
    "narrow_spread_thr": 25,
    "overlay_sell_thr": 0.5,
}


def get_best_bid_ask(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def get_mid(od):
    bid, ask = get_best_bid_ask(od)
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def get_microprice(od):
    bid, ask = get_best_bid_ask(od)
    if bid is None or ask is None:
        return None
    bid_vol = od.buy_orders[bid]
    ask_vol = abs(od.sell_orders[ask])
    total = bid_vol + ask_vol
    if total == 0:
        return (bid + ask) / 2
    return (bid * ask_vol + ask * bid_vol) / total


def inventory_size_multiplier(position, limit, tiers):
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


def spread_adaptive_quote(fair_r, best_bid, best_ask, base_spread, wide_thr, narrow_thr, bias=0):
    mkt_spread = best_ask - best_bid
    if mkt_spread <= narrow_thr:
        bid_price = best_bid
        ask_price = best_ask
    elif mkt_spread >= wide_thr:
        bid_price = best_bid + 1
        ask_price = best_ask - 1
    else:
        bid_price = max(fair_r - base_spread, best_bid + 1)
        ask_price = min(fair_r + base_spread, best_ask - 1)
        bid_price = max(bid_price, best_bid)
        ask_price = min(ask_price, best_ask)
    if bias > 0:
        bid_price = min(bid_price + 1, best_ask - 1)
    elif bias < 0:
        ask_price = max(ask_price - 1, best_bid + 1)
    bid_price = min(bid_price, best_ask - 1)
    ask_price = max(ask_price, best_bid + 1)
    return bid_price, ask_price


def trade_ash(state, ts):
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
    orders = []
    pos = position
    if best_ask is not None:
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price <= fair_r - p["take_edge_buy"]:
                vol = abs(od.sell_orders[ask_price])
                qty = min(vol, limit - pos)
                if qty > 0:
                    orders.append(Order(product, ask_price, qty))
                    pos += qty
            else:
                break
    if best_bid is not None:
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price >= fair_r + p["take_edge_sell"]:
                vol = od.buy_orders[bid_price]
                qty = min(vol, limit + pos)
                if qty > 0:
                    orders.append(Order(product, bid_price, -qty))
                    pos -= qty
            else:
                break
    reservation = fair - p["k_inv"] * (pos / limit)
    res_r = round(reservation)
    tiers = {"medium": p["tier_medium"], "high": p["tier_high"], "extreme": p["tier_extreme"]}
    buy_mult, sell_mult = inventory_size_multiplier(pos, limit, tiers)
    if best_bid is not None and best_ask is not None:
        inv_bias = -1 if pos > limit * p["inv_bias_thr"] else (1 if pos < -limit * p["inv_bias_thr"] else 0)
        remaining_buy = limit - pos
        remaining_sell = limit + pos
        l1_buy = min(round(p["L1_size"] * buy_mult), remaining_buy)
        l1_sell = min(round(p["L1_size"] * sell_mult), remaining_sell)
        l1_bid, l1_ask = spread_adaptive_quote(
            res_r, best_bid, best_ask,
            p["L1_base_spread"], p["wide_spread_thr"], p["narrow_spread_thr"],
            bias=inv_bias,
        )
        if l1_buy > 0:
            orders.append(Order(product, l1_bid, l1_buy))
            remaining_buy -= l1_buy
        if l1_sell > 0:
            orders.append(Order(product, l1_ask, -l1_sell))
            remaining_sell -= l1_sell
        l2_bid = min(res_r - p["L2_spread"], l1_bid - 1)
        l2_ask = max(res_r + p["L2_spread"], l1_ask + 1)
        l2_bid = min(l2_bid, best_ask - 1)
        l2_ask = max(l2_ask, best_bid + 1)
        l2_buy = min(round(p["L2_size"] * buy_mult), remaining_buy)
        l2_sell = min(round(p["L2_size"] * sell_mult), remaining_sell)
        if l2_buy > 0:
            orders.append(Order(product, l2_bid, l2_buy))
            remaining_buy -= l2_buy
        if l2_sell > 0:
            orders.append(Order(product, l2_ask, -l2_sell))
            remaining_sell -= l2_sell
        l3_bid = min(res_r - p["L3_spread"], l2_bid - 1)
        l3_ask = max(res_r + p["L3_spread"], l2_ask + 1)
        l3_bid = min(l3_bid, best_ask - 1)
        l3_ask = max(l3_ask, best_bid + 1)
        l3_buy = min(round(p["L3_size"] * buy_mult), remaining_buy)
        l3_sell = min(round(p["L3_size"] * sell_mult), remaining_sell)
        if l3_buy > 0:
            orders.append(Order(product, l3_bid, l3_buy))
        if l3_sell > 0:
            orders.append(Order(product, l3_ask, -l3_sell))
    if abs(pos) >= p["tier_extreme"] * limit:
        if pos > 0 and best_bid is not None:
            orders.append(Order(product, fair_r, -min(p["flatten_size"], pos)))
        elif pos < 0 and best_ask is not None:
            orders.append(Order(product, fair_r, min(p["flatten_size"], -pos)))
    return orders


def trade_pepper(state, ts):
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
    day_base = ts.get("pepper_day_base")
    if day_base is None:
        best_dist = float("inf")
        for _, base in p["day_base_map"].items():
            expected = base + p["fair_slope"] * timestamp
            dist = abs(mid - expected)
            if dist < best_dist:
                best_dist = dist
                day_base = base
        ts["pepper_day_base"] = day_base
    fair = day_base + p["fair_slope"] * timestamp
    fair_r = round(fair)
    dev = mid - fair
    if abs(dev) >= p["fair_sanity_max_dev"]:
        return []
    orders = []
    pos = position
    min_hold = round(limit * p["min_long_frac"])
    buy_edge = p["dip_buy_edge"] if dev < -p["dip_threshold"] else p["buy_edge"]
    for ask_price in sorted(od.sell_orders.keys()):
        if ask_price <= fair_r + buy_edge:
            vol = abs(od.sell_orders[ask_price])
            qty = min(vol, limit - pos)
            if qty > 0:
                orders.append(Order(product, ask_price, qty))
                pos += qty
        else:
            break
    for bid_price in sorted(od.buy_orders.keys(), reverse=True):
        if bid_price > fair_r + p["take_profit_edge"]:
            vol = od.buy_orders[bid_price]
            max_sell = max(0, pos - min_hold)
            qty = min(vol, max_sell)
            if qty > 0:
                orders.append(Order(product, bid_price, -qty))
                pos -= qty
        else:
            break
    remaining_buy = limit - pos
    if remaining_buy > 0:
        bid_price, _ = spread_adaptive_quote(
            fair_r, best_bid, best_ask,
            p["bid_spread"], p["wide_spread_thr"], p["narrow_spread_thr"],
            bias=1,
        )
        orders.append(Order(product, bid_price, remaining_buy))
    if pos > round(limit * p["overlay_sell_thr"]):
        sell_qty = min(p["base_size"], pos - min_hold)
        if sell_qty > 0:
            _, ask_price = spread_adaptive_quote(
                fair_r, best_bid, best_ask,
                p["ask_spread"], p["wide_spread_thr"], p["narrow_spread_thr"],
                bias=1,
            )
            orders.append(Order(product, ask_price, -sell_qty))
    return orders


class Trader:
    def run(self, state):
        orders = {}
        conversions = 0
        ts = json.loads(state.traderData) if state.traderData else {}
        for product in state.order_depths:
            if product == "ASH_COATED_OSMIUM":
                orders[product] = trade_ash(state, ts)
            elif product == "INTARIAN_PEPPER_ROOT":
                orders[product] = trade_pepper(state, ts)
        return orders, conversions, json.dumps(ts)
