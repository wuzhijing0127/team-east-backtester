"""
Round 2 Strategy
================
ASH: proven round 1 maker-taker + optional light signal skew + closeout switch.
PEPPER: two-mode controller (carry or flat-close), configurable.

Default config: CONSERVATIVE PACKAGE (flat-close PEPPER + ASH closeout ON)
  - Historical combined P&L: 282,007
  - Zero rule-sensitive terminal inventory risk
  - Switch PEPPER_FLAT_CLOSE=False if carry is confirmed allowed

Key changes from round 1:
  1. PEPPER fair: 12000 + 1000*day + t/1000 (instead of slope-based calibration)
  2. PEPPER flat-close mode: unwind from t=998000, hard liquidate by t=999000
  3. ASH closeout switch: flatten inventory from t=998000
  4. ASH light signal skew: shift take edges when imb signal is strong
  5. Data-quality safeguards: suppress aggressive takes when book side missing
"""

import json
from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List, Tuple, Optional


# ============================================================
# GLOBAL MODE CONFIGURATION
# ============================================================
# PEPPER modes:
#   "carry"       — pure buy-and-hold to limit (best when carry allowed)
#   "flat_close"  — accumulate then unwind to 0 by session end
#   "updrift_mm"  — active spread capture around drifting fair, unwind end
PEPPER_MODE = "carry"
PEPPER_FLAT_CLOSE = (PEPPER_MODE == "flat_close")  # legacy flag
ASH_CLOSEOUT = True        # Flatten ASH inventory from t=998000

# Session timing
SESSION_END = 1000000      # one day = 1M timestamp units
UNWIND_START = 998000      # begin soft unwind
HARD_UNWIND = 999000       # hard liquidation


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


def get_l1_imbalance(od: OrderDepth) -> float:
    """[-1, 1]. Positive = more bid volume = buy pressure."""
    bid, ask = get_best_bid_ask(od)
    if bid is None or ask is None:
        return 0.0
    bid_vol = od.buy_orders.get(bid, 0)
    ask_vol = abs(od.sell_orders.get(ask, 0))
    total = bid_vol + ask_vol
    if total == 0:
        return 0.0
    return (bid_vol - ask_vol) / total


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


# ============================================================
# PEPPER — two-mode drift-rider
# ============================================================
# Fair model (from regression analysis, R²=0.9999976):
#   fair = 12000 + 1000*day + t/1000
# where day ∈ {-1, 0, 1} relative to reference day 0 (intercept=12000)
#
# Residual std is only ~1.3 ticks — PEPPER is a deterministic conveyor belt.
# Drift = +0.1 per 100-tick update = +1000 per day.

PEPPER_LIMIT = 80
PEPPER_SLOPE = 0.001

# Updrift MM parameters
PEPPER_UPDRIFT = {
    "target_long_frac": 0.6,    # target ~48 lots long (sweep-tuned)
    "bid_spread": 5,            # passive bid at fair - 5
    "ask_spread": 8,            # passive ask at fair + 8
    "take_buy_edge": 2,         # aggressive buy asks <= fair + 2
    "take_sell_edge": 6,        # aggressive sell bids >= fair + 6
    "k_inv": 3.0,
    "base_size": 15,
    "min_long_frac": 0.3,
}


def infer_pepper_day(mid: float, timestamp: int) -> int:
    """
    Infer which day we're on from first observed mid.
    Day -1: intercept ~11000, Day 0: ~12000, Day 1: ~13000
    """
    # Expected mid at timestamp t on day d: 12000 + 1000*d + t/1000
    # Solve for d: d = (mid - 12000 - t/1000) / 1000
    raw = (mid - 12000 - timestamp * PEPPER_SLOPE) / 1000
    # Clamp so a weird first snapshot can't produce nonsense
    return max(-1, min(1, round(raw)))


def fair_pepper(day: int, timestamp: int) -> float:
    return 12000 + 1000 * day + timestamp * PEPPER_SLOPE


def trade_pepper(state: TradingState, ts: dict) -> List[Order]:
    product = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(product)
    if od is None:
        return []

    position = state.position.get(product, 0)
    limit = PEPPER_LIMIT
    best_bid, best_ask = get_best_bid_ask(od)
    mid = get_mid(od)
    timestamp = state.timestamp

    # Data-quality safeguard: missing book
    if best_ask is None and position <= 0:
        return []

    orders = []
    pos = position

    # Determine day on first call and cache it
    if "pep_day" not in ts and mid is not None:
        ts["pep_day"] = infer_pepper_day(mid, timestamp)

    day = ts.get("pep_day", 0)
    fair = fair_pepper(day, timestamp)
    fair_r = round(fair)

    # ===================================================
    # MODE A: CARRY (pure buy-and-hold)
    # ===================================================
    if PEPPER_MODE == "carry":
        # Sweep asks to max long
        if best_ask is not None:
            for ask_price in sorted(od.sell_orders.keys()):
                vol = abs(od.sell_orders[ask_price])
                qty = min(vol, limit - pos)
                if qty > 0:
                    orders.append(Order(product, ask_price, qty))
                    pos += qty
                if pos >= limit:
                    break

        # Passive bid for remaining capacity
        remaining = limit - pos
        if remaining > 0 and best_ask is not None:
            orders.append(Order(product, best_ask - 1, remaining))
        return orders

    # ===================================================
    # MODE C: UPDRIFT MARKET MAKING
    # Active spread capture around drifting fair.
    # Long-biased (target 75% of limit) to ride the drift.
    # Aggressive buy on cheap asks, aggressive sell on rich bids,
    # passive quotes on both sides. Unwind to 0 at end of day.
    # ===================================================
    if PEPPER_MODE == "updrift_mm":
        up = PEPPER_UPDRIFT
        target_pos = round(up["target_long_frac"] * limit)
        min_hold = round(up["min_long_frac"] * limit)

        # --- End-of-day unwind ---
        if timestamp >= HARD_UNWIND:
            # Hard liquidate
            if pos > 0 and best_bid is not None:
                for bp in sorted(od.buy_orders.keys(), reverse=True):
                    vol = od.buy_orders[bp]
                    qty = min(vol, pos)
                    if qty > 0:
                        orders.append(Order(product, bp, -qty))
                        pos -= qty
                    if pos <= 0:
                        break
            return orders

        if timestamp >= UNWIND_START:
            # Target-based unwind (smooth)
            if pos > 0 and best_bid is not None:
                steps_left = max(1, (HARD_UNWIND - timestamp) // 100)
                qty = max(1, (pos + steps_left - 1) // steps_left)
                qty = min(qty, pos)
                orders.append(Order(product, best_bid, -qty))
            return orders

        # --- Aggressive BUY: take cheap asks ---
        if best_ask is not None:
            for ask_price in sorted(od.sell_orders.keys()):
                if ask_price <= fair_r + up["take_buy_edge"]:
                    vol = abs(od.sell_orders[ask_price])
                    qty = min(vol, limit - pos)
                    if qty > 0:
                        orders.append(Order(product, ask_price, qty))
                        pos += qty
                else:
                    break

        # --- Aggressive SELL: take rich bids (above fair + take_sell_edge) ---
        if best_bid is not None:
            for bid_price in sorted(od.buy_orders.keys(), reverse=True):
                if bid_price >= fair_r + up["take_sell_edge"]:
                    vol = od.buy_orders[bid_price]
                    max_sell = max(0, pos - min_hold)  # keep min_long
                    qty = min(vol, max_sell)
                    if qty > 0:
                        orders.append(Order(product, bid_price, -qty))
                        pos -= qty
                else:
                    break

        # --- Reservation price skewed around target position ---
        inv_dev = (pos - target_pos) / limit if limit else 0
        reservation = fair - up["k_inv"] * inv_dev
        res_r = round(reservation)

        # --- Passive BID: eager (tight), full remaining capacity ---
        remaining_buy = limit - pos
        if remaining_buy > 0:
            bid_price = res_r - up["bid_spread"]
            if best_ask is not None:
                bid_price = min(bid_price, best_ask - 1)
            orders.append(Order(product, bid_price, remaining_buy))

        # --- Passive ASK: only when above target, wide premium ---
        if pos > target_pos:
            sell_qty = min(up["base_size"], pos - min_hold)
            if sell_qty > 0:
                ask_price = res_r + up["ask_spread"]
                if best_bid is not None:
                    ask_price = max(ask_price, best_bid + 1)
                orders.append(Order(product, ask_price, -sell_qty))

        return orders

    # ===================================================
    # MODE B: FLAT-CLOSE (conservative)
    # ===================================================
    # Early session: accumulate long
    # Late session (998000+): begin unwind
    # Very late (999000+): hard liquidate

    if timestamp < UNWIND_START:
        # Accumulate — buy any ask at or below fair
        if best_ask is not None:
            for ask_price in sorted(od.sell_orders.keys()):
                if ask_price <= fair_r:  # buy_thresh = 0
                    vol = abs(od.sell_orders[ask_price])
                    qty = min(vol, limit - pos, 20)  # max clip 20
                    if qty > 0:
                        orders.append(Order(product, ask_price, qty))
                        pos += qty
                else:
                    break

        # Passive bid: competitive with current best
        remaining = limit - pos
        if remaining > 0:
            bid_price = fair_r - 2
            if best_bid is not None:
                bid_price = max(bid_price, best_bid + 1)
            if best_ask is not None:
                bid_price = min(bid_price, best_ask - 1)
            qty = min(10, remaining)
            orders.append(Order(product, bid_price, qty))

    elif timestamp < HARD_UNWIND:
        # Soft unwind: target-based — smoothly reduce to 0 by HARD_UNWIND
        if pos > 0 and best_bid is not None:
            steps_left = max(1, (HARD_UNWIND - timestamp) // 100)
            # ceil(pos / steps_left) — ensures we finish if liquidity allows
            qty = max(1, (pos + steps_left - 1) // steps_left)
            qty = min(qty, pos)
            orders.append(Order(product, best_bid, -qty))

    else:
        # Hard liquidate: sweep all bids to exit
        if pos > 0 and best_bid is not None:
            for bid_price in sorted(od.buy_orders.keys(), reverse=True):
                vol = od.buy_orders[bid_price]
                qty = min(vol, pos)
                if qty > 0:
                    orders.append(Order(product, bid_price, -qty))
                    pos -= qty
                if pos <= 0:
                    break

    return orders


# ============================================================
# ASH — proven round 1 maker-taker + light signal skew + closeout
# ============================================================

ASH_PARAMS = {
    "position_limit": 80,
    "anchor_fair": 10000,
    "micro_beta": 0.05,
    "take_edge": 4,
    "half_spread": 1,
    "k_inv": 10.0,
    "base_size": 15,
    "flatten_size": 10,
    "flatten_size_closeout": 20,
    "tier_medium": 0.4,
    "tier_high": 0.7,
    "tier_extreme": 0.9,
    # Signal skew (from regression: s = 0.04*(10000-mid) + 4.6*imb)
    "signal_threshold": 1.0,
}


def trade_ash(state: TradingState, ts: dict) -> List[Order]:
    product = "ASH_COATED_OSMIUM"
    p = ASH_PARAMS
    od = state.order_depths.get(product)
    if od is None:
        return []

    position = state.position.get(product, 0)
    limit = p["position_limit"]
    best_bid, best_ask = get_best_bid_ask(od)
    timestamp = state.timestamp

    # Data-quality safeguard
    if best_bid is None and best_ask is None:
        return []

    # Fair value: anchor + microprice tilt
    fair = float(p["anchor_fair"])
    micro = get_microprice(od)
    mid = get_mid(od)
    if micro is not None and mid is not None:
        fair += p["micro_beta"] * (micro - mid)
    fair_r = round(fair)

    orders = []
    pos = position

    # ===================================================
    # CLOSEOUT MODE: flatten from t=998000 — do NOT reopen late
    # ===================================================
    if ASH_CLOSEOUT and timestamp >= UNWIND_START:
        flat_sz = p["flatten_size_closeout"]
        if pos > 0 and best_bid is not None:
            qty = min(flat_sz, pos)
            orders.append(Order(product, best_bid, -qty))
        elif pos < 0 and best_ask is not None:
            qty = min(flat_sz, -pos)
            orders.append(Order(product, best_ask, qty))
        # Do not reopen inventory late — always return here in closeout mode
        return orders

    # ===================================================
    # LIGHT SIGNAL SKEW (execution-only, not fair-shifting)
    # ===================================================
    # s = 0.04*(10000-mid) + 4.6*imb
    # Strong positive signal: reduce buy_take_edge by 1, reduce ask size
    # Strong negative signal: reduce sell_take_edge by 1, reduce bid size
    imb = get_l1_imbalance(od)
    signal = 0.0
    if mid is not None:
        signal = 0.04 * (10000 - mid) + 4.6 * imb

    # Inventory-aware take edges
    inv_frac = pos / limit if limit else 0
    buy_te = p["take_edge"]
    sell_te = p["take_edge"]
    if inv_frac > 0.4:
        buy_te += 1
        sell_te -= 1
    elif inv_frac < -0.4:
        buy_te -= 1
        sell_te += 1

    # Apply signal skew
    sig_thr = p["signal_threshold"]
    ask_size_mult = 1.0
    bid_size_mult = 1.0
    if signal >= sig_thr:
        buy_te = max(0, buy_te - 1)
        ask_size_mult = 0.8
    elif signal <= -sig_thr:
        sell_te = max(0, sell_te - 1)
        bid_size_mult = 0.8

    buy_te = max(0, buy_te)
    sell_te = max(0, sell_te)

    # ===================================================
    # AGGRESSIVE TAKES
    # ===================================================
    # Safeguard: if book side missing, suppress aggressive takes on that side
    if best_ask is not None:
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price <= fair_r - buy_te:
                vol = abs(od.sell_orders[ask_price])
                qty = min(vol, limit - pos)
                if qty > 0:
                    orders.append(Order(product, ask_price, qty))
                    pos += qty
            else:
                break

    if best_bid is not None:
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price >= fair_r + sell_te:
                vol = od.buy_orders[bid_price]
                qty = min(vol, limit + pos)
                if qty > 0:
                    orders.append(Order(product, bid_price, -qty))
                    pos -= qty
            else:
                break

    # ===================================================
    # NONLINEAR RESERVATION SKEW + SIGNAL QUOTE-CENTER SKEW
    # ===================================================
    inv_frac = pos / limit if limit else 0
    # Small signal-based shift of quote center (±2 ticks max)
    quote_skew = max(-2, min(2, round(0.35 * signal)))
    reservation = fair - p["k_inv"] * inv_frac * abs(inv_frac) + quote_skew
    res_r = round(reservation)

    hs = p["half_spread"]
    bid_price = res_r - hs
    ask_price = res_r + hs

    # Inside-market quoting (kept in original form — empirically best on backtester)
    if best_bid is not None:
        bid_price = min(best_bid + 1, bid_price)
    if best_ask is not None:
        ask_price = max(best_ask - 1, ask_price)
    # Avoid crossed quotes
    if best_ask is not None:
        bid_price = min(bid_price, best_ask - 1)
    if best_bid is not None:
        ask_price = max(ask_price, best_bid + 1)

    # Inventory-aware + signal-aware sizing
    buy_mult, sell_mult = inventory_size_multiplier(pos, limit, p)
    buy_qty = min(round(p["base_size"] * buy_mult * bid_size_mult), limit - pos)
    sell_qty = min(round(p["base_size"] * sell_mult * ask_size_mult), limit + pos)

    if buy_qty > 0:
        orders.append(Order(product, bid_price, buy_qty))
    if sell_qty > 0:
        orders.append(Order(product, ask_price, -sell_qty))

    # ===================================================
    # NORMAL FLATTEN AT FAIR (non-closeout mode)
    # ===================================================
    if abs(pos) >= p["tier_extreme"] * limit:
        if pos > 0 and best_bid is not None:
            fq = min(p["flatten_size"], pos)
            orders.append(Order(product, fair_r, -fq))
        elif pos < 0 and best_ask is not None:
            fq = min(p["flatten_size"], -pos)
            orders.append(Order(product, fair_r, fq))

    return orders


# ============================================================
# MAIN TRADER
# ============================================================

class Trader:
    def bid(self):
        """
        Round 2 Market Access Fee bid.
        One-time fee paid if in top 50% of bids; grants +25% order book volume.

        Empirical analysis (simulate_extra_access.py) with carry PEPPER mode:
          Baseline 3-day PnL:   295,793
          +25% volumes PnL:     296,936
          Delta (max rational): +1,143

        Carry mode benefits less from extra access than updrift_mm because:
          - PEPPER carry is already maxed at 80 long — extra asks barely help (+44)
          - No active sell-side trading means extra bids go unused
          - Only ASH side gets meaningful uplift (+1,099)

        Game theory: need only top 50%. Bid 800 — leaves ~343 margin
        below break-even (1,143) for simulation uncertainty, high enough
        to likely clear teams bidding 0-700.
        """
        return 800

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