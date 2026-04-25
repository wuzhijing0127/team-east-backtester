"""
r3_refined_stoploss_v1 — v3 base + 9-layer stop-loss engine
=============================================================

Builds on r3_refined_v3_fastema (current best: PnL 8,826, MaxDD 339) by adding
a multi-layered stop-loss system that adjusts trading behavior per product
based on a 5-state state machine.

STATE MACHINE (per product):
  NORMAL    — full sizes, normal quoting
  CAUTIOUS  — 50% sizes, half_spread +1, reduced take aggression
  DEFENSIVE — 25% sizes, scalp only (no passive quoting)
  FLATTEN   — no new entries; actively reduce position toward 0
  HALT      — zero trading (circuit-breaker tripped)

STOP-LOSS TRIGGER HIERARCHY (evaluated each tick, highest priority first):

  L1  PORTFOLIO KILL SWITCH       total MtM PnL < -5000   → HALT all
  L2  PORTFOLIO CIRCUIT BREAKER   DD from rolling high > 2500 → HALT all
  L3  PER-PRODUCT TRAILING STOP   product DD > 1500 → that product FLATTEN
  L4  PEPPER PRICE TRAILING       mid from rolling-high-mid > 8 ticks AND pos>0 → reduce 50%
  L5  ASH ADVERSE DEVIATION       |mid-ema| > 10 AND pos wrong-side → FLATTEN ASH
  L6  REGIME REVERSAL (PEPPER)    EMA-short < EMA-long for 10 consec ticks → DEFENSIVE
  L7  VOLATILITY SPIKE            realized vol > 2.5× rolling avg → CAUTIOUS
  L8  RAPID-MOVE STOP             |mid-Δ| > 15 in 5 ticks → pause new entries 1 tick
  L9  ADVERSE SELECTION (ASH)     cumulative avg_buy > avg_sell over window → CAUTIOUS
  L10 CONSECUTIVE LOSSES          5 consecutive MtM-negative ticks → CAUTIOUS; reset on 3 wins
  L11 STAGNATION STOP             no progress over 200 ticks → CAUTIOUS
  L12 END-OF-SESSION WINDOW       last 20k ticks → CAUTIOUS, last 5k → FLATTEN

Recovery logic:
  - Mode downgrades on trigger clearance (sticky — don't flap every tick)
  - Min hold time in DEFENSIVE/FLATTEN = 20 ticks before upgrade
  - HALT is only cleared by manual restart (not auto-recovered)
"""

import json
from datamodel import Order, OrderDepth, TradingState

# === MAF_HOOK (round 3 confirmed MAF ignored) ===
MAF = 0
# =================================================

# Core trading parameters (from v3 winner)
ASH_LIMIT = 20
ASH_INIT_FAIR = 10000
ASH_EMA_ALPHA = 0.05
ASH_HALF_SPREAD = 1
ASH_K_INV = 2.5
ASH_BASE_SIZE = 8
ASH_TAKE_EDGE = 1
ASH_MICRO_BETA = 0.15   # NEW: microprice tilt coefficient — fair = ema + β·(micro-mid)

PEPPER_LIMIT = 80
PEPPER_EMA_ALPHA = 0.02
PEPPER_DIP_K = 2

# ============= Stop-loss thresholds (tune here) =============
PORTFOLIO_KILL_SWITCH = -5000       # L1: total MtM PnL floor
PORTFOLIO_DD_HARD = 2500            # L2: max drawdown from peak
PROD_TRAILING_STOP = 1500           # L3: per-product drawdown
PEPPER_TRAIL_TICKS = 8              # L4: PEPPER mid trailing stop
ASH_DEVIATION_LIMIT = 10            # L5: ASH mid deviation from EMA
PEPPER_EMA_SHORT_ALPHA = 0.05       # L6: short EMA for regime detection
PEPPER_EMA_LONG_ALPHA = 0.01        # L6: long EMA for regime detection
REGIME_CONFIRM_TICKS = 10           # L6: ticks of crossed EMAs to confirm
VOL_WINDOW = 20                     # L7: vol estimation window
VOL_SPIKE_MULT = 2.5                # L7: spike threshold multiplier
VOL_BASELINE_WINDOW = 100           # L7: rolling baseline vol window
RAPID_MOVE_TICKS = 15               # L8: price-change tick threshold
RAPID_MOVE_WINDOW = 5               # L8: window for rapid-move detection
ADVERSE_WINDOW = 40                 # L9: recent ASH trades tracked
ADVERSE_THRESHOLD = 150             # L9: cumulative adverse threshold
CONSEC_LOSS_LIMIT = 5               # L10: consecutive losses trigger
CONSEC_LOSS_RESET = 3               # L10: consecutive wins to reset
STAG_WINDOW = 200                   # L11: no-progress window
STAG_EPSILON = 50                   # L11: PnL-change threshold
EOS_CAUTIOUS_WINDOW = 20000         # L12: enter CAUTIOUS if ts > sim_end - this
EOS_FLATTEN_WINDOW = 5000           # L12: enter FLATTEN if ts > sim_end - this
SIM_END_TS = 99900                  # round-3 sim end timestamp

MIN_HOLD_TICKS_DEFENSIVE = 20       # how long to stay defensive before relaxing


# ============= Helpers =============

def bb_ba(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def mid_of(od):
    b, a = bb_ba(od)
    if b is None or a is None:
        return None
    return (b + a) / 2


def microprice(od):
    """Volume-weighted fair estimator — tilts toward heavier side of book."""
    b, a = bb_ba(od)
    if b is None or a is None:
        return None
    bv = od.buy_orders[b]
    av = abs(od.sell_orders[a])
    t = bv + av
    if t == 0:
        return (b + a) / 2
    return (b * av + a * bv) / t


# ============= State-machine actions =============
# Mode → (size_multiplier, spread_add, allow_passive, allow_take)
MODE_TABLE = {
    "NORMAL":    (1.0, 0, True,  True),
    "CAUTIOUS":  (0.5, 1, True,  True),
    "DEFENSIVE": (0.25, 2, False, True),
    "FLATTEN":   (0.0, 2, False, True),    # no new passive quotes; take is OK for flattening
    "HALT":      (0.0, 0, False, False),
}


def init_state(ts):
    if "stop_loss" not in ts:
        ts["stop_loss"] = {
            "portfolio": {
                "rolling_high": 0.0,
                "total_pnl_est": 0.0,
                "mode": "NORMAL",
                "halted": False,
            },
            "ash": _fresh_product_state(ASH_INIT_FAIR),
            "pepper": _fresh_product_state(None),
            "last_ts": 0,
        }
    return ts["stop_loss"]


def _fresh_product_state(init_fair):
    return {
        "mode": "NORMAL",
        "mode_entered_ts": 0,
        "rolling_high_pnl": 0.0,
        "rolling_high_mid": None,
        "ema_fair": init_fair,
        "ema_short": None,
        "ema_long": None,
        "regime_crossed_streak": 0,
        "cost_basis": 0.0,
        "last_mid": None,
        "mid_history": [],
        "pnl_history": [],
        "vol_history": [],
        "recent_trades": [],     # for adverse-selection tracking (ASH)
        "consec_losses": 0,
        "consec_wins": 0,
        "pnl_progress_ref": 0.0,
        "pnl_progress_ts": 0,
        "pnl_est": 0.0,
    }


# ============= PnL estimation (per product) =============
# We don't have direct PnL feed; estimate MtM from position + cost-basis + mid.
# Cost basis updated ONLY when we detect position change via state.position.

def update_pnl_estimate(sl_prod, pos, mid, last_pos):
    """Estimate product MtM given current pos and mid."""
    if mid is None:
        return sl_prod["pnl_est"]
    if last_pos is None:
        last_pos = pos
    # Position delta
    delta = pos - last_pos
    if delta != 0 and sl_prod["last_mid"] is not None:
        # We transacted; assume at last known mid (best guess without fill price)
        fill_px = sl_prod["last_mid"]
        # Update cost basis via weighted avg for same-sign adds; realized for reductions
        if last_pos == 0:
            sl_prod["cost_basis"] = fill_px
        elif (last_pos > 0 and delta > 0) or (last_pos < 0 and delta < 0):
            # Adding to position — weighted average cost
            new_qty = abs(last_pos) + abs(delta)
            sl_prod["cost_basis"] = (
                abs(last_pos) * sl_prod["cost_basis"] + abs(delta) * fill_px
            ) / new_qty if new_qty > 0 else fill_px
        else:
            # Reducing position — realize PnL
            realized = abs(delta) * (fill_px - sl_prod["cost_basis"]) * (1 if last_pos > 0 else -1)
            sl_prod["pnl_est"] += realized
    # Mark to market unrealized
    if pos != 0:
        unrealized = pos * (mid - sl_prod["cost_basis"])
    else:
        unrealized = 0.0
    return sl_prod["pnl_est"] + unrealized


# ============= Trigger evaluators =============

def eval_triggers(ts_state, sl, product_key, pos, mid, limit, ts):
    """Return the desired mode for this product after evaluating all triggers."""
    sl_prod = sl[product_key]
    portfolio = sl["portfolio"]

    # L1: PORTFOLIO KILL SWITCH
    if portfolio["total_pnl_est"] < PORTFOLIO_KILL_SWITCH:
        portfolio["halted"] = True
        portfolio["mode"] = "HALT"
        return "HALT"

    # L2: PORTFOLIO CIRCUIT BREAKER (drawdown from peak)
    pf_dd = portfolio["rolling_high"] - portfolio["total_pnl_est"]
    if pf_dd > PORTFOLIO_DD_HARD:
        portfolio["mode"] = "HALT"
        return "HALT"

    # L3: PER-PRODUCT TRAILING STOP
    prod_dd = sl_prod["rolling_high_pnl"] - sl_prod["pnl_est"]
    if prod_dd > PROD_TRAILING_STOP:
        return "FLATTEN"

    proposed_mode = "NORMAL"

    # L4: PEPPER PRICE TRAILING STOP
    if product_key == "pepper" and mid is not None:
        if sl_prod["rolling_high_mid"] is None or mid > sl_prod["rolling_high_mid"]:
            sl_prod["rolling_high_mid"] = mid
        drop = sl_prod["rolling_high_mid"] - mid
        if drop > PEPPER_TRAIL_TICKS and pos > 0:
            proposed_mode = "DEFENSIVE"

    # L5: ASH ADVERSE DEVIATION STOP
    if product_key == "ash" and mid is not None and sl_prod["ema_fair"] is not None:
        dev = mid - sl_prod["ema_fair"]
        # Wrong-side: long but mid below fair, or short but mid above fair
        wrong_side = (pos > 0 and dev < -ASH_DEVIATION_LIMIT) or (
            pos < 0 and dev > ASH_DEVIATION_LIMIT
        )
        if wrong_side:
            proposed_mode = _max_severity(proposed_mode, "FLATTEN")

    # L6: REGIME REVERSAL (PEPPER EMA short < long)
    if product_key == "pepper" and mid is not None:
        if sl_prod["ema_short"] is None:
            sl_prod["ema_short"] = mid
            sl_prod["ema_long"] = mid
        sl_prod["ema_short"] = PEPPER_EMA_SHORT_ALPHA * mid + (1 - PEPPER_EMA_SHORT_ALPHA) * sl_prod["ema_short"]
        sl_prod["ema_long"] = PEPPER_EMA_LONG_ALPHA * mid + (1 - PEPPER_EMA_LONG_ALPHA) * sl_prod["ema_long"]
        if sl_prod["ema_short"] < sl_prod["ema_long"]:
            sl_prod["regime_crossed_streak"] += 1
        else:
            sl_prod["regime_crossed_streak"] = 0
        if sl_prod["regime_crossed_streak"] >= REGIME_CONFIRM_TICKS:
            proposed_mode = _max_severity(proposed_mode, "DEFENSIVE")

    # L7: VOLATILITY SPIKE
    if mid is not None and sl_prod["last_mid"] is not None:
        move = abs(mid - sl_prod["last_mid"])
        sl_prod["vol_history"].append(move)
        if len(sl_prod["vol_history"]) > VOL_BASELINE_WINDOW:
            sl_prod["vol_history"] = sl_prod["vol_history"][-VOL_BASELINE_WINDOW:]
        if len(sl_prod["vol_history"]) >= VOL_WINDOW:
            recent_vol = sum(sl_prod["vol_history"][-VOL_WINDOW:]) / VOL_WINDOW
            baseline = sum(sl_prod["vol_history"]) / len(sl_prod["vol_history"]) or 1
            if recent_vol > baseline * VOL_SPIKE_MULT:
                proposed_mode = _max_severity(proposed_mode, "CAUTIOUS")

    # L8: RAPID-MOVE STOP
    if mid is not None:
        sl_prod["mid_history"].append((ts, mid))
        sl_prod["mid_history"] = [h for h in sl_prod["mid_history"] if h[0] >= ts - RAPID_MOVE_WINDOW * 100]
        if len(sl_prod["mid_history"]) >= 2:
            recent = [h[1] for h in sl_prod["mid_history"]]
            move = max(recent) - min(recent)
            if move > RAPID_MOVE_TICKS:
                proposed_mode = _max_severity(proposed_mode, "CAUTIOUS")

    # L9: ADVERSE-SELECTION DETECTOR (ASH-specific, tracks fills)
    if product_key == "ash" and len(sl_prod["recent_trades"]) >= ADVERSE_WINDOW:
        recent = sl_prod["recent_trades"][-ADVERSE_WINDOW:]
        buys = [t[1] for t in recent if t[0] > 0]
        sells = [t[1] for t in recent if t[0] < 0]
        if buys and sells:
            avg_buy = sum(buys) / len(buys)
            avg_sell = sum(sells) / len(sells)
            # If buying at higher avg than selling → adverse
            if avg_buy - avg_sell > 0 and (avg_buy - avg_sell) * min(len(buys), len(sells)) > ADVERSE_THRESHOLD:
                proposed_mode = _max_severity(proposed_mode, "CAUTIOUS")

    # L10: CONSECUTIVE LOSSES
    # Update on PnL change since last tick
    if len(sl_prod["pnl_history"]) >= 2:
        delta = sl_prod["pnl_history"][-1] - sl_prod["pnl_history"][-2]
        if delta < -1:
            sl_prod["consec_losses"] += 1
            sl_prod["consec_wins"] = 0
        elif delta > 1:
            sl_prod["consec_wins"] += 1
            sl_prod["consec_losses"] = max(0, sl_prod["consec_losses"] - 1)
        if sl_prod["consec_wins"] >= CONSEC_LOSS_RESET:
            sl_prod["consec_losses"] = 0
            sl_prod["consec_wins"] = 0
    if sl_prod["consec_losses"] >= CONSEC_LOSS_LIMIT:
        proposed_mode = _max_severity(proposed_mode, "CAUTIOUS")

    # L11: STAGNATION STOP
    if ts - sl_prod["pnl_progress_ts"] > STAG_WINDOW * 100:
        if abs(sl_prod["pnl_est"] - sl_prod["pnl_progress_ref"]) < STAG_EPSILON:
            proposed_mode = _max_severity(proposed_mode, "CAUTIOUS")
        sl_prod["pnl_progress_ref"] = sl_prod["pnl_est"]
        sl_prod["pnl_progress_ts"] = ts

    # L12: END-OF-SESSION WINDOW
    ticks_remaining = SIM_END_TS - ts
    if ticks_remaining <= EOS_FLATTEN_WINDOW:
        proposed_mode = _max_severity(proposed_mode, "FLATTEN")
    elif ticks_remaining <= EOS_CAUTIOUS_WINDOW:
        proposed_mode = _max_severity(proposed_mode, "CAUTIOUS")

    # Sticky downgrade — don't upgrade out of DEFENSIVE/FLATTEN for MIN_HOLD_TICKS
    current = sl_prod["mode"]
    if current in ("DEFENSIVE", "FLATTEN") and proposed_mode not in ("DEFENSIVE", "FLATTEN", "HALT"):
        held_for = ts - sl_prod["mode_entered_ts"]
        if held_for < MIN_HOLD_TICKS_DEFENSIVE * 100:
            proposed_mode = current

    if proposed_mode != current:
        sl_prod["mode_entered_ts"] = ts
    sl_prod["mode"] = proposed_mode
    return proposed_mode


def _max_severity(a, b):
    order = {"NORMAL": 0, "CAUTIOUS": 1, "DEFENSIVE": 2, "FLATTEN": 3, "HALT": 4}
    return a if order[a] >= order[b] else b


def update_rolling_highs(sl):
    for key in ("ash", "pepper"):
        if sl[key]["pnl_est"] > sl[key]["rolling_high_pnl"]:
            sl[key]["rolling_high_pnl"] = sl[key]["pnl_est"]
    total = sl["ash"]["pnl_est"] + sl["pepper"]["pnl_est"]
    sl["portfolio"]["total_pnl_est"] = total
    if total > sl["portfolio"]["rolling_high"]:
        sl["portfolio"]["rolling_high"] = total


# ============= Trading functions =============

def trade_ash(state, ts_data, sl, timestamp):
    prod = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(prod)
    if od is None:
        return []
    pos = state.position.get(prod, 0)
    last_pos = sl["ash"].get("last_pos", pos)
    bb, ba = bb_ba(od)
    mid = mid_of(od)
    micro = microprice(od)

    # Update EMA fair
    ema_fair = sl["ash"]["ema_fair"]
    if mid is not None:
        ema_fair = ASH_EMA_ALPHA * mid + (1 - ASH_EMA_ALPHA) * ema_fair
        sl["ash"]["ema_fair"] = ema_fair

    # Apply microprice tilt — dynamic fair responds to book imbalance
    if mid is not None and micro is not None:
        fair = ema_fair + ASH_MICRO_BETA * (micro - mid)
    else:
        fair = ema_fair
    fr = round(fair)

    # Update PnL estimate + history
    pnl = update_pnl_estimate(sl["ash"], pos, mid, last_pos)
    sl["ash"]["pnl_est"] = pnl
    sl["ash"]["pnl_history"].append(pnl)
    if len(sl["ash"]["pnl_history"]) > 500:
        sl["ash"]["pnl_history"] = sl["ash"]["pnl_history"][-500:]

    # Evaluate stop-loss triggers to pick mode
    mode = eval_triggers(ts_data, sl, "ash", pos, mid, ASH_LIMIT, timestamp)
    size_mult, spread_add, allow_passive, allow_take = MODE_TABLE[mode]

    sl["ash"]["last_mid"] = mid
    sl["ash"]["last_pos"] = pos

    orders = []
    if not allow_take and not allow_passive and mode != "FLATTEN":
        return orders

    # Take logic (allowed in all modes except HALT; in FLATTEN only to reduce)
    if allow_take:
        effective_take_edge = ASH_TAKE_EDGE + spread_add
        if ba is not None:
            for ap in sorted(od.sell_orders):
                # If FLATTEN and already short, we may need to buy to reduce
                want_buy = (mode != "FLATTEN") or (pos < 0)
                if not want_buy:
                    break
                if ap <= fr - effective_take_edge and pos < ASH_LIMIT:
                    q = min(abs(od.sell_orders[ap]), ASH_LIMIT - pos)
                    if q > 0:
                        orders.append(Order(prod, ap, q))
                        sl["ash"]["recent_trades"].append((q, ap))
                        pos += q
                else:
                    break
        if bb is not None:
            for bp_ in sorted(od.buy_orders, reverse=True):
                want_sell = (mode != "FLATTEN") or (pos > 0)
                if not want_sell:
                    break
                if bp_ >= fr + effective_take_edge and pos > -ASH_LIMIT:
                    q = min(od.buy_orders[bp_], ASH_LIMIT + pos)
                    if q > 0:
                        orders.append(Order(prod, bp_, -q))
                        sl["ash"]["recent_trades"].append((-q, bp_))
                        pos -= q
                else:
                    break

    # Trim recent_trades history
    if len(sl["ash"]["recent_trades"]) > ADVERSE_WINDOW * 2:
        sl["ash"]["recent_trades"] = sl["ash"]["recent_trades"][-ADVERSE_WINDOW * 2:]

    # Passive MM (only in NORMAL / CAUTIOUS)
    if allow_passive and mode in ("NORMAL", "CAUTIOUS"):
        res = fair - ASH_K_INV * (pos / ASH_LIMIT)
        rr = round(res)
        hs = ASH_HALF_SPREAD + spread_add
        bid_px = rr - hs
        ask_px = rr + hs
        if bb is not None:
            bid_px = min(bb + 1, bid_px)
        if ba is not None:
            ask_px = max(ba - 1, ask_px)
        if ba is not None:
            bid_px = min(bid_px, ba - 1)
        if bb is not None:
            ask_px = max(ask_px, bb + 1)
        sz = max(1, round(ASH_BASE_SIZE * size_mult))
        if pos < ASH_LIMIT:
            orders.append(Order(prod, bid_px, min(sz, ASH_LIMIT - pos)))
        if pos > -ASH_LIMIT:
            orders.append(Order(prod, ask_px, -min(sz, ASH_LIMIT + pos)))

    # FLATTEN — actively reduce at best quotes if passive isn't allowed
    if mode == "FLATTEN" and pos != 0:
        if pos > 0 and bb is not None:
            orders.append(Order(prod, bb, -min(pos, 15)))
        elif pos < 0 and ba is not None:
            orders.append(Order(prod, ba, min(-pos, 15)))

    return orders


def trade_pepper(state, ts_data, sl, timestamp):
    prod = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(prod)
    if od is None:
        return []
    pos = state.position.get(prod, 0)
    last_pos = sl["pepper"].get("last_pos", pos)
    bb, ba = bb_ba(od)
    mid = mid_of(od)

    # EMA for dip overlay (kept from v1, but may be disabled by mode)
    ema = ts_data.get("pep_ema", mid if mid is not None else 10000)
    if mid is not None:
        ema = PEPPER_EMA_ALPHA * mid + (1 - PEPPER_EMA_ALPHA) * ema
        ts_data["pep_ema"] = ema

    # Update PnL estimate
    pnl = update_pnl_estimate(sl["pepper"], pos, mid, last_pos)
    sl["pepper"]["pnl_est"] = pnl
    sl["pepper"]["pnl_history"].append(pnl)
    if len(sl["pepper"]["pnl_history"]) > 500:
        sl["pepper"]["pnl_history"] = sl["pepper"]["pnl_history"][-500:]

    mode = eval_triggers(ts_data, sl, "pepper", pos, mid, PEPPER_LIMIT, timestamp)
    size_mult, spread_add, allow_passive, allow_take = MODE_TABLE[mode]

    sl["pepper"]["last_mid"] = mid
    sl["pepper"]["last_pos"] = pos

    orders = []
    if mode == "HALT":
        return orders

    # FLATTEN: reduce position
    if mode == "FLATTEN" and pos > 0 and bb is not None:
        orders.append(Order(prod, bb, -min(pos, 30)))
        return orders
    if mode == "FLATTEN" and pos < 0 and ba is not None:
        orders.append(Order(prod, ba, min(-pos, 30)))
        return orders

    # Normal / Cautious / Defensive: scaled max-long engine
    target_limit = int(PEPPER_LIMIT * size_mult) if size_mult > 0 else 0

    if allow_take and ba is not None:
        for ap in sorted(od.sell_orders):
            if pos >= target_limit:
                break
            q = min(abs(od.sell_orders[ap]), target_limit - pos)
            if q > 0:
                orders.append(Order(prod, ap, q))
                pos += q

    if allow_passive and mode in ("NORMAL", "CAUTIOUS"):
        rem = target_limit - pos
        if rem > 0 and bb is not None:
            # Dip overlay only in NORMAL (too risky in CAUTIOUS)
            if mode == "NORMAL" and mid is not None and mid < ema - PEPPER_DIP_K:
                for offset, sz in [(0, 35), (-1, 25), (-2, 20)]:
                    if rem <= 0:
                        break
                    q = min(int(sz * size_mult), rem)
                    if q <= 0:
                        continue
                    px = bb + offset
                    if ba is not None and px >= ba:
                        px = ba - 1
                    orders.append(Order(prod, px, q))
                    rem -= q
            else:
                orders.append(Order(prod, bb, rem))

    return orders


class Trader:
    def run(self, state):
        orders = {}
        ts_data = json.loads(state.traderData) if state.traderData else {}
        sl = init_state(ts_data)
        timestamp = getattr(state, "timestamp", 0)

        # Update rolling highs BEFORE evaluating triggers (so peak is fresh)
        update_rolling_highs(sl)

        for prod in state.order_depths:
            if prod == "ASH_COATED_OSMIUM":
                orders[prod] = trade_ash(state, ts_data, sl, timestamp)
            elif prod == "INTARIAN_PEPPER_ROOT":
                orders[prod] = trade_pepper(state, ts_data, sl, timestamp)

        # Update rolling highs AGAIN after trades (for next tick's eval)
        update_rolling_highs(sl)
        sl["last_ts"] = timestamp

        return orders, 0, json.dumps(ts_data)
