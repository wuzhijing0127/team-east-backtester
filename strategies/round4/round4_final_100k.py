"""Round 4 final submission — 100K timescale variant.

Same as round4_final.py but rescaled to the 100K platform timescale
(matches the artifact ts range 100..99900 observed in actual platform runs).

Three engines, each transplanted whole from its source-of-truth:
  - VELVETFRUIT_EXTRACT — flow-MM with sweep-frozen alpha.
  - HYDROGEL_PACK       — flow-MM with HG_strat.py params (verbatim).
  - 10 VEV options      — entire options engine from options_strat.py
    (smile, IV EMA, residual-momentum sell filter, optional VFE delta hedge).

Time scale: 100_000.
  - HG / VFE flow-MM closeout: ts >= 99_800
  - Options taper-unwind:      ts >= 99_000
  - Options hard unwind:       ts >= 99_800
"""

import json
import math
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from datamodel import Order, OrderDepth, TradingState


# ====================================================================
# Universe & timing
# ====================================================================
VFE = "VELVETFRUIT_EXTRACT"
HG  = "HYDROGEL_PACK"
STRIKES = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]
VEV = {k: f"VEV_{k}" for k in STRIKES}
LIMITS = {VFE: 200, HG: 200, **{VEV[k]: 300 for k in STRIKES}}

SMILE_STRIKES = [5000, 5100, 5200, 5300, 5400, 5500]
ACTIVE_STRIKES = [5200, 5300]
POSITION_CAP_BY_STRIKE: dict = {5200: 50, 5300: 100}

SESSION_END              = 100_000
UNDERLYING_UNWIND_START  = 99_800    # HG / VFE flow-MM closeout
HARD_UNWIND              = 99_800    # options-engine hard liquidation
UNWIND_START_OPTIONS     = 99_000    # options-engine taper start

HIST_DTE_AT_DAY_START = {0: 8, 1: 7, 2: 6, 3: 5}
LIVE_DTE_DEFAULT = 5
BACKTEST_DAY_NUM = None

# Tracks options sells blocked by the residual momentum filter (debug only).
SKIP_COUNT_BY_DAY: dict = defaultdict(int)


# ====================================================================
# Smile shape (static fallback) and per-strike priors (per_strike_iv mode)
# ====================================================================
SMILE_A = 0.5417
SMILE_B = 0.0023
SMILE_C = 0.0121

PRIOR_IV = {
    5000: 0.01267,
    5100: 0.01254,
    5200: 0.01268,
    5300: 0.01282,
    5400: 0.01202,
    5500: 0.01304,
}


# ====================================================================
# Black-Scholes
# ====================================================================
SQRT_2PI = math.sqrt(2 * math.pi)


def _norm_pdf(x): return math.exp(-0.5 * x * x) / SQRT_2PI
def _norm_cdf(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(S, K, T, sigma):
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    return S * _norm_cdf(d1) - K * _norm_cdf(d2)


def bs_call_vega(S, K, T, sigma):
    if T <= 0 or sigma <= 0:
        return 0.0
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * sqrtT)
    return S * _norm_pdf(d1) * sqrtT


def bs_call_delta(S, K, T, sigma):
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * sqrtT)
    return _norm_cdf(d1)


def implied_vol_call(price, S, K, T):
    if T <= 0 or price <= 0:
        return None
    intrinsic = max(0.0, S - K)
    if price - intrinsic <= 0 or price >= S:
        return None
    sigma = 0.5
    for _ in range(40):
        diff = bs_call(S, K, T, sigma) - price
        if abs(diff) < 1e-5:
            return sigma
        v = bs_call_vega(S, K, T, sigma)
        if v < 1e-8:
            break
        sigma_new = sigma - diff / v
        if sigma_new <= 1e-4 or sigma_new > 5.0:
            break
        sigma = sigma_new
    lo, hi = 1e-4, 5.0
    for _ in range(60):
        m = 0.5 * (lo + hi)
        if bs_call(S, K, T, m) > price:
            hi = m
        else:
            lo = m
    return 0.5 * (lo + hi) if hi - lo < 1e-3 else None


def fit_smile(points):
    """Quadratic fit a + b*x + c*x^2 over (m_log/sqrt(T), iv_day) points."""
    n = len(points)
    if n < 3:
        return None
    if n == 3:
        (x0, y0), (x1, y1), (x2, y2) = points
        denom = (x0 - x1) * (x0 - x2) * (x1 - x2)
        if abs(denom) < 1e-12:
            return None
        c = (x2 * (y1 - y0) + x1 * (y0 - y2) + x0 * (y2 - y1)) / denom
        b = (x2 * x2 * (y0 - y1) + x1 * x1 * (y2 - y0) + x0 * x0 * (y1 - y2)) / denom
        a = y0 - b * x0 - c * x0 * x0
        return a, b, c
    sx = sx2 = sx3 = sx4 = sy = sxy = sx2y = 0.0
    for x, y in points:
        x2 = x * x
        sx += x; sx2 += x2; sx3 += x2 * x; sx4 += x2 * x2
        sy += y; sxy += x * y; sx2y += x2 * y
    m = [[n, sx, sx2], [sx, sx2, sx3], [sx2, sx3, sx4]]
    r = [sy, sxy, sx2y]
    det = (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
           - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
           + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    if abs(det) < 1e-14:
        return None

    def _det(a, b, c, d, e, f, g, h, i):
        return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)

    return (
        _det(r[0], m[0][1], m[0][2], r[1], m[1][1], m[1][2], r[2], m[2][1], m[2][2]) / det,
        _det(m[0][0], r[0], m[0][2], m[1][0], r[1], m[1][2], m[2][0], r[2], m[2][2]) / det,
        _det(m[0][0], m[0][1], r[0], m[1][0], m[1][1], r[1], m[2][0], m[2][1], r[2]) / det,
    )


def _tte_days(timestamp, day_num):
    if day_num is None:
        days_left = LIVE_DTE_DEFAULT - timestamp / SESSION_END
    else:
        start_dte = HIST_DTE_AT_DAY_START.get(day_num, LIVE_DTE_DEFAULT)
        days_left = start_dte - timestamp / SESSION_END
    return max(days_left, 1e-4)


# ====================================================================
# Order-book helpers (option-engine flavour)
# ====================================================================
def best_bid_ask(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def mid_price(od):
    b, a = best_bid_ask(od)
    return None if b is None or a is None else 0.5 * (b + a)


def _ema(prev, x, alpha):
    if alpha <= 0:
        return prev
    if prev is None:
        return x
    return prev + alpha * (x - prev)


# ====================================================================
# Order-book helpers (flow-MM flavour — different empty handling)
# ====================================================================
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
    bv = od.buy_orders[bid]
    av = abs(od.sell_orders[ask])
    if bv + av == 0:
        return (bid + ask) / 2
    return (bid * av + ask * bv) / (bv + av)


def get_l1_imbalance(od):
    bid, ask = get_best_bid_ask(od)
    if bid is None or ask is None:
        return 0.0
    bv = od.buy_orders.get(bid, 0)
    av = abs(od.sell_orders.get(ask, 0))
    s = bv + av
    return 0.0 if s == 0 else (bv - av) / s


def get_deep_imbalance(od):
    bvs = sum(od.buy_orders.values())
    avs = sum(abs(v) for v in od.sell_orders.values())
    s = bvs + avs
    return 0.0 if s == 0 else (bvs - avs) / s


def closeout(product, pos, bb, ba, flat_size):
    out = []
    if pos > 0 and bb is not None:
        out.append(Order(product, bb, -min(flat_size, pos)))
    elif pos < 0 and ba is not None:
        out.append(Order(product, ba, min(flat_size, -pos)))
    return out


def push_window(ts: dict, key: str, value: float, max_len: int) -> list:
    buf = ts.setdefault(key, [])
    buf.append(value)
    if len(buf) > max_len:
        del buf[: len(buf) - max_len]
    ts[key] = buf
    return buf


def avg(xs):
    return (sum(xs) / len(xs)) if xs else 0.0


def realized_vol(xs):
    if len(xs) < 3:
        return 0.0
    diffs = [xs[i] - xs[i - 1] for i in range(1, len(xs))]
    m = sum(diffs) / len(diffs)
    var = sum((d - m) ** 2 for d in diffs) / len(diffs)
    return math.sqrt(var)


# ====================================================================
# HG_PARAMS — verbatim from HG_strat.py
# ====================================================================
HG_PARAMS = {
    'limit': 200,
    'soft_cap': 150,
    'half_spread': 6,
    'base_size': 20,
    'take_edge': 8,
    'k_inv': 1.0,
    'micro_coef': 0.5,
    'imb_coef': 0.4,
    'ema_alpha': 0.05,
    'imb_window': 0,
    'trend_coef': 0.45,
    'trend_lag': 5,
    'revert_coef': 0.2,
    'revert_window': 120,
    'micro_trend_coef': 0.0,
    'deep_imb_coef': 0.0,
    'vol_widen_coef': 0.0,
    'asymm_skew': 1.0,
    'long_ema_alpha': 0.0,
    'ema_blend': 0.0,
    'vel_coef': 0.2,
    'vel_alpha': 0.1,
    'revert_gate_threshold': 0.0,
    'stop_pos_threshold': 100,
    'stop_drawdown_threshold': 4000.0,
    'stop_min_ticks_at_pos': 200,
    'stop_unwind_size': 30,
}


# ====================================================================
# VF_PARAMS — sweep-frozen winners (slim alpha + neutral legacy keys
# so the full-schema trade_flow_mm runs cleanly).
# Validated PnL=924, vol=25.88, slope=0.0104 in single-product backtest.
# ====================================================================
VF_PARAMS = {
    # Inventory bounds
    'limit': 200,
    'soft_cap': 150,
    'base_size': 15,
    # Quote width / take threshold
    'half_spread': 1,
    'take_edge': 2,
    # Inventory shade
    'k_inv': 0.5,
    # Alpha — sweep-frozen
    'micro_coef': 0.4,
    'imb_coef':   0.5,
    'trend_coef': 0.3, 'trend_lag': 5,
    'vel_coef':   0.3, 'vel_alpha': 0.1,
    'ema_alpha':  0.0,
    # Legacy schema keys — disabled / neutral so behaviour matches the
    # sweep-tested slim engine.
    'imb_window': 0,
    'revert_coef': 0.0, 'revert_window': 0,
    'micro_trend_coef': 0.0,
    'deep_imb_coef':    0.0,
    'vol_widen_coef':   0.0,
    'asymm_skew':       1.0,
    'long_ema_alpha':   0.0, 'ema_blend': 0.0,
    'revert_gate_threshold': 0.0,
    # Catastrophic insurance
    'stop_pos_threshold': 150,
    'stop_drawdown_threshold': 1500.0,
    'stop_min_ticks_at_pos': 100,
    'stop_unwind_size': 25,
}


# ====================================================================
# Options config — from options_strat.py (unwind_start rescaled to 1M)
# ====================================================================
P = {
    "smile_mode": "static",
    "iv_ema_alpha": 2 / 51,
    "per_tick_min_strikes": 3,

    "abs_edge": 3,            # min absolute edge (theo - ask for buys, bid - theo for sells)
    "spread_mult": 0.5,       # required edge also >= spread_mult * full_spread
    "position_cap": 50,       # fallback when strike not in POSITION_CAP_BY_STRIKE
    "trade_size": 10,

    # Residual momentum filter (sell side only).
    # residual = opt_mid - theo;  residual_mom = residual - residual_ema
    # Skip sell if residual_mom > residual_max_rise.
    "residual_ema_alpha": 2 / 21,
    "residual_max_rise":  0.5,

    "delta_cap": 40.0,
    "delta_hedge_with_vfe": False,

    # Partial hedge (only when delta_hedge_with_vfe=True): rehedge when
    # |net_delta| > hedge_band, then trade toward -round(hedge_ratio * net_delta).
    "hedge_band":  0.0,
    "hedge_ratio": 1.0,

    "unwind_start": UNWIND_START_OPTIONS,   # 99_000 — original options_strat.py value
}


def _load(td):
    if not td:
        return {}
    try:
        return json.loads(td)
    except Exception:
        return {}


def _resolve_day_num():
    return BACKTEST_DAY_NUM


# ====================================================================
# Underlying flow-MM engine — verbatim from HG_strat.py
# (Used by both HG_PARAMS and the padded VF_PARAMS.)
# ====================================================================
def trade_flow_mm(state: TradingState, ts: dict, product: str, p: dict) -> List[Order]:
    od = state.order_depths.get(product)
    if od is None:
        return []
    bb, ba = get_best_bid_ask(od)
    if bb is None or ba is None:
        return []

    pos = state.position.get(product, 0)
    limit = p["limit"]
    soft_cap = p["soft_cap"]

    if state.timestamp >= UNDERLYING_UNWIND_START:
        return closeout(product, pos, bb, ba, 25)

    mid = get_mid(od)
    micro = get_microprice(od)
    spread = ba - bb

    # Imbalance
    imb_now = get_l1_imbalance(od)
    if p["imb_window"] > 0:
        buf = push_window(ts, f"{product}_imb_buf", imb_now, p["imb_window"])
        imb = avg(buf)
    else:
        imb = imb_now

    deep_imb = get_deep_imbalance(od) if p["deep_imb_coef"] != 0 else 0.0

    # Trend
    trend_signal = 0.0
    if p["trend_coef"] != 0 and p["trend_lag"] > 0:
        mid_buf = push_window(ts, f"{product}_mid_buf", mid,
                              max(p["trend_lag"], p.get("revert_window", 0)) + 1)
        if len(mid_buf) > p["trend_lag"]:
            trend_signal = mid - mid_buf[-(p["trend_lag"] + 1)]
    else:
        if p["revert_coef"] != 0 and p["revert_window"] > 0:
            push_window(ts, f"{product}_mid_buf", mid, p["revert_window"] + 1)

    revert_signal = 0.0
    if p["revert_coef"] != 0 and p["revert_window"] > 0:
        mid_buf = ts.get(f"{product}_mid_buf", [])
        if len(mid_buf) >= 5:
            revert_signal = mid - avg(mid_buf)

    micro_trend = 0.0
    if p["micro_trend_coef"] != 0:
        mb = push_window(ts, f"{product}_micro_buf", micro, 6)
        if len(mb) >= 4:
            micro_trend = micro - mb[-4]

    # EMA-velocity
    velocity = 0.0
    vel_coef = p.get("vel_coef", 0.0)
    if vel_coef != 0:
        vel_alpha = p.get("vel_alpha", 0.10)
        prev_mid_key = f"{product}_vel_prev_mid"
        vel_key = f"{product}_smooth_vel"
        prev_mid = ts.get(prev_mid_key, mid)
        delta_mid = mid - prev_mid
        prev_vel = ts.get(vel_key, 0.0)
        velocity = vel_alpha * delta_mid + (1 - vel_alpha) * prev_vel
        ts[vel_key] = velocity
        ts[prev_mid_key] = mid

    # Trend-magnitude gate on revert_coef
    revert_scale = 1.0
    gate_thresh = p.get("revert_gate_threshold", 0.0)
    if gate_thresh > 0 and trend_signal != 0:
        revert_scale = max(0.0, 1.0 - abs(trend_signal) / gate_thresh)

    # Compose flow-driven fair
    base = (mid
            + p["micro_coef"] * (micro - mid)
            + p["imb_coef"] * imb * spread
            + p["deep_imb_coef"] * deep_imb * spread
            + p["trend_coef"] * trend_signal
            - p["revert_coef"] * revert_scale * revert_signal
            + p["micro_trend_coef"] * micro_trend
            + vel_coef * velocity)

    if p["ema_alpha"] > 0:
        ema_key = f"{product}_fair_ema"
        prev = ts.get(ema_key, base)
        fair = p["ema_alpha"] * base + (1 - p["ema_alpha"]) * prev
        ts[ema_key] = fair
    else:
        fair = base

    # Dual-EMA self-anchoring
    if p["long_ema_alpha"] > 0 and p["ema_blend"] > 0 and micro is not None:
        long_key = f"{product}_long_ema"
        prev_long = ts.get(long_key, micro)
        long_ema = p["long_ema_alpha"] * micro + (1 - p["long_ema_alpha"]) * prev_long
        ts[long_key] = long_ema
        fair = p["ema_blend"] * long_ema + (1 - p["ema_blend"]) * fair

    # Regime-aware half_spread
    half_spread = p["half_spread"]
    if p["vol_widen_coef"] != 0:
        mb_for_vol = ts.get(f"{product}_mid_buf", [])
        if len(mb_for_vol) >= 10:
            rv = realized_vol(mb_for_vol[-30:])
            half_spread = max(1, int(round(half_spread + p["vol_widen_coef"] * rv)))

    orders: List[Order] = []
    base_size = p["base_size"]
    take_edge = p["take_edge"]

    # STOP-LOSS
    stop_pos_threshold = p.get("stop_pos_threshold", 0)
    if stop_pos_threshold and abs(pos) >= stop_pos_threshold:
        sl_mtm_key = f"{product}_sl_mtm"
        sl_peak_key = f"{product}_sl_peak"
        sl_prev_mid_key = f"{product}_sl_prev_mid"
        sl_ticks_key = f"{product}_sl_ticks"

        prev_sl_mid = ts.get(sl_prev_mid_key, mid)
        cur_mtm = ts.get(sl_mtm_key, 0.0) + pos * (mid - prev_sl_mid)
        peak_mtm = max(ts.get(sl_peak_key, cur_mtm), cur_mtm)
        sl_dd = peak_mtm - cur_mtm
        ticks_at_pos = ts.get(sl_ticks_key, 0) + 1

        ts[sl_mtm_key] = cur_mtm
        ts[sl_peak_key] = peak_mtm
        ts[sl_prev_mid_key] = mid
        ts[sl_ticks_key] = ticks_at_pos

        if (sl_dd >= p.get("stop_drawdown_threshold", 800.0)
                and ticks_at_pos >= p.get("stop_min_ticks_at_pos", 100)):
            stop_size = p.get("stop_unwind_size", 20)
            if pos > 0 and ba is not None:
                qty = min(stop_size, pos)
                px = ba - 1 if ba - 1 > bb else ba
                orders.append(Order(product, px, -qty))
                pos -= qty
            elif pos < 0 and bb is not None:
                qty = min(stop_size, -pos)
                px = bb + 1 if bb + 1 < ba else bb
                orders.append(Order(product, px, qty))
                pos += qty
            ts[sl_peak_key] = cur_mtm
    elif stop_pos_threshold:
        ts[f"{product}_sl_mtm"] = 0.0
        ts[f"{product}_sl_peak"] = 0.0
        ts[f"{product}_sl_prev_mid"] = mid
        ts[f"{product}_sl_ticks"] = 0

    # Aggressive takes
    for ap in sorted(od.sell_orders.keys()):
        if fair - ap < take_edge:
            break
        room = max(0, limit - pos)
        qty = min(abs(od.sell_orders[ap]), room, base_size)
        if qty > 0:
            orders.append(Order(product, ap, qty))
            pos += qty
    for bp in sorted(od.buy_orders.keys(), reverse=True):
        if bp - fair < take_edge:
            break
        room = max(0, limit + pos)
        qty = min(od.buy_orders[bp], room, base_size)
        if qty > 0:
            orders.append(Order(product, bp, -qty))
            pos -= qty

    # Inventory-aware reservation skew
    inv_frac = pos / max(1, soft_cap)
    asym = p["asymm_skew"]
    if pos > 0:
        skew = p["k_inv"] * asym * inv_frac * max(2, half_spread * 2)
    elif pos < 0:
        skew = p["k_inv"] * (1.0 / asym if asym > 0 else 1.0) * inv_frac * max(2, half_spread * 2)
    else:
        skew = 0.0
    res = fair - skew

    bid_price = int(math.floor(res - half_spread))
    ask_price = int(math.ceil(res + half_spread))

    if bb + 1 < ba and bid_price > bb:
        bid_price = bb + 1
    if ba - 1 > bb and ask_price < ba:
        ask_price = ba - 1
    bid_price = min(bid_price, ba - 1)
    ask_price = max(ask_price, bb + 1)

    room_buy = max(0, soft_cap - pos)
    room_sell = max(0, soft_cap + pos)
    b_size = min(base_size, room_buy)
    a_size = min(base_size, room_sell)
    if b_size > 0 and bid_price > 0:
        orders.append(Order(product, bid_price, b_size))
    if a_size > 0 and ask_price > 0:
        orders.append(Order(product, ask_price, -a_size))

    return orders


# ====================================================================
# Trader — three engines, single tick
# ====================================================================
class Trader:
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        ts = _load(state.traderData)
        timestamp = state.timestamp
        day_num = _resolve_day_num()

        books = state.order_depths
        positions = state.position

        orders: Dict[str, List[Order]] = {VEV[k]: [] for k in STRIKES}
        orders[HG]  = []
        orders[VFE] = []

        # Live position view for the options engine (and for any reader
        # downstream — flow-MM uses state.position directly).
        pos_now = {sym: positions.get(sym, 0) for sym in orders}

        # ---- 1. HG flow-MM ----
        if HG in books:
            orders[HG] = trade_flow_mm(state, ts, HG, HG_PARAMS)

        # ---- 2. VFE flow-MM ----
        if VFE in books:
            orders[VFE] = trade_flow_mm(state, ts, VFE, VF_PARAMS)
            # Refresh pos_now[VFE] view in case the options engine reads it
            # to compute hedge sizing (it currently doesn't account for VFE
            # orders queued by flow-MM, but keep it pointed at on-tick start
            # position to match options_strat.py behaviour).

        # ---- 3. Options engine ----
        self._run_options(state, ts, day_num, books, orders, pos_now)

        return orders, 0, json.dumps(ts)

    # ----------------------------------------------------------------
    # Options — body of options_strat.Trader.run, refactored to add into
    # the shared `orders` dict instead of returning early from run().
    # ----------------------------------------------------------------
    def _run_options(self, state, ts, day_num, books, orders, pos_now):
        timestamp = state.timestamp

        vfe_book = books.get(VFE)
        S = mid_price(vfe_book) if vfe_book else None

        opt_books = {}
        opt_mid = {}
        for k in STRIKES:
            ob = books.get(VEV[k])
            if ob is None:
                continue
            opt_books[k] = ob
            m = mid_price(ob)
            if m is not None:
                opt_mid[k] = m

        T_days = _tte_days(timestamp, day_num)

        # Taper-unwind window for options
        if timestamp >= P["unwind_start"]:
            for k in STRIKES:
                sym = VEV[k]
                if pos_now.get(sym, 0) == 0:
                    continue
                ob = opt_books.get(k)
                if ob is not None:
                    self._unwind(orders, pos_now, ob, sym, timestamp)
            return

        if S is None:
            return

        mode = P["smile_mode"]
        per_tick_smile = None
        sigma_ema_prev = None
        new_iv_obs = {}

        if mode == "per_tick":
            pts = []
            for k in SMILE_STRIKES:
                if k not in opt_mid:
                    continue
                price = opt_mid[k]
                if price - max(0.0, S - k) < 1.0:
                    continue
                iv_year = implied_vol_call(price, S, k, T_days / 365)
                if iv_year is None or not (0.05 < iv_year < 3.0):
                    continue
                iv_day = iv_year / math.sqrt(365)
                m_val = math.log(k / S) / math.sqrt(T_days)
                pts.append((m_val, iv_day))
            if len(pts) >= P["per_tick_min_strikes"]:
                per_tick_smile = fit_smile(pts)
            if per_tick_smile is None:
                return

        elif mode == "per_strike_iv":
            sigma_ema_prev = ts.get("sigma_ema") or {
                str(k): PRIOR_IV.get(k, 0.012) for k in SMILE_STRIKES
            }
            for k in SMILE_STRIKES:
                if k not in opt_mid:
                    continue
                price = opt_mid[k]
                if price - max(0.0, S - k) < 1.0:
                    continue
                iv_year = implied_vol_call(price, S, k, T_days / 365)
                if iv_year is None or not (0.05 < iv_year < 3.0):
                    continue
                iv_day = iv_year / math.sqrt(365)
                new_iv_obs[str(k)] = iv_day

        def _sigma(k):
            if mode == "per_strike_iv" and sigma_ema_prev is not None:
                s = sigma_ema_prev.get(str(k))
                if s is None:
                    s = PRIOR_IV.get(k, 0.012)
                return s
            if mode == "static":
                m_val = math.log(k / S) / math.sqrt(T_days)
                return max(1e-4, SMILE_A * m_val * m_val + SMILE_B * m_val + SMILE_C)
            a_, b_, c_ = per_tick_smile
            m_val = math.log(k / S) / math.sqrt(T_days)
            return max(1e-4, a_ * m_val * m_val + b_ * m_val + c_)

        def _theo(k):
            return bs_call(S, k, T_days, _sigma(k))

        net_delta = 0.0
        for k in STRIKES:
            p_k = pos_now.get(VEV[k], 0)
            if p_k == 0:
                continue
            sigma_k = _sigma(k) if k in SMILE_STRIKES else max(
                1e-4,
                SMILE_A * (math.log(k / S) / math.sqrt(T_days)) ** 2
                + SMILE_B * (math.log(k / S) / math.sqrt(T_days))
                + SMILE_C,
            )
            d = bs_call_delta(S, k, T_days, sigma_k)
            net_delta += p_k * d

        delta_cap = P["delta_cap"]

        # Residual momentum EMA (updated every tick, used for sell filter)
        residual_ema = ts.get("residual_ema") or {}
        alpha_res = P["residual_ema_alpha"]
        residual_mom: dict = {}
        for k in ACTIVE_STRIKES:
            if k not in opt_mid:
                continue
            theo_k_now = _theo(k)
            residual_now = opt_mid[k] - theo_k_now
            prev_ema = residual_ema.get(str(k))
            if prev_ema is None:
                residual_ema[str(k)] = residual_now
                residual_mom[k] = 0.0
            else:
                residual_mom[k] = residual_now - prev_ema
                residual_ema[str(k)] = prev_ema + alpha_res * (residual_now - prev_ema)
        ts["residual_ema"] = residual_ema

        net_delta_holder = [net_delta]
        for k in ACTIVE_STRIKES:
            ob = opt_books.get(k)
            if ob is None:
                continue
            mid_k = opt_mid.get(k)
            if mid_k is None or mid_k <= 0:
                continue
            theo_k = _theo(k)
            sym = VEV[k]
            sigma_k = _sigma(k)
            delta_k = bs_call_delta(S, k, T_days, sigma_k)
            self._signal_trade(
                orders, pos_now, ob, sym, mid_k, theo_k,
                delta_k=delta_k,
                net_delta_holder=net_delta_holder,
                delta_cap=delta_cap,
                residual_mom_k=residual_mom.get(k, 0.0),
                strike=k,
            )

        if P["delta_hedge_with_vfe"] and vfe_book is not None:
            self._hedge_vfe(orders, pos_now, vfe_book, net_delta_holder[0])

        if mode == "per_strike_iv" and sigma_ema_prev is not None:
            a = P["iv_ema_alpha"]
            sigma_ema_new = dict(sigma_ema_prev)
            for key, iv_day in new_iv_obs.items():
                sigma_ema_new[key] = (1 - a) * sigma_ema_prev.get(key, iv_day) + a * iv_day
            ts["sigma_ema"] = sigma_ema_new

    # ----------------------------------------------------------------
    # Options helpers — verbatim from options_strat.py
    # ----------------------------------------------------------------
    def _signal_trade(
        self, orders, pos_now, ob, sym, mid_k, theo_k,
        delta_k, net_delta_holder, delta_cap,
        residual_mom_k: float = 0.0,
        strike: int = None,
    ):
        bb, ba = best_bid_ask(ob)
        if ba is None and bb is None:
            return

        spread   = (ba - bb) if (ba is not None and bb is not None) else 0
        required = max(P["abs_edge"], P["spread_mult"] * spread)

        cap  = POSITION_CAP_BY_STRIKE.get(strike, P["position_cap"])
        size = P["trade_size"]
        pos  = pos_now[sym]

        # Buy side: no momentum filter
        if ba is not None and pos < cap:
            buy_edge = theo_k - ba
            if buy_edge > required:
                new_net = net_delta_holder[0] + delta_k * size
                if delta_cap is None or new_net <= delta_cap:
                    qty = min(size, abs(ob.sell_orders.get(ba, 0)), cap - pos)
                    if qty > 0:
                        orders[sym].append(Order(sym, ba, qty))
                        pos_now[sym] += qty
                        net_delta_holder[0] += delta_k * qty

        # Sell side: residual momentum filter
        if bb is not None and pos > -cap:
            sell_edge = bb - theo_k
            if sell_edge > required:
                if residual_mom_k > P["residual_max_rise"]:
                    if BACKTEST_DAY_NUM is not None:
                        SKIP_COUNT_BY_DAY[BACKTEST_DAY_NUM] += 1
                    return
                new_net = net_delta_holder[0] - delta_k * size
                if delta_cap is None or new_net >= -delta_cap:
                    qty = min(size, ob.buy_orders.get(bb, 0), cap + pos)
                    if qty > 0:
                        orders[sym].append(Order(sym, bb, -qty))
                        pos_now[sym] -= qty
                        net_delta_holder[0] -= delta_k * qty

    def _hedge_vfe(self, orders, pos_now, vfe_book, net_delta):
        # Partial hedge: only act when |net_delta| > hedge_band; otherwise
        # leave VFE position alone (no churn inside the band).
        band  = P.get("hedge_band", 0.0)
        ratio = P.get("hedge_ratio", 1.0)
        if abs(net_delta) <= band:
            return

        target = -round(ratio * net_delta)
        cur    = pos_now.get(VFE, 0)
        diff   = target - cur
        if diff == 0:
            return
        bb, ba = best_bid_ask(vfe_book)
        lim    = LIMITS.get(VFE, 80)
        if diff > 0 and ba is not None:
            qty = min(diff, abs(vfe_book.sell_orders.get(ba, 0)), lim - cur)
            if qty > 0:
                orders[VFE].append(Order(VFE, ba, qty))
                pos_now[VFE] = cur + qty
        elif diff < 0 and bb is not None:
            qty = min(-diff, vfe_book.buy_orders.get(bb, 0), lim + cur)
            if qty > 0:
                orders[VFE].append(Order(VFE, bb, -qty))
                pos_now[VFE] = cur - qty

    def _unwind(self, orders, pos_now, ob, sym, timestamp):
        p = pos_now[sym]
        if p == 0:
            return
        bb, ba = best_bid_ask(ob)
        steps_left = max(1, (HARD_UNWIND - timestamp) // 100)
        if timestamp >= HARD_UNWIND:
            steps_left = 1
        if p > 0 and bb is not None:
            if timestamp >= HARD_UNWIND:
                remaining = p
                for bp in sorted(ob.buy_orders.keys(), reverse=True):
                    if remaining <= 0:
                        break
                    take = min(ob.buy_orders[bp], remaining)
                    if take > 0:
                        orders[sym].append(Order(sym, bp, -take))
                        pos_now[sym] -= take
                        remaining -= take
            else:
                qty = min(max(1, (p + steps_left - 1) // steps_left), p)
                orders[sym].append(Order(sym, bb, -qty))
                pos_now[sym] -= qty
        elif p < 0 and ba is not None:
            if timestamp >= HARD_UNWIND:
                remaining = -p
                for ap in sorted(ob.sell_orders.keys()):
                    if remaining <= 0:
                        break
                    take = min(abs(ob.sell_orders[ap]), remaining)
                    if take > 0:
                        orders[sym].append(Order(sym, ap, take))
                        pos_now[sym] += take
                        remaining -= take
            else:
                qty = min(max(1, (-p + steps_left - 1) // steps_left), -p)
                orders[sym].append(Order(sym, ba, qty))
                pos_now[sym] += qty
