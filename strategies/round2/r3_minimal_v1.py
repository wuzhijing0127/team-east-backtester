"""
r3_minimal_v1 — simplest robust version per step 13
=====================================================
Exact rules from spec:

  fair = 0.25*anchor + 0.45*mid + 0.30*micro
  fast EMA / slow EMA trend
  if trend strong:      widen spread by 1, halve size against trend
  if inventory > 50%:   disable worsening side by 75%, aggressive flatten by 25%
  if |mid-anchor| large for many ticks:  cut anchor weight to 0.05

That's it. No state machine, no 12-layer stop-loss, no multi-band.
"""

import json
from datamodel import Order, OrderDepth, TradingState

MAF = 0

# --- Fair weights ---
W_ANCHOR_DEFAULT = 0.25
W_ANCHOR_REDUCED = 0.05
W_MID            = 0.45
W_MICRO          = 0.30

# --- Trend detection ---
FAST_ALPHA = 2 / 9      # 8-tick EMA
SLOW_ALPHA = 2 / 33     # 32-tick EMA
STRONG_TREND = 1.5      # |trend| above this = "strong"

# --- Anchor reduction ---
ANCHOR_DEV_THRESHOLD = 5
ANCHOR_FAR_TICKS = 20    # "many ticks"

PRODUCT_CFG = {
    "ASH_COATED_OSMIUM": {
        "anchor":     10000,
        "limit":      20,
        "k_inv":      2.5,
        "base_size":  15,
        "base_hs":    1,
    },
    "INTARIAN_PEPPER_ROOT": {
        "anchor":     12000,   # rolling
        "limit":      80,
        "k_inv":      1.0,
        "base_size":  25,
        "base_hs":    2,
    },
}


def bb_ba(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def process(prod, od, pos, cfg, ts, key):
    bid, ask = bb_ba(od)
    if bid is None or ask is None:
        return []
    mid = (bid + ask) / 2

    bv = od.buy_orders[bid]
    av = abs(od.sell_orders[ask])
    micro = (bid * av + ask * bv) / (bv + av) if (bv + av) > 0 else mid

    # Rolling anchor for PEPPER
    anchor = cfg["anchor"]
    if key == "pepper":
        rolling = ts.get(f"anchor_{key}", mid)
        rolling = 0.002 * mid + 0.998 * rolling
        ts[f"anchor_{key}"] = rolling
        anchor = rolling

    # Fast/slow EMA for trend
    fast = ts.get(f"fast_{key}", mid)
    slow = ts.get(f"slow_{key}", mid)
    fast = FAST_ALPHA * mid + (1 - FAST_ALPHA) * fast
    slow = SLOW_ALPHA * mid + (1 - SLOW_ALPHA) * slow
    ts[f"fast_{key}"] = fast
    ts[f"slow_{key}"] = slow
    trend = fast - slow

    # Sustained anchor departure → reduce anchor weight
    anchor_dev = mid - anchor
    dev_key = f"anchor_dev_streak_{key}"
    streak = ts.get(dev_key, 0)
    streak = streak + 1 if abs(anchor_dev) > ANCHOR_DEV_THRESHOLD else 0
    ts[dev_key] = streak
    w_anchor = W_ANCHOR_REDUCED if streak >= ANCHOR_FAR_TICKS else W_ANCHOR_DEFAULT
    remain = 1.0 - w_anchor
    scale = remain / (W_MID + W_MICRO)
    fair = w_anchor * anchor + W_MID * scale * mid + W_MICRO * scale * micro

    # Strong trend → widen spread by 1, halve size against trend
    hs = cfg["base_hs"]
    size_buy = cfg["base_size"]
    size_sell = cfg["base_size"]
    strong_up = trend >= STRONG_TREND
    strong_dn = trend <= -STRONG_TREND
    if strong_up:
        hs += 1
        size_sell = size_sell // 2     # against trend = selling into uptrend
    elif strong_dn:
        hs += 1
        size_buy = size_buy // 2

    # Inventory > 50% → disable worsening side 75%, aggressive flatten 25%
    limit = cfg["limit"]
    inv_frac = abs(pos) / limit if limit > 0 else 0.0
    flatten_extra_qty = 0
    if inv_frac > 0.5:
        if pos > 0:
            size_buy = int(size_buy * 0.25)     # 75% reduction
            flatten_extra_qty = int(pos * 0.25)  # aggressive flatten 25% of position
        elif pos < 0:
            size_sell = int(size_sell * 0.25)
            flatten_extra_qty = int(-pos * 0.25)

    # Reservation with inventory skew
    reservation = fair - cfg["k_inv"] * (pos / limit if limit > 0 else 0)
    bid_q = round(reservation) - hs
    ask_q = round(reservation) + hs
    bid_q = min(bid_q, ask - 1)
    ask_q = max(ask_q, bid + 1)

    orders = []

    # Take at edge 1
    take_edge = 2 if (strong_up or strong_dn) else 1
    cur = pos
    for ap in sorted(od.sell_orders):
        if cur >= limit: break
        if ap > fair - take_edge: break
        q = min(abs(od.sell_orders[ap]), limit - cur)
        if q > 0:
            orders.append(Order(prod, ap, q))
            cur += q
    for bp in sorted(od.buy_orders, reverse=True):
        if cur <= -limit: break
        if bp < fair + take_edge: break
        q = min(od.buy_orders[bp], limit + cur)
        if q > 0:
            orders.append(Order(prod, bp, -q))
            cur -= q

    # Passive quotes
    if cur < limit and size_buy > 0:
        q = min(size_buy, limit - cur)
        orders.append(Order(prod, bid_q, q))
    if cur > -limit and size_sell > 0:
        q = min(size_sell, limit + cur)
        orders.append(Order(prod, ask_q, -q))

    # Aggressive flatten kick (inv_frac > 0.5)
    if flatten_extra_qty > 0:
        if pos > 0:
            orders.append(Order(prod, bid, -flatten_extra_qty))
        elif pos < 0:
            orders.append(Order(prod, ask, flatten_extra_qty))

    return orders


class Trader:
    def run(self, state):
        orders = {}
        ts = json.loads(state.traderData) if state.traderData else {}
        for prod in state.order_depths:
            if prod not in PRODUCT_CFG:
                continue
            cfg = PRODUCT_CFG[prod]
            key = "ash" if prod == "ASH_COATED_OSMIUM" else "pepper"
            pos = state.position.get(prod, 0)
            orders[prod] = process(prod, state.order_depths[prod], pos, cfg, ts, key)
        return orders, 0, json.dumps(ts)
