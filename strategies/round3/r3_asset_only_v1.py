import json
import math
from typing import Dict, List, Optional, Tuple

from datamodel import Order, OrderDepth, TradingState


# ----------------------------------------------------------------------
# Universe / limits
# ----------------------------------------------------------------------
HYDRO = "HYDROGEL_PACK"
VFE = "VELVETFRUIT_EXTRACT"
STRIKES = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]
VEV = {k: f"VEV_{k}" for k in STRIKES}
LIMITS = {HYDRO: 200, VFE: 200, **{VEV[k]: 300 for k in STRIKES}}

# Strikes used for the smile fit (extrinsic value > 1 typically holds)
SMILE_STRIKES = [5000, 5100, 5200, 5300, 5400, 5500]
# Strikes we actively quote / position
ACTIVE_OPTION_STRIKES = [5300, 5400, 5500]

SESSION_END = 1_000_000
UNWIND_START = 990_000
HARD_UNWIND = 998_000

HIST_DTE_AT_DAY_START = {0: 8, 1: 7, 2: 6}
LIVE_DTE_DEFAULT = 5


# ----------------------------------------------------------------------
# Black-Scholes (call only — puts are not in this game)
# ----------------------------------------------------------------------
SQRT_2PI = math.sqrt(2 * math.pi)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(S: float, K: float, T: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    return S * _norm_cdf(d1) - K * _norm_cdf(d2)


def bs_call_delta(S: float, K: float, T: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else (0.0 if S < K else 0.5)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * sqrtT)
    return _norm_cdf(d1)


def bs_call_vega(S: float, K: float, T: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return 0.0
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * sqrtT)
    return S * _norm_pdf(d1) * sqrtT


def implied_vol_call(price: float, S: float, K: float, T: float) -> Optional[float]:
    if T <= 0 or price <= 0:
        return None
    intrinsic = max(0.0, S - K)
    if price - intrinsic <= 0 or price >= S:
        return None
    sigma = 0.5
    for _ in range(40):
        c = bs_call(S, K, T, sigma)
        diff = c - price
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
        mid = 0.5 * (lo + hi)
        if bs_call(S, K, T, mid) > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi) if hi - lo < 1e-3 else None


# ----------------------------------------------------------------------
# Smile fit — parabola in log-moneyness
# ----------------------------------------------------------------------
def fit_smile(points: List[Tuple[float, float]]) -> Optional[Tuple[float, float, float]]:
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
        sx += x
        sx2 += x2
        sx3 += x2 * x
        sx4 += x2 * x2
        sy += y
        sxy += x * y
        sx2y += x2 * y
    m = [[n, sx, sx2], [sx, sx2, sx3], [sx2, sx3, sx4]]
    r = [sy, sxy, sx2y]
    return _solve_3x3(m, r)


def _solve_3x3(m, r) -> Optional[Tuple[float, float, float]]:
    det = (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )
    if abs(det) < 1e-14:
        return None

    def _det(a, b, c, d, e, f, g, h, i):
        return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)

    return (
        _det(r[0], m[0][1], m[0][2], r[1], m[1][1], m[1][2], r[2], m[2][1], m[2][2]) / det,
        _det(m[0][0], r[0], m[0][2], m[1][0], r[1], m[1][2], m[2][0], r[2], m[2][2]) / det,
        _det(m[0][0], m[0][1], r[0], m[1][0], m[1][1], r[1], m[2][0], m[2][1], r[2]) / det,
    )


# ----------------------------------------------------------------------
# Order book helpers
# ----------------------------------------------------------------------
def best_bid_ask(od: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def mid_price(od: OrderDepth) -> Optional[float]:
    b, a = best_bid_ask(od)
    return None if b is None or a is None else 0.5 * (b + a)


def microprice(od: OrderDepth) -> Optional[float]:
    b, a = best_bid_ask(od)
    if b is None or a is None:
        return None
    bv = od.buy_orders.get(b, 0)
    av = abs(od.sell_orders.get(a, 0))
    if bv + av == 0:
        return 0.5 * (b + a)
    return (b * av + a * bv) / (bv + av)


def l1_imbalance(od: OrderDepth) -> float:
    b, a = best_bid_ask(od)
    if b is None or a is None:
        return 0.0
    bv = od.buy_orders.get(b, 0)
    av = abs(od.sell_orders.get(a, 0))
    s = bv + av
    return 0.0 if s == 0 else (bv - av) / s


def spread_int(od: OrderDepth) -> Optional[int]:
    b, a = best_bid_ask(od)
    return None if b is None or a is None else a - b


# ----------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------
P = {
    # Fair-value model
    "vfe_imb_coef": 0.20,
    "vfe_micro_coef": 0.30,
    "hydro_imb_coef": 0.40,
    "hydro_micro_coef": 0.50,

    # HYDROGEL MM
    "hydro_half_spread": 3,
    "hydro_base_size": 20,
    "hydro_k_inv": 0.10,        # ticks of skew per unit pos/limit
    "hydro_soft_limit": 150,
    "hydro_take_edge": 8,        # take only if >= 8 ticks below/above fair

    # VFE MM
    "vfe_half_spread": 1,
    "vfe_base_size": 10,
    "vfe_k_inv": 0.05,
    "vfe_soft_cap": 80,
    "vfe_take_edge": 4,

    # Option MM (per-strike)
    "opt_half_spread": 1,
    "opt_base_size": 25,
    "opt_k_inv": 0.06,
    "opt_soft_cap": 200,
    "opt_take_edge": 2,         # take when ask <= fair_opt - 2

    # Option residual skew (max ticks shift fair toward theo direction)
    # Set to 0 — backtester closes at mid, so leaning to theo just adds spread cost.
    "opt_resid_skew_max": 0.0,

    # Delta hedging
    "delta_band": 50,
    "delta_chunk": 25,
}


def _load(td: str) -> dict:
    if not td:
        return {}
    try:
        return json.loads(td)
    except Exception:
        return {}


def _tte_years(timestamp: int, day_num: Optional[int]) -> float:
    if day_num is None:
        days_left = LIVE_DTE_DEFAULT - timestamp / SESSION_END
    else:
        start_dte = HIST_DTE_AT_DAY_START.get(day_num, LIVE_DTE_DEFAULT)
        days_left = start_dte - timestamp / SESSION_END
    return max(days_left, 1e-4) / 365.0


# ----------------------------------------------------------------------
# Trader
# ----------------------------------------------------------------------
class Trader:
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        ts = _load(state.traderData)
        timestamp = state.timestamp

        day_num = None

        books = state.order_depths
        positions = state.position
        orders: Dict[str, List[Order]] = {sym: [] for sym in [HYDRO, VFE] + [VEV[k] for k in STRIKES]}
        pos_now = {sym: positions.get(sym, 0) for sym in orders}

        in_closeout = timestamp >= UNWIND_START
        if in_closeout:
            self._closeout_all(orders, pos_now, books, timestamp)
            return orders, 0, json.dumps(ts)

        # ------------------------------------------------------------
        # Fair values
        # ------------------------------------------------------------
        vfe_book = books.get(VFE)
        vfe_mid = mid_price(vfe_book) if vfe_book else None
        vfe_micro = microprice(vfe_book) if vfe_book else None
        vfe_imb = l1_imbalance(vfe_book) if vfe_book else 0.0
        vfe_spr = spread_int(vfe_book) if vfe_book else None
        fair_vfe = None
        if vfe_mid is not None:
            tilt = P["vfe_micro_coef"] * (vfe_micro - vfe_mid) if vfe_micro is not None else 0.0
            fair_vfe = vfe_mid + tilt + (P["vfe_imb_coef"] * vfe_imb * (vfe_spr or 0))

        hydro_book = books.get(HYDRO)
        hydro_mid = mid_price(hydro_book) if hydro_book else None
        hydro_micro = microprice(hydro_book) if hydro_book else None
        hydro_imb = l1_imbalance(hydro_book) if hydro_book else 0.0
        hydro_spr = spread_int(hydro_book) if hydro_book else None
        fair_hydro = None
        if hydro_mid is not None:
            tilt = P["hydro_micro_coef"] * (hydro_micro - hydro_mid) if hydro_micro is not None else 0.0
            fair_hydro = hydro_mid + tilt + (P["hydro_imb_coef"] * hydro_imb * (hydro_spr or 0))

        # ------------------------------------------------------------
        # Smile (used for delta computation only — not for taking)
        # ------------------------------------------------------------
        T = _tte_years(timestamp, day_num)
        opt_books, opt_bid, opt_ask, opt_mid = {}, {}, {}, {}
        for k in STRIKES:
            sym = VEV[k]
            ob = books.get(sym)
            if ob is None:
                continue
            opt_books[k] = ob
            b, a = best_bid_ask(ob)
            if b is not None:
                opt_bid[k] = b
            if a is not None:
                opt_ask[k] = a
            m = mid_price(ob)
            if m is not None:
                opt_mid[k] = m

        ivs: Dict[int, float] = {}
        if fair_vfe is not None:
            for k in SMILE_STRIKES:
                if k not in opt_mid:
                    continue
                price = opt_mid[k]
                if price - max(0.0, fair_vfe - k) < 1.0:
                    continue
                iv = implied_vol_call(price, fair_vfe, k, T)
                if iv is not None and 0.05 < iv < 3.0:
                    ivs[k] = iv

        global_smile = None
        if len(ivs) >= 3 and fair_vfe is not None:
            pts = [(math.log(k / fair_vfe), iv) for k, iv in ivs.items()]
            global_smile = fit_smile(pts)

        median_iv = None
        if ivs:
            vs = sorted(ivs.values())
            median_iv = vs[len(vs) // 2]

        def _sigma_for(k: int) -> Optional[float]:
            if global_smile is not None and fair_vfe is not None:
                a, b, c = global_smile
                x = math.log(k / fair_vfe)
                return max(0.05, a + b * x + c * x * x)
            return median_iv

        # ------------------------------------------------------------
        # 1. HYDROGEL MM
        # ------------------------------------------------------------
        if fair_hydro is not None and hydro_book is not None:
            self._mm_product(
                orders, pos_now, hydro_book, fair_hydro, HYDRO,
                half_spread=P["hydro_half_spread"],
                base_size=P["hydro_base_size"],
                k_inv=P["hydro_k_inv"],
                soft_cap=P["hydro_soft_limit"],
                take_edge=P["hydro_take_edge"],
            )

        # ------------------------------------------------------------
        # 2. VFE MM (small)
        # ------------------------------------------------------------
        if fair_vfe is not None and vfe_book is not None:
            self._mm_product(
                orders, pos_now, vfe_book, fair_vfe, VFE,
                half_spread=P["vfe_half_spread"],
                base_size=P["vfe_base_size"],
                k_inv=P["vfe_k_inv"],
                soft_cap=P["vfe_soft_cap"],
                take_edge=P["vfe_take_edge"],
            )

        # ------------------------------------------------------------
        # 3. Option MM on K=5300/5400/5500 around their mid +
        #    a small skew toward theo (smile-implied fair).
        # ------------------------------------------------------------
        for k in ACTIVE_OPTION_STRIKES:
            ob = opt_books.get(k)
            if ob is None:
                continue
            m = opt_mid.get(k)
            if m is None:
                continue
            sym = VEV[k]
            opt_fair = m
            # Smile-residual skew capped to opt_resid_skew_max
            sigma = _sigma_for(k)
            if sigma is not None and fair_vfe is not None:
                theo_k = bs_call(fair_vfe, k, T, sigma)
                resid = theo_k - m
                # Cap and apply
                cap = P["opt_resid_skew_max"]
                opt_fair = m + max(-cap, min(cap, resid))
            self._mm_product(
                orders, pos_now, ob, opt_fair, sym,
                half_spread=P["opt_half_spread"],
                base_size=P["opt_base_size"],
                k_inv=P["opt_k_inv"],
                soft_cap=P["opt_soft_cap"],
                take_edge=P["opt_take_edge"],
            )

        # ------------------------------------------------------------
        # 4. Delta hedge with VFE
        # ------------------------------------------------------------
        if fair_vfe is not None:
            net_delta = pos_now.get(VFE, 0)
            for k in STRIKES:
                p = pos_now.get(VEV[k], 0)
                if p == 0:
                    continue
                sigma = _sigma_for(k)
                if sigma is None:
                    continue
                d = bs_call_delta(fair_vfe, k, T, sigma)
                haircut = 0.80 if k <= 5400 else 0.60
                net_delta += int(round(p * haircut * d))
            if abs(net_delta) > P["delta_band"] and vfe_book is not None:
                bb, ba = best_bid_ask(vfe_book)
                vfe_pos = pos_now[VFE]
                vfe_lim = LIMITS[VFE]
                chunk = min(P["delta_chunk"], abs(net_delta) - P["delta_band"])
                if net_delta > 0 and bb is not None:
                    qty = min(chunk, vfe_pos + vfe_lim)
                    if qty > 0:
                        orders[VFE].append(Order(VFE, bb, -qty))
                        pos_now[VFE] -= qty
                elif net_delta < 0 and ba is not None:
                    qty = min(chunk, vfe_lim - vfe_pos)
                    if qty > 0:
                        orders[VFE].append(Order(VFE, ba, qty))
                        pos_now[VFE] += qty

        return orders, 0, json.dumps(ts)

    # ------------------------------------------------------------------
    def _mm_product(
        self,
        orders, pos_now, ob: OrderDepth, fair: float, sym: str,
        half_spread: int, base_size: int, k_inv: float,
        soft_cap: int, take_edge: int,
    ) -> None:
        limit = LIMITS[sym]
        bb, ba = best_bid_ask(ob)
        pos = pos_now[sym]

        # Aggressive take only at large dislocation
        if ba is not None:
            for ap in sorted(ob.sell_orders.keys()):
                if fair - ap < take_edge:
                    break
                room = max(0, limit - pos)
                qty = min(abs(ob.sell_orders[ap]), room, base_size)
                if qty > 0:
                    orders[sym].append(Order(sym, ap, qty))
                    pos += qty
        if bb is not None:
            for bp in sorted(ob.buy_orders.keys(), reverse=True):
                if bp - fair < take_edge:
                    break
                room = max(0, limit + pos)
                qty = min(ob.buy_orders[bp], room, base_size)
                if qty > 0:
                    orders[sym].append(Order(sym, bp, -qty))
                    pos -= qty

        pos_now[sym] = pos

        # Reservation skewed by inventory
        skew = k_inv * (pos / max(1, soft_cap)) * max(2, half_spread * 2)
        res = fair - skew
        bid = int(math.floor(res - half_spread))
        ask = int(math.ceil(res + half_spread))

        # Place inside the book (one tick better than best) when allowed
        if bb is not None and bid > bb:
            bid = bb + 1 if bb + 1 < (ba if ba is not None else bb + 100) else bb
        if ba is not None and ask < ba:
            ask = ba - 1 if ba - 1 > (bb if bb is not None else ba - 100) else ba

        # Avoid crossing
        if ba is not None:
            bid = min(bid, ba - 1)
        if bb is not None:
            ask = max(ask, bb + 1)

        room_buy = max(0, soft_cap - pos)
        room_sell = max(0, soft_cap + pos)
        b_size = min(base_size, room_buy)
        a_size = min(base_size, room_sell)
        if b_size > 0 and bid > 0:
            orders[sym].append(Order(sym, bid, b_size))
        if a_size > 0 and ask > 0:
            orders[sym].append(Order(sym, ask, -a_size))

    # ------------------------------------------------------------------
    def _closeout_all(self, orders, pos_now, books, timestamp):
        steps_left = max(1, (HARD_UNWIND - timestamp) // 100)
        if timestamp >= HARD_UNWIND:
            steps_left = 1
        for sym, p in pos_now.items():
            if p == 0:
                continue
            ob = books.get(sym)
            if ob is None:
                continue
            bb, ba = best_bid_ask(ob)
            if p > 0 and bb is not None:
                if timestamp >= HARD_UNWIND:
                    remaining = p
                    for bp in sorted(ob.buy_orders.keys(), reverse=True):
                        if remaining <= 0:
                            break
                        take = min(ob.buy_orders[bp], remaining)
                        if take > 0:
                            orders[sym].append(Order(sym, bp, -take))
                            remaining -= take
                else:
                    qty = max(1, (p + steps_left - 1) // steps_left)
                    orders[sym].append(Order(sym, bb, -min(qty, p)))
            elif p < 0 and ba is not None:
                if timestamp >= HARD_UNWIND:
                    remaining = -p
                    for ap in sorted(ob.sell_orders.keys()):
                        if remaining <= 0:
                            break
                        take = min(abs(ob.sell_orders[ap]), remaining)
                        if take > 0:
                            orders[sym].append(Order(sym, ap, take))
                            remaining -= take
                else:
                    qty = max(1, (-p + steps_left - 1) // steps_left)
                    orders[sym].append(Order(sym, ba, min(qty, -p)))