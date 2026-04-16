# Advanced ASH — position-aware taking + dynamic sizing
# Params: hs=1, bs=20, te_neutral=0, te_loaded=0, neutral_thr=0.3, dynamic_size=True, floor=0.6

import json
from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List, Tuple, Optional


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


def inventory_size_multiplier(position, limit, params):
    frac = abs(position) / limit if limit else 0
    if frac >= params["tier_extreme"]:
        add_mult = 0.0
    elif frac >= params["tier_high"]:
        add_mult = 0.25
    elif frac >= params["tier_medium"]:
        add_mult = 0.5
    else:
        add_mult = 1.0
    if position > 0:
        return add_mult, 1.0
    elif position < 0:
        return 1.0, add_mult
    return 1.0, 1.0


ASH_PARAMS = {
    "position_limit": 50,
    "anchor_fair": 10000,
    "micro_beta": 0.0,
    "half_spread": 1,
    "k_inv": 2.5,
    "base_size": 20,
    "flatten_size": 10,
    "tier_medium": 0.4,
    "tier_high": 0.7,
    "tier_extreme": 0.9,
    "take_edge_neutral": 0,
    "take_edge_loaded": 0,
    "take_neutral_threshold": 0.3,
}

PEPPER_LIMIT = 80


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
    mid = get_mid(od)
    if micro is not None and mid is not None:
        fair = fair + p["micro_beta"] * (micro - mid)
    fair_r = round(fair)
    orders = []
    pos = position

    # Position-aware taking: aggressive near neutral, passive when loaded
    inv_frac = abs(pos) / limit if limit > 0 else 0
    if inv_frac < p["take_neutral_threshold"]:
        buy_take_edge = p["take_edge_neutral"]
        sell_take_edge = p["take_edge_neutral"]
    else:
        buy_take_edge = p["take_edge_loaded"]
        sell_take_edge = p["take_edge_loaded"]

    # Also reduce counter-trend taking when loaded (from 182338 logic)
    if pos < 0:
        buy_take_edge = max(buy_take_edge, p["take_edge_neutral"])  # more aggressive buy when short
    if pos > 0:
        sell_take_edge = max(sell_take_edge, p["take_edge_neutral"])  # more aggressive sell when long

    if best_ask is not None:
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price <= fair_r - buy_take_edge:
                vol = abs(od.sell_orders[ask_price])
                qty = min(vol, limit - pos)
                if qty > 0:
                    orders.append(Order(product, ask_price, qty))
                    pos += qty
            else:
                break
    if best_bid is not None:
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price >= fair_r + sell_take_edge:
                vol = od.buy_orders[bid_price]
                qty = min(vol, limit + pos)
                if qty > 0:
                    orders.append(Order(product, bid_price, -qty))
                    pos -= qty
            else:
                break

    # Reservation price with inventory skew
    reservation = fair - p["k_inv"] * (pos / limit)
    res_r = round(reservation)
    bid_price = res_r - p["half_spread"]
    ask_price = res_r + p["half_spread"]
    if best_bid is not None:
        bid_price = min(best_bid + 1, bid_price)
    if best_ask is not None:
        ask_price = max(best_ask - 1, ask_price)
    if best_ask is not None:
        bid_price = min(bid_price, best_ask - 1)
    if best_bid is not None:
        ask_price = max(ask_price, best_bid + 1)

    buy_mult, sell_mult = inventory_size_multiplier(pos, limit, p)

    # Dynamic or fixed sizing
    inv_frac = abs(pos) / limit if limit > 0 else 0
    size_mult = max(0.6, 1.0 - inv_frac)
    buy_qty = min(round(p["base_size"] * buy_mult * size_mult), limit - pos)
    sell_qty = min(round(p["base_size"] * sell_mult * size_mult), limit + pos)

    if buy_qty > 0:
        orders.append(Order(product, bid_price, buy_qty))
    if sell_qty > 0:
        orders.append(Order(product, ask_price, -sell_qty))

    # Emergency flattening
    if abs(pos) >= p["tier_extreme"] * limit:
        if pos > 0 and best_bid is not None:
            qty = min(p["flatten_size"], pos)
            orders.append(Order(product, best_bid, -qty))
        elif pos < 0 and best_ask is not None:
            qty = min(p["flatten_size"], -pos)
            orders.append(Order(product, best_ask, qty))
    return orders


def trade_pepper(state, ts):
    product = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(product)
    if od is None:
        return []
    position = state.position.get(product, 0)
    limit = PEPPER_LIMIT
    best_bid, best_ask = get_best_bid_ask(od)
    orders = []
    pos = position
    if best_ask is not None:
        for ask_price in sorted(od.sell_orders.keys()):
            vol = abs(od.sell_orders[ask_price])
            qty = min(vol, limit - pos)
            if qty > 0:
                orders.append(Order(product, ask_price, qty))
                pos += qty
            if pos >= limit:
                break
    remaining = limit - pos
    if remaining > 0:
        if best_bid is not None and best_ask is not None:
            bp = best_bid + 1
            if bp < best_ask:
                orders.append(Order(product, bp, remaining))
        elif best_bid is not None:
            orders.append(Order(product, best_bid + 1, remaining))
    return orders


class Trader:
    def run(self, state):
        orders = {}
        ts = json.loads(state.traderData) if state.traderData else {}
        for product in state.order_depths:
            if product == "ASH_COATED_OSMIUM":
                orders[product] = trade_ash(state, ts)
            elif product == "INTARIAN_PEPPER_ROOT":
                orders[product] = trade_pepper(state, ts)
        return orders, 0, json.dumps(ts)
