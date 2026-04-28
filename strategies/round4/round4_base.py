"""Round 2/3 final — combined VEV-option + HG/VFE-underlying strategy.

Merges:
  - strategies/option.py    : Z-score residual engine on VEV_5300/5400/5500.
                              Uses fixed parabolic smile shape (SMILE_A/B)
                              with a live ATM-IV-tracked vertical shift
                              (SMILE_C → smile_c).
  - strategies/underline.py : Flow-MM template ("trade_flow_mm") for
                              HYDROGEL_PACK and VELVETFRUIT_EXTRACT, with
                              microprice / imbalance / trend / revert /
                              EMA-of-velocity / stop-loss signals.

Both engines share a single `traderData` JSON blob. Different per-product
unwind windows are applied:
  - HYDROGEL_PACK / VELVETFRUIT_EXTRACT → unwind from ts ≥ 998_000
  - VEV_<K>                              → unwind from ts ≥ 990_000

Time scale: SESSION_END = 1_000_000 (local backtester). Both source files
used this scale. The official IMC platform uses 100_000 — do not submit
this combined file to live without rescaling thresholds.
"""

import json
import math
from typing import Dict, List, Optional, Tuple

from datamodel import Order, OrderDepth, TradingState


# ====================================================================
# OPTIONS — universe & smile (from option.py)
# ====================================================================
VFE = "VELVETFRUIT_EXTRACT"
STRIKES = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]
VEV = {k: f"VEV_{k}" for k in STRIKES}
LIMITS = {VEV[k]: 300 for k in STRIKES}

ACTIVE_OPTION_STRIKES = [5300, 5400, 5500]

SESSION_END = 1_000_000
HARD_UNWIND = 998_000          # used by option-engine's per-step liquidator

HIST_DTE_AT_DAY_START = {0: 8, 1: 7, 2: 6}
LIVE_DTE_DEFAULT = 5

# Fixed smile shape; the constant term is replaced live by ATM-IV EMA.
SMILE_A = 0.5417
SMILE_B = 0.0023
SMILE_C = 0.0121
ATM_IV_ALPHA = 0.1


# ====================================================================
# Black-Scholes (from option.py)
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
    """N(d1). For a long call, positive delta in [0, 1]."""
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else (0.0 if S < K else 0.5)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * sqrtT)
    return _norm_cdf(d1)


def implied_vol_call(price: float, S: float, K: float, T: float) -> Optional[float]:
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


def _smile_sigma_day(K: float, S: float, T_days: float, smile_c: float = SMILE_C) -> float:
    m = math.log(K / S) / math.sqrt(T_days)
    return SMILE_A * m * m + SMILE_B * m + smile_c


def fixed_theo(S: float, K: float, T_days: float, smile_c: float = SMILE_C) -> float:
    sigma_day = max(0.0001, _smile_sigma_day(K, S, T_days, smile_c))
    return bs_call(S, K, T_days, sigma_day)


def _tte_days(timestamp, day_num):
    if day_num is None:
        days_left = LIVE_DTE_DEFAULT - timestamp / SESSION_END
    else:
        start_dte = HIST_DTE_AT_DAY_START.get(day_num, LIVE_DTE_DEFAULT)
        days_left = start_dte - timestamp / SESSION_END
    return max(days_left, 1e-4)


# ====================================================================
# Order-book helpers — option.py style
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
# Order-book helpers — underline.py style (different naming kept to
# avoid touching the flow-MM engine internals)
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
# UNDERLYING — flow-MM params (from underline.py PARAMS_BLOCK)
# ====================================================================
VARIANT_ID = 'p7a_hg_insurance_conservative'
VARIANT_LABEL = 'HG insurance: pos=100, dd=4000, ticks=200 (20k ts), size=30'

HG_PARAMS = {
    # Structural
    'limit': 200,
    'soft_cap': 150,
    'half_spread': 6,
    'base_size': 20,
    'take_edge': 8,
    'k_inv': 1.0,
    # Alpha
    'micro_coef': 0.5,
    'imb_coef': 0.4,
    'trend_coef': 0.45,
    'trend_lag': 5,
    'revert_coef': 0.2,
    'revert_window': 120,
    'vel_coef': 0.2,
    'vel_alpha': 0.1,
    'ema_alpha': 0.05,
    # Stop-loss
    'stop_pos_threshold': 100,
    'stop_drawdown_threshold': 4000.0,
    'stop_min_ticks_at_pos': 200,
    'stop_unwind_size': 30,
}

VF_PARAMS = {
    # Inventory bounds
    'limit': 200,
    'soft_cap': 150,
    'base_size': 15,
    # Quote width / take threshold
    'half_spread': 1,
    'take_edge': 2,
    # Inventory shade (protective)
    'k_inv': 0.5,
    # Alpha — 5 groups, 7 levers
    'trend_coef': 0.3,      # momentum
    'trend_lag': 5,
    'vel_coef': 0.3,        # ema-velocity
    'vel_alpha': 0.1,
    'micro_coef': 0.2,      # microprice tilt
    'imb_coef': 0.3,        # L1 imbalance
    'ema_alpha': 0.0,       # mid smoothing
    # Catastrophic insurance
    'stop_pos_threshold': 150,
    'stop_drawdown_threshold': 1500.0,
    'stop_min_ticks_at_pos': 100,
    'stop_unwind_size': 25,
}

# Underlying engine's unwind window (HG / VFE)
UNDERLYING_UNWIND_START = 998_000


# ====================================================================
# OPTIONS — Z-score engine params (from option.py)
# ====================================================================
P = {
    "resid_z_alpha": 2 / 501,
    "resid_z_threshold": 1.0,
    "resid_z_position_cap": 300,
    "resid_z_size": 20,

    "long_only_strikes": [],
    "short_only_strikes": [],

    "unwind_options": 990_000,

    # ----- Delta hedging (VFE used as the hedge instrument) -----
    # Wired but DISABLED by default. With z=1.0 cap=300, the options
    # engine accumulates large bleeding positions and the hedge merely
    # adds spread cost. Sweep results (see chat history):
    #   off                 : -61,659
    #   on (band=5,h=1.0)   : -306,000  (worst)
    #   on (band=100,h=1.0) : -200,783
    #   on (band=25,h=0.25) : -195,183  (best, still worse than off)
    # Re-enable once the options engine itself is profitable.
    "delta_hedge_enabled": False,
    "delta_hedge_band": 25,
    "delta_hedge_chunk": 10,
    "delta_haircut": 0.5,
}


def _load(td):
    if not td:
        return {}
    try:
        return json.loads(td)
    except Exception:
        return {}


# ====================================================================
# Underlying flow-MM engine (verbatim from underline.py)
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
# Trader — combined dispatch
# ====================================================================
class Trader:
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        ts = _load(state.traderData)
        timestamp = state.timestamp
        day_num = None

        books = state.order_depths
        positions = state.position

        # Initialize order containers for ALL tradable symbols. Ensures the
        # closeout path returns valid empty lists for symbols we never touched.
        orders: Dict[str, List[Order]] = {VEV[k]: [] for k in STRIKES}
        orders["HYDROGEL_PACK"] = []
        orders["VELVETFRUIT_EXTRACT"] = []

        # ---- 1. Underlying engine (HG + VFE) ----
        if "HYDROGEL_PACK" in books:
            orders["HYDROGEL_PACK"] = trade_flow_mm(state, ts, "HYDROGEL_PACK", HG_PARAMS)
        if "VELVETFRUIT_EXTRACT" in books:
            orders["VELVETFRUIT_EXTRACT"] = trade_flow_mm(state, ts, "VELVETFRUIT_EXTRACT", VF_PARAMS)

        # ---- 2. Options engine (z-score on active strikes) ----
        pos_now = {sym: positions.get(sym, 0) for sym in (VEV[k] for k in STRIKES)}

        vfe_book = books.get(VFE)
        S = mid_price(vfe_book) if vfe_book else None
        T_days = _tte_days(timestamp, day_num)

        # Dynamic ATM-IV anchoring (smile_c override)
        smile_c = ts.get("atm_iv", SMILE_C)
        if S is not None:
            atm_k = min(STRIKES, key=lambda k: abs(k - S))
            atm_ob = books.get(VEV[atm_k])
            atm_mid = mid_price(atm_ob) if atm_ob else None
            if atm_mid is not None:
                live_iv = implied_vol_call(atm_mid, S, atm_k, T_days)
                if live_iv is not None:
                    smile_c = _ema(ts.get("atm_iv"), live_iv, ATM_IV_ALPHA)
        ts["atm_iv"] = smile_c

        # Options closeout (different threshold from underlying)
        in_options_closeout = timestamp >= P["unwind_options"]
        if in_options_closeout:
            for k in STRIKES:
                ob = books.get(VEV[k])
                if ob is not None:
                    self._unwind(orders, pos_now, ob, VEV[k], timestamp)
        else:
            for k in ACTIVE_OPTION_STRIKES:
                ob = books.get(VEV[k])
                if ob is None:
                    continue
                m = mid_price(ob)
                if m is None or S is None:
                    continue
                theo_k = fixed_theo(S, k, T_days, smile_c)
                sym = VEV[k]
                allow_long = sym not in P["short_only_strikes"]
                allow_short = sym not in P["long_only_strikes"]
                self._resid_z_trade(
                    orders, pos_now, ts, ob, sym, k, m, theo_k, timestamp,
                    allow_long=allow_long, allow_short=allow_short,
                )

        # ---- 3. Delta hedge — push VFE toward -net_option_delta ----
        # Skip during options closeout (positions are being flattened anyway)
        # and during underlying closeout (VFE is being closed out by
        # trade_flow_mm).
        if (P["delta_hedge_enabled"]
                and not in_options_closeout
                and timestamp < UNDERLYING_UNWIND_START
                and S is not None
                and "VELVETFRUIT_EXTRACT" in books):
            self._delta_hedge(orders, positions, pos_now, books, S, T_days, smile_c, ts)

        return orders, 0, json.dumps(ts)

    # ------------------------------------------------------------------
    def _delta_hedge(self, orders, positions, pos_now, books, S, T_days, smile_c, ts):
        """Add a VFE order to drive VFE position toward -net_option_delta."""
        # Compute net BS delta over CURRENT option positions (start of tick).
        net_delta = 0.0
        for k in STRIKES:
            sym = VEV[k]
            p_k = positions.get(sym, 0)
            if p_k == 0:
                continue
            sigma = max(0.0001, _smile_sigma_day(k, S, T_days, smile_c))
            d = bs_call_delta(S, k, T_days, sigma)
            net_delta += p_k * d * P["delta_haircut"]

        ts["net_option_delta"] = net_delta

        target_vfe = -int(round(net_delta))
        cur_vfe = positions.get("VELVETFRUIT_EXTRACT", 0)
        gap = target_vfe - cur_vfe
        if abs(gap) <= P["delta_hedge_band"]:
            return

        vfe_book = books.get("VELVETFRUIT_EXTRACT")
        if vfe_book is None:
            return
        bb, ba = best_bid_ask(vfe_book)

        # Account for VFE orders already queued by trade_flow_mm so we do
        # not trip the +/-200 position limit (which would cancel ALL VFE
        # orders for the tick).
        vfe_lim = 200
        existing = orders.get("VELVETFRUIT_EXTRACT", [])
        existing_long = sum(o.quantity for o in existing if o.quantity > 0)
        existing_short = sum(-o.quantity for o in existing if o.quantity < 0)
        room_buy = max(0, vfe_lim - cur_vfe - existing_long)
        room_sell = max(0, vfe_lim + cur_vfe - existing_short)

        chunk = P["delta_hedge_chunk"]
        if gap > 0 and ba is not None:
            qty = min(gap, chunk, room_buy, abs(vfe_book.sell_orders.get(ba, 0)))
            if qty > 0:
                orders["VELVETFRUIT_EXTRACT"].append(
                    Order("VELVETFRUIT_EXTRACT", ba, qty)
                )
        elif gap < 0 and bb is not None:
            qty = min(-gap, chunk, room_sell, vfe_book.buy_orders.get(bb, 0))
            if qty > 0:
                orders["VELVETFRUIT_EXTRACT"].append(
                    Order("VELVETFRUIT_EXTRACT", bb, -qty)
                )

    # ------------------------------------------------------------------
    def _resid_z_trade(
        self, orders, pos_now, ts, ob, sym, k, mid_k, theo_k, timestamp,
        allow_long=True, allow_short=True,
    ):
        resid = theo_k - mid_k
        alpha = P["resid_z_alpha"]
        mean_key = f"resid_mean_{k}"
        var_key = f"resid_var_{k}"

        prev_mean = ts.get(mean_key)
        prev_var = ts.get(var_key, 0.0)

        if prev_mean is None:
            new_mean = resid
            new_var = 4.0
        else:
            dev = resid - prev_mean
            new_mean = _ema(prev_mean, resid, alpha)
            new_var = (1 - alpha) * prev_var + alpha * (dev * dev)

        ts[mean_key] = new_mean
        ts[var_key] = new_var

        if new_var <= 0:
            return

        z = (resid - new_mean) / math.sqrt(max(new_var, 1e-9))
        z = -z

        thr = P["resid_z_threshold"]
        cap = P["resid_z_position_cap"]
        size = P["resid_z_size"]
        bb, ba = best_bid_ask(ob)
        pos = pos_now[sym]

        if z > thr and ba is not None and pos < cap and allow_long:
            qty = min(size, abs(ob.sell_orders.get(ba, 0)), cap - pos)
            if qty > 0:
                orders[sym].append(Order(sym, ba, qty))
                pos_now[sym] += qty
        elif z < -thr and bb is not None and pos > -cap and allow_short:
            qty = min(size, ob.buy_orders.get(bb, 0), cap + pos)
            if qty > 0:
                orders[sym].append(Order(sym, bb, -qty))
                pos_now[sym] -= qty

    # ------------------------------------------------------------------
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