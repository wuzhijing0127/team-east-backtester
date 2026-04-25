"""
r3_structured_v1 — full 4-module pipeline per user spec (steps 1-9)
====================================================================
Pipeline per tick (per product):
  1. Feature extractor    — fair, mid, micro, spread, imbalance, vol, trend, inventory
  2. Regime classifier    — A_STABLE / B_TRENDING / C_WIDE_JUMPY / D_INVENTORY / E_MODEL_DISAGREE
  3. Strategy policy      — dynamic half-spread (4), taking logic (5), inventory bands (6)
  4. Risk manager         — vetoes worsening orders; regime-memory protection (7)
  + diagnostics logging per tick + by-state summary (8)
"""

import json
from datamodel import Order, OrderDepth, TradingState

MAF = 0   # round 3 confirmed MAF ignored


# ======================================================================
# PRODUCT CONFIG
# ======================================================================
PRODUCT_CONFIG = {
    "ASH_COATED_OSMIUM": {
        "anchor":     10000,
        "limit":      20,
        "k_inv":      2.5,
        "base_size":  8,
        # State A is the only state where aggressive inside-market quoting is allowed
    },
    "INTARIAN_PEPPER_ROOT": {
        # PEPPER drifts; anchor should adapt. Start with rolling-mid init; weight dynamically
        "anchor":     12000,        # initial; replaced by rolling value after first tick
        "limit":      80,
        "k_inv":      1.0,          # lighter skew — PEPPER wants max long
        "base_size":  20,
    },
}

# ======================================================================
# STEP 1: FAIR VALUE MODEL
# ======================================================================
FV_W_ANCHOR = 0.20
FV_W_MID    = 0.35
FV_W_MICRO  = 0.25
FV_W_EMA    = 0.20

FAST_EMA_ALPHA = 0.05
SLOW_EMA_ALPHA = 0.01


# ======================================================================
# STEP 2: STATE DETECTION THRESHOLDS
# ======================================================================
STATE_PARAMS = {
    "spread_tight":       5,
    "spread_wide":        15,
    "trend_neutral_band": 1.0,
    "imbalance_band":     0.2,
    "vol_window":         20,
    "vol_low":            0.5,
    "vol_high":           2.0,
    "anchor_near_band":   3.0,
    "inv_low":            0.4,
    "inv_med":            0.7,
    "inv_high":           0.9,
}


# ======================================================================
# STEP 3: STATE-BASED BEHAVIOR
# ======================================================================
ANCHOR_DEV_STREAK_TICKS = 15

BEHAVIOR = {
    "A_STABLE": {
        "quote_both_sides":       True,
        "take_edge":              1,
        "size_mult":              1.0,
        "allow_inside_market":    True,
        "anchor_weight_override": None,
        "flatten_mode":           False,
    },
    "B_TRENDING": {
        "quote_both_sides":       True,
        "take_edge":              2,
        "size_mult":              0.6,
        "allow_inside_market":    False,
        "anchor_weight_override": 0.05,
        "flatten_mode":           False,
    },
    "C_WIDE_JUMPY": {
        "quote_both_sides":       True,
        "take_edge":              3,
        "size_mult":              0.4,
        "allow_inside_market":    False,
        "anchor_weight_override": None,
        "flatten_mode":           False,
    },
    "D_INVENTORY": {
        "quote_both_sides":       False,
        "take_edge":              0,
        "size_mult":              0.5,
        "allow_inside_market":    False,
        "anchor_weight_override": None,
        "flatten_mode":           True,
    },
    "E_MODEL_DISAGREE": {
        "quote_both_sides":       True,
        "take_edge":              2,
        "size_mult":              0.5,
        "allow_inside_market":    False,
        "anchor_weight_override": 0.05,
        "flatten_mode":           False,
    },
}


# ======================================================================
# STEP 4: DYNAMIC HALF-SPREAD CONSTANTS
# ======================================================================
HS_BASE = 1
HS_MIN  = 1
HS_MAX  = 5
EMA8_ALPHA  = 2 / 9
EMA32_ALPHA = 2 / 33
VOL_WINDOW_HS   = 20
UNC_STREAK_TICKS = 5

# ======================================================================
# STEP 5: TAKING
# ======================================================================
DEEP_EDGE_EXTRA = 2
HEAVY_INV_FRAC  = 0.7

# ======================================================================
# STEP 6: INVENTORY BANDS
# ======================================================================
BAND_1_MAX = 0.30
BAND_2_MAX = 0.60
BAND_3_MAX = 0.80

# ======================================================================
# STEP 7: REGIME MEMORY / LONG-HORIZON PROTECTION
# ======================================================================
REGIME_TREND_WINDOW       = 50
FAR_ANCHOR_TICKS_PROTECT  = 30
FAR_ANCHOR_THRESHOLD      = 5
NEG_PNL_STATE_TRIGGER     = -500
CONSERVATIVE_DURATION     = 100    # ticks to stay conservative once triggered

# ======================================================================
# STEP 8: DIAGNOSTICS
# ======================================================================
DIAG_KEEP_LAST = 0   # don't persist diag_log (was causing traderData bloat/server hang)


# ======================================================================
# HELPERS
# ======================================================================
def bb_ba(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


# ======================================================================
# MODULE 1: FEATURE EXTRACTOR
# ======================================================================
def extract_features(od, ts_state, product_key, cfg, position):
    """Compute all per-tick features: fair, state-detection inputs, spread/vol/trend features."""
    bid, ask = bb_ba(od)
    if bid is None or ask is None:
        return None
    mid = (bid + ask) / 2

    # --- Microprice ---
    bv = od.buy_orders[bid]
    av = abs(od.sell_orders[ask])
    micro = (bid * av + ask * bv) / (bv + av) if (bv + av) > 0 else mid

    # --- Anchor (PEPPER: use rolling baseline to adapt) ---
    anchor = cfg["anchor"]
    if product_key == "pepper":
        # PEPPER anchor drifts; use slow-EMA baseline
        rolling_anchor = ts_state.get(f"anchor_{product_key}", mid)
        rolling_anchor = 0.002 * mid + 0.998 * rolling_anchor
        ts_state[f"anchor_{product_key}"] = rolling_anchor
        anchor = rolling_anchor

    # --- Fair-value EMA (blended weight) ---
    ema_key = f"fv_ema_{product_key}"
    ema = ts_state.get(ema_key, anchor)
    ema = FAST_EMA_ALPHA * mid + (1 - FAST_EMA_ALPHA) * ema
    ts_state[ema_key] = ema

    # --- fair_short / fair_long for trend direction ---
    fs_key = f"fv_short_{product_key}"
    fl_key = f"fv_long_{product_key}"
    fair_short = ts_state.get(fs_key, mid)
    fair_long  = ts_state.get(fl_key, mid)
    fair_short = FAST_EMA_ALPHA * mid + (1 - FAST_EMA_ALPHA) * fair_short
    fair_long  = SLOW_EMA_ALPHA * mid + (1 - SLOW_EMA_ALPHA) * fair_long
    ts_state[fs_key] = fair_short
    ts_state[fl_key] = fair_long
    trend_fair = fair_short - fair_long

    # --- 8/32 tick EMA for half-spread trend feature ---
    ema8  = ts_state.get(f"ema8_{product_key}",  mid)
    ema32 = ts_state.get(f"ema32_{product_key}", mid)
    ema8  = EMA8_ALPHA  * mid + (1 - EMA8_ALPHA)  * ema8
    ema32 = EMA32_ALPHA * mid + (1 - EMA32_ALPHA) * ema32
    ts_state[f"ema8_{product_key}"]  = ema8
    ts_state[f"ema32_{product_key}"] = ema32
    trend_hs = ema8 - ema32

    # --- Spread ---
    mspread = int(ask - bid)

    # --- Volatility (std of mid changes) ---
    hist = ts_state.setdefault(f"mid_hist_{product_key}", [])
    hist.append(mid)
    if len(hist) > VOL_WINDOW_HS + 1:
        del hist[0 : len(hist) - (VOL_WINDOW_HS + 1)]
    if len(hist) >= 3:
        diffs = [hist[i] - hist[i-1] for i in range(1, len(hist))]
        m = sum(diffs) / len(diffs)
        vol20 = (sum((d - m) ** 2 for d in diffs) / len(diffs)) ** 0.5
    else:
        vol20 = 0.0

    # --- Book imbalance ---
    total = bv + av
    imbalance = (bv - av) / total if total > 0 else 0.0

    # --- Anchor deviation ---
    anchor_dev = mid - anchor

    # --- Blended fair (may have override from regime) ---
    w_anchor = ts_state.get(f"w_anchor_override_{product_key}", FV_W_ANCHOR)
    # Normalize remaining weights
    remaining = 1.0 - w_anchor
    remaining_base = FV_W_MID + FV_W_MICRO + FV_W_EMA
    scale = remaining / remaining_base
    fair = (w_anchor * anchor
            + FV_W_MID   * scale * mid
            + FV_W_MICRO * scale * micro
            + FV_W_EMA   * scale * ema)

    # --- Inventory fraction ---
    inv_frac = abs(position) / cfg["limit"] if cfg["limit"] > 0 else 0.0

    return {
        "bid": bid, "ask": ask, "mid": mid, "micro": micro,
        "anchor": anchor, "ema": ema,
        "fair": fair, "fair_short": fair_short, "fair_long": fair_long,
        "trend_fair": trend_fair,
        "ema8": ema8, "ema32": ema32, "trend": trend_hs, "trend_mag": abs(trend_hs),
        "mspread": mspread, "vol20": vol20,
        "imbalance": imbalance, "anchor_dev": anchor_dev,
        "inv_frac": inv_frac, "position": position,
    }


# ======================================================================
# MODULE 2: REGIME CLASSIFIER
# ======================================================================
def classify_regime(features, ts_state, product_key):
    """Bucket features into 6 dims then pick composite state by priority D > C > E > B > A."""
    p = STATE_PARAMS

    # Dim buckets
    spread_b = ("tight" if features["mspread"] < p["spread_tight"]
                else "wide" if features["mspread"] > p["spread_wide"] else "normal")
    trend_b  = ("uptrend" if features["trend_fair"] >  p["trend_neutral_band"]
                else "downtrend" if features["trend_fair"] < -p["trend_neutral_band"] else "neutral")
    imb_b    = ("buy_pressure" if features["imbalance"] >  p["imbalance_band"]
                else "sell_pressure" if features["imbalance"] < -p["imbalance_band"] else "balanced")
    vol_b    = ("low" if features["vol20"] < p["vol_low"]
                else "high" if features["vol20"] > p["vol_high"] else "medium")
    dev_b    = ("near" if abs(features["anchor_dev"]) < p["anchor_near_band"]
                else "below" if features["anchor_dev"] < 0 else "above")
    if   features["inv_frac"] >= 0.95: inv_b = "extreme"
    elif features["inv_frac"] >= p["inv_high"]: inv_b = "high"
    elif features["inv_frac"] >= p["inv_med"]:  inv_b = "medium"
    else: inv_b = "low"

    # Composite state (priority order)
    # D — inventory stress
    if inv_b in ("high", "extreme"):
        state = "D_INVENTORY"
    # C — wide/jumpy
    elif spread_b == "wide" or vol_b == "high":
        state = "C_WIDE_JUMPY"
    else:
        # E streak tracking
        dev_key = f"anchor_dev_streak_{product_key}"
        streak = ts_state.get(dev_key, 0)
        streak = streak + 1 if dev_b in ("below", "above") else 0
        ts_state[dev_key] = streak
        if streak >= ANCHOR_DEV_STREAK_TICKS:
            state = "E_MODEL_DISAGREE"
        elif trend_b != "neutral" or dev_b != "near":
            state = "B_TRENDING"
        else:
            state = "A_STABLE"

    # Apply regime-memory anchor weight override
    behavior = dict(BEHAVIOR[state])   # copy so we can modify
    if behavior["anchor_weight_override"] is not None:
        ts_state[f"w_anchor_override_{product_key}"] = behavior["anchor_weight_override"]
    else:
        ts_state[f"w_anchor_override_{product_key}"] = FV_W_ANCHOR

    buckets = {"spread": spread_b, "trend": trend_b, "imbalance": imb_b,
               "vol": vol_b, "anchor_dev": dev_b, "inventory": inv_b}
    return state, behavior, buckets


# ======================================================================
# MODULE 3: STRATEGY POLICY (Step 4: half-spread + Step 5: taking)
# ======================================================================
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


def compute_halfspread(features, behavior, ts_state, product_key, cfg):
    """Compute asymmetric (hs_bid, hs_ask) + reservation price."""
    pos       = features["position"]
    limit     = cfg["limit"]
    inv_frac  = features["inv_frac"]
    uncertainty = abs(features["mid"] - features["fair"])
    trend     = features["trend"]

    # Score-based symmetric baseline
    A_sp = _A_spread(features["mspread"])
    A_v  = _A_vol(features["vol20"])
    A_t  = _A_trend(features["trend_mag"])
    A_i  = _A_inv(inv_frac)
    A_u  = _A_uncertainty(uncertainty)
    A_fr = 0   # fill-risk: requires own_trades history; placeholder per spec fallback
    score  = A_sp + A_v + A_t + A_i + A_u + A_fr
    hs_sym = _score_to_hs(score)
    hs_bid = hs_ask = hs_sym

    # Step 4.5 — inventory-side asymmetry
    if pos > 0:
        if   inv_frac >= 0.85: hs_bid += 4; hs_ask -= 2
        elif inv_frac >= 0.70: hs_bid += 2; hs_ask -= 1
        elif inv_frac >= 0.50: hs_bid += 1
    elif pos < 0:
        if   inv_frac >= 0.85: hs_ask += 4; hs_bid -= 2
        elif inv_frac >= 0.70: hs_ask += 2; hs_bid -= 1
        elif inv_frac >= 0.50: hs_ask += 1

    # Step 4.6 — trend-side asymmetry
    if   trend >=  2: hs_bid -= 1; hs_ask += 1
    elif trend <= -2: hs_bid += 1; hs_ask -= 1

    # Step 4.7 — uncertainty sustained override
    unc_key = f"unc_streak_{product_key}"
    streak  = ts_state.get(unc_key, 0)
    streak  = streak + 1 if uncertainty >= 4 else 0
    ts_state[unc_key] = streak
    if streak >= UNC_STREAK_TICKS and uncertainty >= 6:
        hs_bid += 2; hs_ask += 2
    elif streak >= UNC_STREAK_TICKS:
        hs_bid += 1; hs_ask += 1

    # Step 4.8 — emergency mode (sticky, exits at inv_frac < 0.60 or pos==0)
    em_key = f"emergency_{product_key}"
    in_em = ts_state.get(em_key, False)
    if inv_frac >= 0.90:
        in_em = True
    elif in_em and (inv_frac < 0.60 or pos == 0):
        in_em = False
    ts_state[em_key] = in_em
    if in_em:
        if pos > 0:
            hs_bid = HS_MAX + 3; hs_ask = 1
        elif pos < 0:
            hs_bid = 1;           hs_ask = HS_MAX + 3

    # Clamp
    hs_bid = min(max(hs_bid, HS_MIN), HS_MAX + 2)
    hs_ask = min(max(hs_ask, HS_MIN), HS_MAX + 2)

    # Reservation price with inventory skew
    reservation = features["fair"] - cfg["k_inv"] * (pos / limit if limit > 0 else 0)

    hs_diag = {
        "A_spread": A_sp, "A_vol": A_v, "A_trend": A_t,
        "A_inv": A_i, "A_uncertainty": A_u, "A_fillrisk": A_fr,
        "score": score, "hs_sym": hs_sym,
        "hs_bid": hs_bid, "hs_ask": hs_ask,
        "reservation": round(reservation, 2),
        "uncertainty": round(uncertainty, 3),
        "unc_streak": streak, "emergency": in_em,
    }
    return hs_bid, hs_ask, reservation, hs_diag


def place_passive_quotes(features, behavior, hs_bid, hs_ask, reservation, cfg):
    """Step 4.9 — quote placement with stable-state allowance."""
    best_bid = features["bid"]
    best_ask = features["ask"]
    bid_q = round(reservation) - hs_bid
    ask_q = round(reservation) + hs_ask

    bid_q = min(bid_q, best_ask - 1)
    ask_q = max(ask_q, best_bid + 1)

    if behavior["allow_inside_market"]:
        # Stable state: aggressive inside-market allowed
        bid_q = min(bid_q, best_bid + 1)
        ask_q = max(ask_q, best_ask - 1)
    else:
        # Non-stable: only join at best (don't undercut)
        bid_q = min(bid_q, best_bid)
        ask_q = max(ask_q, best_ask)

    return bid_q, ask_q


def compute_taking(prod, od, features, behavior, position, limit):
    """Step 5 — stricter taking logic separate from passive quoting."""
    orders = []
    if od is None:
        return orders, {}

    fair      = features["fair"]
    trend     = features["trend"]
    inv_frac  = features["inv_frac"]
    take_edge = behavior["take_edge"]

    heavy_long  = position > 0 and inv_frac >= HEAVY_INV_FRAC
    heavy_short = position < 0 and inv_frac >= HEAVY_INV_FRAC
    strong_up   = trend >=  2.0
    strong_dn   = trend <= -2.0
    deep        = take_edge + DEEP_EDGE_EXTRA
    flatten     = behavior["flatten_mode"]

    cur = position
    took_buy = took_sell = 0

    # BUY ASK
    for ap in sorted(od.sell_orders):
        if cur >= limit:
            break
        if flatten and position < 0:
            req = 0
        elif heavy_long or strong_dn:
            req = deep
        else:
            req = take_edge
        if ap > fair - req:
            break
        q = min(abs(od.sell_orders[ap]), limit - cur)
        if q > 0:
            orders.append(Order(prod, ap, q))
            cur += q
            took_buy += q

    # SELL BID
    for bp in sorted(od.buy_orders, reverse=True):
        if cur <= -limit:
            break
        if flatten and position > 0:
            req = 0
        elif heavy_short or strong_up:
            req = deep
        else:
            req = take_edge
        if bp < fair + req:
            break
        q = min(od.buy_orders[bp], limit + cur)
        if q > 0:
            orders.append(Order(prod, bp, -q))
            cur -= q
            took_sell += q

    take_diag = {"take_edge": take_edge, "deep_edge": deep,
                 "heavy_long": heavy_long, "heavy_short": heavy_short,
                 "strong_up": strong_up, "strong_dn": strong_dn,
                 "flatten": flatten, "took_buy": took_buy, "took_sell": took_sell}
    return orders, take_diag


# ======================================================================
# MODULE 4: RISK MANAGER (Step 6 + Step 7)
# ======================================================================
def inventory_band(inv_frac):
    if inv_frac < BAND_1_MAX:  return 1
    if inv_frac < BAND_2_MAX:  return 2
    if inv_frac < BAND_3_MAX:  return 3
    return 4


def risk_filter(prod, proposed_orders, features, ts_state, cfg, product_key, state_label):
    """Veto/reduce orders per inventory bands (Step 6) + regime memory protection (Step 7)."""
    pos      = features["position"]
    limit    = cfg["limit"]
    inv_frac = features["inv_frac"]
    band     = inventory_band(inv_frac)

    # Step 7 — regime memory: track trend, far-from-anchor ticks, conservative mode
    mem = ts_state.setdefault(f"regime_mem_{product_key}", {
        "trend_hist": [], "far_anchor_ticks": 0,
        "pnl_by_state": {}, "conservative_until_ts": 0,
    })
    mem["trend_hist"].append(features["trend"])
    if len(mem["trend_hist"]) > REGIME_TREND_WINDOW:
        del mem["trend_hist"][0:len(mem["trend_hist"]) - REGIME_TREND_WINDOW]

    if abs(features["anchor_dev"]) > FAR_ANCHOR_THRESHOLD:
        mem["far_anchor_ticks"] += 1
    else:
        mem["far_anchor_ticks"] = 0

    # Protection rule 1: sustained far-from-anchor + we're trying to sell against trend
    protect_against_sell = False
    if (mem["far_anchor_ticks"] >= FAR_ANCHOR_TICKS_PROTECT
            and features["anchor_dev"] > 0
            and features["trend"] > 0):
        protect_against_sell = True   # don't sell into an uptrend far above anchor

    protect_against_buy = False
    if (mem["far_anchor_ticks"] >= FAR_ANCHOR_TICKS_PROTECT
            and features["anchor_dev"] < 0
            and features["trend"] < 0):
        protect_against_buy = True

    # Step 6 — inventory bands: filter/reduce orders
    filtered = []
    for o in proposed_orders:
        is_buy = o.quantity > 0
        # Worsening side (adds to existing position direction)
        worsening_long  = pos > 0 and is_buy
        worsening_short = pos < 0 and (not is_buy)
        worsening = worsening_long or worsening_short

        # Regime memory vetoes
        if protect_against_sell and not is_buy and pos <= 0:
            continue   # don't ADD short into uptrend above anchor
        if protect_against_buy and is_buy and pos >= 0:
            continue

        # Band rules
        if band == 1:
            filtered.append(o)
        elif band == 2:
            # reduce size on worsening side
            if worsening:
                new_q = int(o.quantity * 0.5)
                if new_q == 0:
                    continue
                filtered.append(Order(prod, o.price, new_q))
            else:
                filtered.append(o)
        elif band == 3:
            # drop worsening; keep flattening
            if worsening:
                continue
            filtered.append(o)
        else:   # band 4 — emergency: only flattening side
            if worsening:
                continue
            filtered.append(o)

    risk_diag = {
        "band": band,
        "far_anchor_ticks": mem["far_anchor_ticks"],
        "protect_sell": protect_against_sell,
        "protect_buy":  protect_against_buy,
        "orders_in":    len(proposed_orders),
        "orders_out":   len(filtered),
    }
    return filtered, risk_diag


# ======================================================================
# PASSIVE QUOTE SIZING (combines behavior + inventory band)
# ======================================================================
def sized_passive_orders(prod, features, behavior, hs_bid, hs_ask, reservation, cfg):
    """Produce passive bid+ask orders with size scaled by behavior + inventory band."""
    pos = features["position"]
    limit = cfg["limit"]
    inv_frac = features["inv_frac"]

    bid_q, ask_q = place_passive_quotes(features, behavior, hs_bid, hs_ask, reservation, cfg)

    orders = []
    size_base = cfg["base_size"]
    size = max(1, int(size_base * behavior["size_mult"]))

    quote_both = behavior["quote_both_sides"]
    if not quote_both:
        # D_INVENTORY state — only the flattening side
        if pos > 0:
            q = min(size, limit + pos)
            if q > 0:
                orders.append(Order(prod, ask_q, -q))
        elif pos < 0:
            q = min(size, limit - pos)
            if q > 0:
                orders.append(Order(prod, bid_q, q))
    else:
        if pos < limit:
            q = min(size, limit - pos)
            if q > 0:
                orders.append(Order(prod, bid_q, q))
        if pos > -limit:
            q = min(size, limit + pos)
            if q > 0:
                orders.append(Order(prod, ask_q, -q))

    return orders


# ======================================================================
# TOP-LEVEL PER-PRODUCT TICK
# ======================================================================
def process_product(state, ts_data, product_name, product_key):
    cfg = PRODUCT_CONFIG[product_name]
    od  = state.order_depths.get(product_name)
    if od is None:
        return [], None

    pos = state.position.get(product_name, 0)

    # Module 1: features
    features = extract_features(od, ts_data, product_key, cfg, pos)
    if features is None:
        return [], None

    # Module 2: classify
    state_label, behavior, buckets = classify_regime(features, ts_data, product_key)

    # Module 3: strategy policy
    hs_bid, hs_ask, reservation, hs_diag = compute_halfspread(
        features, behavior, ts_data, product_key, cfg)

    # Passive + taking
    passive = sized_passive_orders(
        product_name, features, behavior, hs_bid, hs_ask, reservation, cfg)
    takes, take_diag = compute_taking(
        product_name, od, features, behavior, pos, cfg["limit"])

    proposed = passive + takes

    # Module 4: risk filter
    filtered, risk_diag = risk_filter(
        product_name, proposed, features, ts_data, cfg, product_key, state_label)

    # Step 8: diagnostics bundle for this tick
    diag = {
        "product": product_key, "state": state_label, "buckets": buckets,
        "mid": round(features["mid"], 2),
        "fair": round(features["fair"], 2),
        "anchor": round(features["anchor"], 2),
        "micro": round(features["micro"], 2),
        "trend": round(features["trend"], 3),
        "spread": features["mspread"], "vol20": round(features["vol20"], 3),
        "imbalance": round(features["imbalance"], 3),
        "anchor_dev": round(features["anchor_dev"], 3),
        "pos": pos, "inv_frac": round(features["inv_frac"], 3),
        **hs_diag, **take_diag, **risk_diag,
    }
    return filtered, diag


# ======================================================================
# Trader entrypoint
# ======================================================================
class Trader:
    def run(self, state):
        orders = {}
        ts_data = json.loads(state.traderData) if state.traderData else {}
        timestamp = getattr(state, "timestamp", 0)

        for prod in state.order_depths:
            if prod == "ASH_COATED_OSMIUM":
                result, _ = process_product(state, ts_data, prod, "ash")
            elif prod == "INTARIAN_PEPPER_ROOT":
                result, _ = process_product(state, ts_data, prod, "pepper")
            else:
                continue
            orders[prod] = result

        # Keep ts_data lean — strip any legacy bloat from prior runs
        ts_data.pop("diag_log", None)

        return orders, 0, json.dumps(ts_data)
