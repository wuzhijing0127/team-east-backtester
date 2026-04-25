"""
r3_hybrid_v1 — ASH via user's 4-module framework, PEPPER via v12 engine
========================================================================
DIAGNOSIS from structured_v1 (-305) and minimal_v1 (-1,259):
  The user's framework (inventory bands, risk-filter dropping worsening
  orders, flatten triggers at >50%) is DESIGNED for a bilateral MM product.
  Applied to PEPPER, it kills the directional drift-capture engine that
  made +79k in round 1.

HYBRID STRATEGY:
  • ASH: full framework (steps 1-9) — it's a stationary MM product, ideal target
  • PEPPER: v12-proven engine — max-long + dip overlay + fast EMA
"""

import json
from datamodel import Order, OrderDepth, TradingState

MAF = 0

# ======================================================================
# ASH: FULL 4-MODULE FRAMEWORK (from r3_structured_v1, ASH only)
# ======================================================================
ASH_LIMIT = 20
ASH_ANCHOR = 10000
ASH_K_INV = 2.5
ASH_BASE_SIZE = 8

FV_W_ANCHOR = 0.20
FV_W_MID    = 0.35
FV_W_MICRO  = 0.25
FV_W_EMA    = 0.20
FAST_EMA_ALPHA = 0.05
SLOW_EMA_ALPHA = 0.01
EMA8_ALPHA  = 2 / 9
EMA32_ALPHA = 2 / 33
VOL_WINDOW  = 20

STATE_PARAMS = {
    "spread_tight": 5, "spread_wide": 15,
    "trend_neutral_band": 1.0, "imbalance_band": 0.2,
    "vol_low": 0.5, "vol_high": 2.0,
    "anchor_near_band": 3.0,
    "inv_low": 0.4, "inv_med": 0.7, "inv_high": 0.9,
}

ANCHOR_DEV_STREAK_TICKS = 15
UNC_STREAK_TICKS = 5
BAND_1_MAX = 0.30
BAND_2_MAX = 0.60
BAND_3_MAX = 0.80
FAR_ANCHOR_TICKS_PROTECT = 30
FAR_ANCHOR_THRESHOLD = 5
HEAVY_INV_FRAC = 0.7
DEEP_EDGE_EXTRA = 2
HS_MIN, HS_MAX = 1, 5

BEHAVIOR = {
    "A_STABLE":         {"both": True,  "take_edge": 1, "size_mult": 1.0, "inside": True,  "anchor_w": None, "flatten": False},
    "B_TRENDING":       {"both": True,  "take_edge": 2, "size_mult": 0.6, "inside": False, "anchor_w": 0.05, "flatten": False},
    "C_WIDE_JUMPY":     {"both": True,  "take_edge": 3, "size_mult": 0.4, "inside": False, "anchor_w": None, "flatten": False},
    "D_INVENTORY":      {"both": False, "take_edge": 0, "size_mult": 0.5, "inside": False, "anchor_w": None, "flatten": True},
    "E_MODEL_DISAGREE": {"both": True,  "take_edge": 2, "size_mult": 0.5, "inside": False, "anchor_w": 0.05, "flatten": False},
}


def bb_ba(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def _A_spread(m):      return 0 if m <= 3 else (1 if m <= 5 else 2)
def _A_vol(v):         return 0 if v < 1.2 else (1 if v < 2.0 else (2 if v < 3.0 else 3))
def _A_trend(tm):      return 0 if tm < 1.0 else (1 if tm < 2.0 else (2 if tm < 3.0 else 3))
def _A_inv(f):         return 0 if f < 0.5  else (1 if f < 0.7  else (2 if f < 0.85 else 3))
def _A_uncertainty(u): return 0 if u < 1.5  else (1 if u < 3.0  else (2 if u < 5.0  else 3))

def _score_to_hs(s):
    if s <= 1:  return 1
    if s <= 4:  return 2
    if s <= 7:  return 3
    if s <= 10: return 4
    return 5


def trade_ash(state, ts):
    prod = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(prod)
    if od is None:
        return []
    pos = state.position.get(prod, 0)
    bid, ask = bb_ba(od)
    if bid is None or ask is None:
        return []

    mid = (bid + ask) / 2
    bv = od.buy_orders[bid]
    av = abs(od.sell_orders[ask])
    micro = (bid * av + ask * bv) / (bv + av) if (bv + av) > 0 else mid

    # Preliminary fair for classification
    ema = ts.get("ash_ema", ASH_ANCHOR)
    ema = FAST_EMA_ALPHA * mid + (1 - FAST_EMA_ALPHA) * ema
    ts["ash_ema"] = ema

    fast = ts.get("ash_fast", mid); slow = ts.get("ash_slow", mid)
    fast = FAST_EMA_ALPHA * mid + (1 - FAST_EMA_ALPHA) * fast
    slow = SLOW_EMA_ALPHA * mid + (1 - SLOW_EMA_ALPHA) * slow
    ts["ash_fast"] = fast; ts["ash_slow"] = slow
    trend_fair = fast - slow

    ema8 = ts.get("ash_ema8", mid); ema32 = ts.get("ash_ema32", mid)
    ema8 = EMA8_ALPHA * mid + (1 - EMA8_ALPHA) * ema8
    ema32 = EMA32_ALPHA * mid + (1 - EMA32_ALPHA) * ema32
    ts["ash_ema8"] = ema8; ts["ash_ema32"] = ema32
    trend = ema8 - ema32

    mspread = int(ask - bid)
    hist = ts.setdefault("ash_mid_hist", [])
    hist.append(mid)
    if len(hist) > VOL_WINDOW + 1:
        del hist[0 : len(hist) - (VOL_WINDOW + 1)]
    if len(hist) >= 3:
        diffs = [hist[i]-hist[i-1] for i in range(1, len(hist))]
        m = sum(diffs)/len(diffs)
        vol20 = (sum((d-m)**2 for d in diffs)/len(diffs))**0.5
    else:
        vol20 = 0.0

    anchor_dev = mid - ASH_ANCHOR
    inv_frac = abs(pos) / ASH_LIMIT

    # Buckets
    spread_b = ("tight" if mspread < STATE_PARAMS["spread_tight"]
                else "wide" if mspread > STATE_PARAMS["spread_wide"] else "normal")
    trend_b  = ("uptrend" if trend_fair > STATE_PARAMS["trend_neutral_band"]
                else "downtrend" if trend_fair < -STATE_PARAMS["trend_neutral_band"] else "neutral")
    vol_b    = ("low" if vol20 < STATE_PARAMS["vol_low"]
                else "high" if vol20 > STATE_PARAMS["vol_high"] else "medium")
    dev_b    = ("near" if abs(anchor_dev) < STATE_PARAMS["anchor_near_band"]
                else "below" if anchor_dev < 0 else "above")
    if   inv_frac >= 0.95: inv_b = "extreme"
    elif inv_frac >= STATE_PARAMS["inv_high"]: inv_b = "high"
    elif inv_frac >= STATE_PARAMS["inv_med"]:  inv_b = "medium"
    else: inv_b = "low"

    # Classify state
    if inv_b in ("high", "extreme"):
        state_label = "D_INVENTORY"
    elif spread_b == "wide" or vol_b == "high":
        state_label = "C_WIDE_JUMPY"
    else:
        streak = ts.get("ash_dev_streak", 0)
        streak = streak + 1 if dev_b in ("below", "above") else 0
        ts["ash_dev_streak"] = streak
        if streak >= ANCHOR_DEV_STREAK_TICKS:
            state_label = "E_MODEL_DISAGREE"
        elif trend_b != "neutral" or dev_b != "near":
            state_label = "B_TRENDING"
        else:
            state_label = "A_STABLE"

    behavior = BEHAVIOR[state_label]

    # State-specific fair (step 10)
    w_anchor = behavior["anchor_w"] if behavior["anchor_w"] is not None else FV_W_ANCHOR
    remain = 1.0 - w_anchor
    scale  = remain / (FV_W_MID + FV_W_MICRO + FV_W_EMA)
    fair = (w_anchor * ASH_ANCHOR
            + FV_W_MID * scale * mid
            + FV_W_MICRO * scale * micro
            + FV_W_EMA * scale * ema)

    # Half-spread (step 4)
    uncertainty = abs(mid - fair)
    A_sp = _A_spread(mspread)
    A_v  = _A_vol(vol20)
    A_t  = _A_trend(abs(trend))
    A_i  = _A_inv(inv_frac)
    A_u  = _A_uncertainty(uncertainty)
    score = A_sp + A_v + A_t + A_i + A_u
    hs_sym = _score_to_hs(score)
    hs_bid = hs_ask = hs_sym

    if pos > 0:
        if   inv_frac >= 0.85: hs_bid += 4; hs_ask -= 2
        elif inv_frac >= 0.70: hs_bid += 2; hs_ask -= 1
        elif inv_frac >= 0.50: hs_bid += 1
    elif pos < 0:
        if   inv_frac >= 0.85: hs_ask += 4; hs_bid -= 2
        elif inv_frac >= 0.70: hs_ask += 2; hs_bid -= 1
        elif inv_frac >= 0.50: hs_ask += 1

    if   trend >=  2: hs_bid -= 1; hs_ask += 1
    elif trend <= -2: hs_bid += 1; hs_ask -= 1

    unc_streak = ts.get("ash_unc_streak", 0)
    unc_streak = unc_streak + 1 if uncertainty >= 4 else 0
    ts["ash_unc_streak"] = unc_streak
    if unc_streak >= UNC_STREAK_TICKS and uncertainty >= 6:
        hs_bid += 2; hs_ask += 2
    elif unc_streak >= UNC_STREAK_TICKS:
        hs_bid += 1; hs_ask += 1

    in_em = ts.get("ash_emergency", False)
    if inv_frac >= 0.90:
        in_em = True
    elif in_em and (inv_frac < 0.60 or pos == 0):
        in_em = False
    ts["ash_emergency"] = in_em
    if in_em:
        if pos > 0:   hs_bid = HS_MAX + 3; hs_ask = 1
        elif pos < 0: hs_bid = 1;          hs_ask = HS_MAX + 3

    hs_bid = min(max(hs_bid, HS_MIN), HS_MAX + 2)
    hs_ask = min(max(hs_ask, HS_MIN), HS_MAX + 2)
    reservation = fair - ASH_K_INV * (pos / ASH_LIMIT)

    # Quote placement
    bid_q = round(reservation) - hs_bid
    ask_q = round(reservation) + hs_ask
    bid_q = min(bid_q, ask - 1)
    ask_q = max(ask_q, bid + 1)
    if behavior["inside"]:
        bid_q = min(bid_q, bid + 1)
        ask_q = max(ask_q, ask - 1)
    else:
        bid_q = min(bid_q, bid)
        ask_q = max(ask_q, ask)

    # Taking (step 5)
    orders = []
    cur = pos
    heavy_long  = pos > 0 and inv_frac >= HEAVY_INV_FRAC
    heavy_short = pos < 0 and inv_frac >= HEAVY_INV_FRAC
    strong_up   = trend >=  2.0
    strong_dn   = trend <= -2.0
    deep        = behavior["take_edge"] + DEEP_EDGE_EXTRA

    for ap in sorted(od.sell_orders):
        if cur >= ASH_LIMIT: break
        if behavior["flatten"] and pos < 0: req = 0
        elif heavy_long or strong_dn:       req = deep
        else:                                req = behavior["take_edge"]
        if ap > fair - req: break
        q = min(abs(od.sell_orders[ap]), ASH_LIMIT - cur)
        if q > 0:
            orders.append(Order(prod, ap, q)); cur += q
    for bp in sorted(od.buy_orders, reverse=True):
        if cur <= -ASH_LIMIT: break
        if behavior["flatten"] and pos > 0: req = 0
        elif heavy_short or strong_up:      req = deep
        else:                                req = behavior["take_edge"]
        if bp < fair + req: break
        q = min(od.buy_orders[bp], ASH_LIMIT + cur)
        if q > 0:
            orders.append(Order(prod, bp, -q)); cur -= q

    # Passive quotes with size_mult
    size = max(1, int(ASH_BASE_SIZE * behavior["size_mult"]))
    if not behavior["both"]:
        if pos > 0:
            q = min(size, ASH_LIMIT + pos)
            if q > 0: orders.append(Order(prod, ask_q, -q))
        elif pos < 0:
            q = min(size, ASH_LIMIT - pos)
            if q > 0: orders.append(Order(prod, bid_q, q))
    else:
        if cur < ASH_LIMIT:
            q = min(size, ASH_LIMIT - cur)
            if q > 0: orders.append(Order(prod, bid_q, q))
        if cur > -ASH_LIMIT:
            q = min(size, ASH_LIMIT + cur)
            if q > 0: orders.append(Order(prod, ask_q, -q))

    # Inventory band filter (step 6) — still applies to ASH
    band = 1 if inv_frac < BAND_1_MAX else (2 if inv_frac < BAND_2_MAX else
                                             (3 if inv_frac < BAND_3_MAX else 4))
    filtered = []
    for o in orders:
        is_buy = o.quantity > 0
        worsening = (pos > 0 and is_buy) or (pos < 0 and not is_buy)
        if band == 1:
            filtered.append(o)
        elif band == 2:
            if worsening:
                new_q = int(o.quantity * 0.5)
                if new_q != 0:
                    filtered.append(Order(prod, o.price, new_q))
            else:
                filtered.append(o)
        elif band == 3:
            if not worsening: filtered.append(o)
        else:
            if not worsening: filtered.append(o)
    return filtered


# ======================================================================
# PEPPER: v12 PROVEN ENGINE — DO NOT TOUCH
# ======================================================================
PEPPER_LIMIT = 80
PEPPER_ANCHOR_INIT = 10000
PEPPER_EMA_ALPHA = 0.02
PEPPER_DIP_K = 2


def trade_pepper(state, ts):
    prod = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(prod)
    if od is None:
        return []
    pos = state.position.get(prod, 0)
    bid, ask = bb_ba(od)
    if bid is None or ask is None:
        return []
    mid = (bid + ask) / 2

    ema = ts.get("pep_ema", mid)
    ema = PEPPER_EMA_ALPHA * mid + (1 - PEPPER_EMA_ALPHA) * ema
    ts["pep_ema"] = ema

    orders = []
    cur = pos

    # Max long engine — take all asks to limit
    for ap in sorted(od.sell_orders):
        if cur >= PEPPER_LIMIT: break
        q = min(abs(od.sell_orders[ap]), PEPPER_LIMIT - cur)
        if q > 0:
            orders.append(Order(prod, ap, q)); cur += q

    rem = PEPPER_LIMIT - cur
    if rem <= 0:
        return orders

    # Dip overlay
    if mid < ema - PEPPER_DIP_K:
        for offset, sz in [(0, 35), (-1, 25), (-2, 20)]:
            if rem <= 0: break
            q = min(sz, rem)
            px = bid + offset
            if px >= ask: px = ask - 1
            orders.append(Order(prod, px, q)); rem -= q
    else:
        orders.append(Order(prod, bid, rem))
    return orders


# ======================================================================
# Trader
# ======================================================================
class Trader:
    def run(self, state):
        orders = {}
        ts = json.loads(state.traderData) if state.traderData else {}
        for prod in state.order_depths:
            if prod == "ASH_COATED_OSMIUM":
                orders[prod] = trade_ash(state, ts)
            elif prod == "INTARIAN_PEPPER_ROOT":
                orders[prod] = trade_pepper(state, ts)
        ts.pop("diag_log", None)   # strip any legacy bloat
        return orders, 0, json.dumps(ts)
