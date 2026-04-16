"""Modular ASH trading engines — each function is stateless and parameterized.

A. Signal: compute_fair, compute_reservation
B. Take: generate_take_orders
C. Passive: generate_passive_orders
D. Risk: inventory_multipliers, apply_flattening
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class BookState:
    """Pre-computed order book features."""
    best_bid: Optional[int]
    best_ask: Optional[int]
    mid: Optional[float]
    microprice: Optional[float]
    spread: Optional[int]
    bid_vol_total: int
    ask_vol_total: int
    imbalance: float  # (bid - ask) / (bid + ask), in [-1, 1]


def extract_book(od: Any) -> BookState:
    """Extract book features from an OrderDepth object."""
    best_bid = max(od.buy_orders) if od.buy_orders else None
    best_ask = min(od.sell_orders) if od.sell_orders else None

    mid = None
    microprice = None
    spread = None
    bid_vol = 0
    ask_vol = 0

    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2
        spread = best_ask - best_bid

        bv = od.buy_orders[best_bid]
        av = abs(od.sell_orders[best_ask])
        bid_vol = sum(v for v in od.buy_orders.values())
        ask_vol = sum(abs(v) for v in od.sell_orders.values())
        total = bv + av
        microprice = (best_bid * av + best_ask * bv) / total if total > 0 else mid

    imbalance = 0.0
    if bid_vol + ask_vol > 0:
        imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)

    return BookState(
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        microprice=microprice,
        spread=spread,
        bid_vol_total=bid_vol,
        ask_vol_total=ask_vol,
        imbalance=imbalance,
    )


# ── A. Signal computation ─────────────────────────────────────

def compute_fair(
    book: BookState,
    anchor_fair: int,
    micro_beta: float,
    imbalance_beta: float,
) -> float:
    """Compute fair value from anchor + microprice signal + imbalance signal."""
    fair = float(anchor_fair)
    if book.microprice is not None and book.mid is not None:
        fair += micro_beta * (book.microprice - book.mid)
    fair += imbalance_beta * book.imbalance * (book.spread or 1)
    return fair


def compute_reservation(
    fair: float,
    position: int,
    limit: int,
    base_skew: float,
    inventory_skew_k: float,
    signal_skew_k: float = 0.0,
    signal: float = 0.0,
) -> float:
    """Compute always-skewed reservation price."""
    reservation = fair + base_skew
    reservation += inventory_skew_k * (-position / limit) if limit > 0 else 0
    reservation += signal_skew_k * signal
    return reservation


# ── B. Take engine ────────────────────────────────────────────

def generate_take_orders(
    product: str,
    od: Any,
    fair_r: int,
    position: int,
    limit: int,
    take_buy_edge: int,
    take_sell_edge: int,
    take_buy_when_short_edge: int,
    take_sell_when_long_edge: int,
) -> tuple[list, int]:
    """Generate aggressive take orders. Returns (orders, updated_position)."""
    orders = []
    pos = position

    # Effective edges — more aggressive when reducing inventory
    eff_buy_edge = take_buy_when_short_edge if pos < 0 else take_buy_edge
    eff_sell_edge = take_sell_when_long_edge if pos > 0 else take_sell_edge

    # Buy takes: sweep asks at or below fair - edge
    for ask_price in sorted(od.sell_orders.keys()):
        if ask_price <= fair_r - eff_buy_edge:
            vol = abs(od.sell_orders[ask_price])
            qty = min(vol, limit - pos)
            if qty > 0:
                orders.append((product, ask_price, qty))
                pos += qty
        else:
            break

    # Sell takes: sweep bids at or above fair + edge
    for bid_price in sorted(od.buy_orders.keys(), reverse=True):
        if bid_price >= fair_r + eff_sell_edge:
            vol = od.buy_orders[bid_price]
            qty = min(vol, limit + pos)
            if qty > 0:
                orders.append((product, bid_price, -qty))
                pos -= qty
        else:
            break

    return orders, pos


# ── C. Passive quote engine ───────────────────────────────────

def generate_passive_orders(
    product: str,
    book: BookState,
    reservation_r: int,
    position: int,
    limit: int,
    bid_half_spread: int,
    ask_half_spread: int,
    quote_size_bid: int,
    quote_size_ask: int,
    bid_mult: float,
    ask_mult: float,
    join_improve_mode: int,
) -> list:
    """Generate passive bid/ask quotes with asymmetric spreads and sizing."""
    if book.best_bid is None or book.best_ask is None:
        return []

    orders = []

    # Compute raw bid/ask from reservation + asymmetric spreads
    raw_bid = reservation_r - bid_half_spread
    raw_ask = reservation_r + ask_half_spread

    # Apply join/improve logic
    if join_improve_mode == 1:
        # Join best
        bid_price = max(raw_bid, book.best_bid)
        ask_price = min(raw_ask, book.best_ask)
    elif join_improve_mode == 2:
        # Improve best by 1, but don't cross reservation-derived price too much
        bid_price = max(raw_bid, book.best_bid + 1)
        ask_price = min(raw_ask, book.best_ask - 1)
    else:
        # Pure reservation-derived
        bid_price = raw_bid
        ask_price = raw_ask

    # Safety: never cross the spread
    bid_price = min(bid_price, book.best_ask - 1)
    ask_price = max(ask_price, book.best_bid + 1)

    # Don't let bid exceed ask
    if bid_price >= ask_price:
        mid = (bid_price + ask_price) // 2
        bid_price = mid - 1
        ask_price = mid + 1

    # Apply inventory-scaled sizing
    remaining_buy = limit - position
    remaining_sell = limit + position

    buy_qty = min(round(quote_size_bid * bid_mult), remaining_buy)
    sell_qty = min(round(quote_size_ask * ask_mult), remaining_sell)

    if buy_qty > 0:
        orders.append((product, bid_price, buy_qty))
    if sell_qty > 0:
        orders.append((product, ask_price, -sell_qty))

    return orders


# ── D. Risk engine ────────────────────────────────────────────

def inventory_multipliers(
    position: int,
    limit: int,
    tier_medium: float,
    tier_high: float,
    tier_extreme: float,
    bid_mult_normal: float,
    ask_mult_normal: float,
    bid_mult_medium: float,
    ask_mult_medium: float,
    bid_mult_high: float,
    ask_mult_high: float,
    bid_mult_extreme: float,
    ask_mult_extreme: float,
) -> tuple[float, float]:
    """Compute asymmetric bid/ask size multipliers based on inventory level."""
    frac = abs(position) / limit if limit > 0 else 0

    if frac >= tier_extreme:
        add_bid, add_ask = bid_mult_extreme, ask_mult_extreme
    elif frac >= tier_high:
        add_bid, add_ask = bid_mult_high, ask_mult_high
    elif frac >= tier_medium:
        add_bid, add_ask = bid_mult_medium, ask_mult_medium
    else:
        add_bid, add_ask = bid_mult_normal, ask_mult_normal

    # When long, reduce buying; when short, reduce selling
    if position > 0:
        return add_bid, ask_mult_normal  # reduce bid side, full ask
    elif position < 0:
        return bid_mult_normal, add_ask  # full bid, reduce ask side
    else:
        return bid_mult_normal, ask_mult_normal


def apply_flattening(
    product: str,
    book: BookState,
    fair_r: int,
    position: int,
    limit: int,
    flatten_enabled: bool,
    flatten_trigger: float,
    flatten_size: int,
    flatten_aggression: int,
) -> list:
    """Generate emergency flattening orders when position is extreme."""
    if not flatten_enabled:
        return []

    if abs(position) < flatten_trigger * limit:
        return []

    orders = []
    if position > 0 and book.best_bid is not None:
        qty = min(flatten_size, position)
        price = fair_r if flatten_aggression == 1 else book.best_bid
        orders.append((product, price, -qty))
    elif position < 0 and book.best_ask is not None:
        qty = min(flatten_size, -position)
        price = fair_r if flatten_aggression == 1 else book.best_ask
        orders.append((product, price, qty))

    return orders
