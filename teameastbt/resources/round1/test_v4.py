import json
from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List, Tuple, Optional


# ============================================================
# V4 PARAMETERS
# ============================================================

ASH_PARAMS = {
    "position_limit": 50,
    "anchor_fair": 10000,

    # restore a small microprice signal
    "micro_beta": 0.08,

    # active edge taking
    "take_edge_strong": 2.0,     # full sweep threshold
    "take_edge_weak": 1.0,       # partial sweep threshold

    # inventory / reservation
    "k_inv": 3.0,
    "flatten_size": 12,

    # inventory tiers
    "tier_medium": 0.40,
    "tier_high": 0.70,
    "tier_extreme": 0.90,

    # quote regimes
    "join_thr": 3,               # tight spread: join
    "improve_thr": 8,            # medium spread: improve by 1

    # layered passive sizes
    "L1_size": 8,
    "L2_size": 8,
    "L3_size": 4,
    "L2_spread": 4,
    "L3_spread": 7,

    # opportunistic widening
    "opportunity_spread": 8,
    "opportunity_bonus_size": 4,
}

PEPPER_PARAMS = {
    "position_limit": 50,
    "fair_slope": 0.001,
    "day_base_map": {-2: 9998, -1: 10998, 0: 11998},

    # trust / regime
    "fair_sanity_max_dev": 20,
    "drift_ema_alpha": 0.05,
    "drift_threshold_low": 0.02,
    "drift_threshold_high": 0.08,

    # taking
    "buy_edge": 0,
    "take_profit_edge": 4,

    # passive quoting
    "join_thr": 3,
    "improve_thr": 8,
    "bid_spread": 4,
    "ask_spread": 6,
    "base_size": 8,

    # inventory control
    "inventory_tiers": {
        "medium": 0.40,
        "high": 0.65,
        "extreme": 0.85,
    },

    # target position sizing
    "target_small": 15,
    "target_medium": 30,
    "target_large": 45,
}


# ============================================================
# HELPERS
# ============================================================

def get_best_bid_ask(od: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    best_bid = max(od.buy_orders) if od.buy_orders else None
    best_ask = min(od.sell_orders) if od.sell_orders else None
    return best_bid, best_ask


def get_mid(od: OrderDepth) -> Optional[float]:
    best_bid, best_ask = get_best_bid_ask(od)
    if best_bid is None or best_ask is None:
        return None
    return (best_bid + best_ask) / 2


def get_microprice(od: OrderDepth) -> Optional[float]:
    best_bid, best_ask = get_best_bid_ask(od)
    if best_bid is None or best_ask is None:
        return None

    bid_vol = od.buy_orders[best_bid]
    ask_vol = abs(od.sell_orders[best_ask])
    total = bid_vol + ask_vol
    if total == 0:
        return (best_bid + best_ask) / 2

    return (best_bid * ask_vol + best_ask * bid_vol) / total


def inventory_size_multiplier(position: int, limit: int, tiers: dict) -> Tuple[float, float]:
    frac = abs(position) / limit if limit else 0.0

    if frac >= tiers.get("extreme", 0.9):
        add_mult = 0.0
    elif frac >= tiers.get("high", 0.7):
        add_mult = 0.25
    elif frac >= tiers.get("medium", 0.4):
        add_mult = 0.50
    else:
        add_mult = 1.0

    if position > 0:
        return add_mult, 1.0
    elif position < 0:
        return 1.0, add_mult
    else:
        return 1.0, 1.0


def clamp(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, x))


def quote_by_regime(
    fair_r: int,
    best_bid: int,
    best_ask: int,
    join_thr: int,
    improve_thr: int,
    base_bid_spread: int,
    base_ask_spread: int,
    bias: int = 0,
) -> Tuple[int, int]:
    """
    bias > 0: want longer
    bias < 0: want shorter
    """
    spread = best_ask - best_bid

    if spread <= join_thr:
        bid_price = best_bid
        ask_price = best_ask
    elif spread <= improve_thr:
        bid_price = best_bid + 1
        ask_price = best_ask - 1
    else:
        bid_price = max(best_bid + 1, fair_r - base_bid_spread)
        ask_price = min(best_ask - 1, fair_r + base_ask_spread)

    if bias > 0:
        bid_price = min(bid_price + 1, best_ask - 1)
    elif bias < 0:
        ask_price = max(ask_price - 1, best_bid + 1)

    bid_price = min(bid_price, best_ask - 1)
    ask_price = max(ask_price, best_bid + 1)

    return bid_price, ask_price


# ============================================================
# ASH_COATED_OSMIUM — ACTIVE EDGE TAKER + MM
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
    if best_bid is None or best_ask is None:
        return []

    mid = get_mid(od)
    micro = get_microprice(od)

    fair = p["anchor_fair"]
    if mid is not None and micro is not None:
        fair += p["micro_beta"] * (micro - mid)

    fair_r = round(fair)
    orders: List[Order] = []
    pos = position

    # Inventory-aware dynamic thresholds
    # If already long, be harder to buy and easier to sell; mirror if short.
    inv_frac = abs(pos) / limit
    buy_thresh = p["take_edge_strong"]
    sell_thresh = p["take_edge_strong"]
    weak_buy_thresh = p["take_edge_weak"]
    weak_sell_thresh = p["take_edge_weak"]

    if pos > 0:
        buy_thresh += 0.5 + inv_frac
        weak_buy_thresh += 0.5
        sell_thresh -= 0.5
        weak_sell_thresh -= 0.25
    elif pos < 0:
        sell_thresh += 0.5 + inv_frac
        weak_sell_thresh += 0.5
        buy_thresh -= 0.5
        weak_buy_thresh -= 0.25

    # ------------------------------------------------------------
    # ACTIVE TAKING: sweep multiple profitable levels
    # ------------------------------------------------------------
    for ask_price in sorted(od.sell_orders.keys()):
        edge = fair - ask_price
        vol = abs(od.sell_orders[ask_price])

        if edge >= buy_thresh:
            qty = min(vol, limit - pos)
        elif edge >= weak_buy_thresh:
            qty = min(vol, max(0, (limit - pos) // 2))
        else:
            break

        if qty > 0:
            orders.append(Order(product, ask_price, qty))
            pos += qty

    for bid_price in sorted(od.buy_orders.keys(), reverse=True):
        edge = bid_price - fair
        vol = od.buy_orders[bid_price]

        if edge >= sell_thresh:
            qty = min(vol, limit + pos)
        elif edge >= weak_sell_thresh:
            qty = min(vol, max(0, (limit + pos) // 2))
        else:
            break

        if qty > 0:
            orders.append(Order(product, bid_price, -qty))
            pos -= qty

    # ------------------------------------------------------------
    # PASSIVE MM: 3-regime quoting with reservation price
    # ------------------------------------------------------------
    reservation = fair - p["k_inv"] * (pos / limit)
    res_r = round(reservation)

    tiers = {
        "medium": p["tier_medium"],
        "high": p["tier_high"],
        "extreme": p["tier_extreme"],
    }
    buy_mult, sell_mult = inventory_size_multiplier(pos, limit, tiers)

    remaining_buy = limit - pos
    remaining_sell = limit + pos

    inv_bias = -1 if pos > limit * 0.30 else (1 if pos < -limit * 0.30 else 0)
    l1_bid, l1_ask = quote_by_regime(
        fair_r=res_r,
        best_bid=best_bid,
        best_ask=best_ask,
        join_thr=p["join_thr"],
        improve_thr=p["improve_thr"],
        base_bid_spread=3,
        base_ask_spread=3,
        bias=inv_bias,
    )

    spread = best_ask - best_bid
    opp_bonus = p["opportunity_bonus_size"] if spread >= p["opportunity_spread"] else 0

    l1_buy = min(round(p["L1_size"] * buy_mult) + opp_bonus, remaining_buy)
    l1_sell = min(round(p["L1_size"] * sell_mult) + opp_bonus, remaining_sell)

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

    # ------------------------------------------------------------
    # FLATTENING
    # ------------------------------------------------------------
    if abs(pos) >= p["tier_extreme"] * limit:
        if pos > 0:
            qty = min(p["flatten_size"], pos)
            if qty > 0:
                orders.append(Order(product, best_bid, -qty))
        elif pos < 0:
            qty = min(p["flatten_size"], -pos)
            if qty > 0:
                orders.append(Order(product, best_ask, qty))

    return orders


# ============================================================
# INTARIAN_PEPPER_ROOT — DYNAMIC TREND RIDER
# ============================================================

def get_pepper_day_base(mid: float, timestamp: int, p: dict) -> int:
    best_base = None
    best_dist = float("inf")
    for _, base in p["day_base_map"].items():
        expected = base + p["fair_slope"] * timestamp
        dist = abs(mid - expected)
        if dist < best_dist:
            best_dist = dist
            best_base = base
    return best_base


def target_position_from_drift(drift_ema: float, p: dict) -> int:
    if drift_ema >= p["drift_threshold_high"]:
        return p["target_large"]
    if drift_ema >= p["drift_threshold_low"]:
        return p["target_medium"]
    if drift_ema > 0:
        return p["target_small"]
    if drift_ema <= -p["drift_threshold_high"]:
        return -p["target_large"]
    if drift_ema <= -p["drift_threshold_low"]:
        return -p["target_medium"]
    if drift_ema < 0:
        return -p["target_small"]
    return 0


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

    if best_bid is None or best_ask is None or mid is None:
        return []

    timestamp = state.timestamp

    day_base = ts.get("pepper_day_base")
    if day_base is None:
        day_base = get_pepper_day_base(mid, timestamp, p)
        ts["pepper_day_base"] = day_base

    fair = day_base + p["fair_slope"] * timestamp
    fair_r = round(fair)
    fair_trusted = abs(mid - fair) < p["fair_sanity_max_dev"]

    prev_mid = ts.get("pepper_prev_mid", mid)
    ts["pepper_prev_mid"] = mid

    drift_rate = mid - prev_mid
    drift_ema = ts.get("pepper_drift_ema", 0.0)
    drift_ema = p["drift_ema_alpha"] * drift_rate + (1 - p["drift_ema_alpha"]) * drift_ema
    ts["pepper_drift_ema"] = drift_ema

    target_pos = target_position_from_drift(drift_ema, p)
    target_pos = clamp(target_pos, -limit, limit)

    orders: List[Order] = []
    pos = position

    # ------------------------------------------------------------
    # ACTIVE ALIGNMENT TO TARGET
    # ------------------------------------------------------------
    # If fair is trusted, actively move toward target when market is favorable.
    if fair_trusted:
        # Need to buy up to target
        if pos < target_pos:
            need = target_pos - pos
            for ask_price in sorted(od.sell_orders.keys()):
                # Buy if not overpaying relative to fair, or if trend is strong enough
                strong_up = drift_ema >= p["drift_threshold_high"]
                if ask_price <= fair_r + p["buy_edge"] or strong_up:
                    vol = abs(od.sell_orders[ask_price])
                    qty = min(vol, need, limit - pos)
                    if qty > 0:
                        orders.append(Order(product, ask_price, qty))
                        pos += qty
                        need -= qty
                else:
                    break

        # Need to sell down to target
        if pos > target_pos:
            need = pos - target_pos
            for bid_price in sorted(od.buy_orders.keys(), reverse=True):
                strong_down = drift_ema <= -p["drift_threshold_high"]
                if bid_price >= fair_r - p["buy_edge"] or strong_down:
                    vol = od.buy_orders[bid_price]
                    qty = min(vol, need, limit + pos)
                    if qty > 0:
                        orders.append(Order(product, bid_price, -qty))
                        pos -= qty
                        need -= qty
                else:
                    break

    # ------------------------------------------------------------
    # TAKE PROFIT BEYOND TARGET
    # ------------------------------------------------------------
    if pos > target_pos:
        extra = pos - target_pos
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price >= fair_r + p["take_profit_edge"]:
                vol = od.buy_orders[bid_price]
                qty = min(vol, extra, limit + pos)
                if qty > 0:
                    orders.append(Order(product, bid_price, -qty))
                    pos -= qty
                    extra -= qty
            else:
                break

    if pos < target_pos:
        extra = target_pos - pos
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price <= fair_r - p["take_profit_edge"]:
                vol = abs(od.sell_orders[ask_price])
                qty = min(vol, extra, limit - pos)
                if qty > 0:
                    orders.append(Order(product, ask_price, qty))
                    pos += qty
                    extra -= qty
            else:
                break

    # ------------------------------------------------------------
    # PASSIVE QUOTING AROUND TARGET
    # ------------------------------------------------------------
    tiers = p["inventory_tiers"]
    buy_mult, sell_mult = inventory_size_multiplier(pos, limit, tiers)

    # bias toward target
    bias = 0
    if pos < target_pos:
        bias = 1
    elif pos > target_pos:
        bias = -1

    bid_price, ask_price = quote_by_regime(
        fair_r=fair_r,
        best_bid=best_bid,
        best_ask=best_ask,
        join_thr=p["join_thr"],
        improve_thr=p["improve_thr"],
        base_bid_spread=p["bid_spread"],
        base_ask_spread=p["ask_spread"],
        bias=bias,
    )

    # only post the side that helps move toward target more aggressively
    if pos < target_pos:
        buy_qty = min(round(p["base_size"] * buy_mult), target_pos - pos, limit - pos)
        if buy_qty > 0:
            orders.append(Order(product, bid_price, buy_qty))

        # still allow some offer posting if already long enough
        if pos > 0:
            sell_qty = min(round(0.5 * p["base_size"] * sell_mult), limit + pos)
            if sell_qty > 0:
                orders.append(Order(product, ask_price, -sell_qty))

    elif pos > target_pos:
        sell_qty = min(round(p["base_size"] * sell_mult), pos - target_pos, limit + pos)
        if sell_qty > 0:
            orders.append(Order(product, ask_price, -sell_qty))

        if pos < 0:
            buy_qty = min(round(0.5 * p["base_size"] * buy_mult), limit - pos)
            if buy_qty > 0:
                orders.append(Order(product, bid_price, buy_qty))

    else:
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
