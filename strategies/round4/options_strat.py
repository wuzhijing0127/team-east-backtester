import json
import math
from collections import defaultdict

from datamodel import Order

# Tracks sell signals blocked by residual momentum filter; reset by sweep before each run.
SKIP_COUNT_BY_DAY: dict = defaultdict(int)

VFE = "VELVETFRUIT_EXTRACT"
STRIKES = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]
VEV = {k: f"VEV_{k}" for k in STRIKES}
LIMITS = {VFE: 200, **{VEV[k]: 300 for k in STRIKES}}

SMILE_STRIKES = [5000, 5100, 5200, 5300, 5400, 5500]
ACTIVE_STRIKES = [5200, 5300]

# Per-strike position cap override; missing strikes fall back to P["position_cap"].
POSITION_CAP_BY_STRIKE: dict = {5200: 50, 5300: 100}

SESSION_END = 100_000
HARD_UNWIND = 99_800
UNWIND_START = 99_000

HIST_DTE_AT_DAY_START = {0: 8, 1: 7, 2: 6, 3: 5}
LIVE_DTE_DEFAULT = 5

BACKTEST_DAY_NUM = None

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

def best_bid_ask(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask

def mid_price(od):
    b, a = best_bid_ask(od)
    return None if b is None or a is None else 0.5 * (b + a)

def _ema(prev, x, alpha):
    if prev is None:
        return x
    return prev + alpha * (x - prev)

P = {
    "smile_mode": "static",
    "iv_ema_alpha": 2 / 51,
    "per_tick_min_strikes": 3,

    "abs_edge": 3,        # minimum absolute edge required (theo - ask for buys, bid - theo for sells)
    "spread_mult": 0.5,   # required edge also >= spread_mult * full_spread
    "position_cap": 50,   # fallback when strike not in POSITION_CAP_BY_STRIKE
    "trade_size": 10,

    # residual momentum filter (sell side only)
    # residual = opt_mid - theo; residual_mom = residual - residual_ema
    # skip sell if residual_mom > residual_max_rise
    "residual_ema_alpha": 2 / 21,
    "residual_max_rise": 0.5,

    "delta_cap": 40.0,
    "delta_hedge_with_vfe": False,

    # partial hedge (used only when delta_hedge_with_vfe=True): re-hedge only
    # when |net_delta| > hedge_band, then trade toward -round(hedge_ratio * net_delta).
    "hedge_band":  0.0,
    "hedge_ratio": 1.0,

    "unwind_start": UNWIND_START,
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

def _tte_days(timestamp, day_num):
    if day_num is None:
        days_left = LIVE_DTE_DEFAULT - timestamp / SESSION_END
    else:
        start_dte = HIST_DTE_AT_DAY_START.get(day_num, LIVE_DTE_DEFAULT)
        days_left = start_dte - timestamp / SESSION_END
    return max(days_left, 1e-4)

class Trader:
    def run(self, state):
        ts = _load(state.traderData)
        timestamp = state.timestamp
        day_num = _resolve_day_num()

        books = state.order_depths
        positions = state.position
        orders = {VEV[k]: [] for k in STRIKES}
        orders[VFE] = []
        pos_now = {sym: positions.get(sym, 0) for sym in orders}

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

        if timestamp >= P["unwind_start"]:
            for k in STRIKES:
                sym = VEV[k]
                if pos_now.get(sym, 0) == 0:
                    continue
                ob = opt_books.get(k)
                if ob is not None:
                    self._unwind(orders, pos_now, ob, sym, timestamp)
            return orders, 0, json.dumps(ts)

        if S is None:
            return orders, 0, json.dumps(ts)

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
                return orders, 0, json.dumps(ts)

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
            p = pos_now.get(VEV[k], 0)
            if p == 0:
                continue
            sigma_k = _sigma(k) if k in SMILE_STRIKES else max(
                1e-4,
                SMILE_A * (math.log(k / S) / math.sqrt(T_days)) ** 2
                + SMILE_B * (math.log(k / S) / math.sqrt(T_days))
                + SMILE_C,
            )
            d = bs_call_delta(S, k, T_days, sigma_k)
            net_delta += p * d

        delta_cap = P["delta_cap"]

        # ── residual momentum EMA (updated every tick, used for sell filter) ──
        # residual[k] = opt_mid[k] - theo[k]  (market price above/below model)
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

        return orders, 0, json.dumps(ts)

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

        # buy side: no momentum filter
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

        # sell side: apply residual momentum filter
        if bb is not None and pos > -cap:
            sell_edge = bb - theo_k
            if sell_edge > required:
                if residual_mom_k > P["residual_max_rise"]:
                    # market is increasingly overpriced vs model and widening — skip sell
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
        # Partial hedge: only act when |net_delta| > hedge_band; otherwise leave
        # the existing VFE position alone (no churn inside the band).
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