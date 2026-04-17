#!/usr/bin/env python3
"""Runner for the S1-S5 structured matrix. Generates and uploads all 76 configs."""

import sys, logging, time, csv, py_compile, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from auth import TokenProvider
from client import APIClient
from config import Config
from main import run_single, setup_logging
from storage import Database
from optimizer.auto_auth import AutoAuth
from optimizer.test_s1s5 import ALL_S1S5
from utils import save_json, timestamp_slug

setup_logging(debug=False)
logger = logging.getLogger("s1s5")

auth = AutoAuth("albertyea380@gmail.com", "Sz82434653@@")
token = auth.get_token()
config = Config.load(cli_overrides={"bearer_token": token, "max_retries": 10, "retry_backoff_max": 120.0})
tp = TokenProvider(config.bearer_token)
client = APIClient(config, tp)
db = Database(config)


def run_fn(file_path):
    fresh = auth.get_token()
    tp.set_token(fresh)
    try:
        return run_single(client, config, db, file_path, force_upload=True)
    except Exception as e:
        logger.error("Failed: %s", e)
        return None


def write_strategy(name: str, ash_code: str) -> str:
    code = f'''\
import json
from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List, Tuple, Optional

def get_best_bid_ask(od):
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask

PEPPER_LIMIT = 80

{ash_code}

def trade_pepper(state, ts):
    product = "INTARIAN_PEPPER_ROOT"
    od = state.order_depths.get(product)
    if od is None:
        return []
    pos = state.position.get(product, 0)
    limit = PEPPER_LIMIT
    best_bid, best_ask = get_best_bid_ask(od)
    orders = []
    if best_ask is not None:
        for ap in sorted(od.sell_orders.keys()):
            vol = abs(od.sell_orders[ap])
            qty = min(vol, limit - pos)
            if qty > 0:
                orders.append(Order(product, ap, qty))
                pos += qty
            if pos >= limit:
                break
    remaining = limit - pos
    if remaining > 0:
        if best_bid is not None and best_ask is not None:
            bp = best_bid + 1
            if bp < best_ask:
                orders.append(Order(product, bp, remaining))
        elif best_bid is not None:
            orders.append(Order(product, best_bid + 1, remaining))
    return orders

class Trader:
    def run(self, state):
        orders = {{}}
        ts = json.loads(state.traderData) if state.traderData else {{}}
        for product in state.order_depths:
            if product == "ASH_COATED_OSMIUM":
                orders[product] = trade_ash(state, ts)
            elif product == "INTARIAN_PEPPER_ROOT":
                orders[product] = trade_pepper(state, ts)
        return orders, 0, json.dumps(ts)
'''
    path = Path("generated") / f"{name}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code)
    py_compile.compile(str(path), doraise=True)
    return str(path)


def gen_take_schedule_ash(cfg: dict) -> str:
    """Generate ASH code for standard take-schedule configs."""
    schedule = cfg["take_schedule"]
    hs = cfg.get("ash_half_spread", 1)
    bs = cfg.get("ash_base_size", 20)
    k_inv = cfg.get("ash_k_inv", 2.5)
    limit = cfg.get("ash_position_limit", 50)

    # Build take schedule if/elif/else (4-space indent for function body)
    sched_lines = []
    for i, (thr, te) in enumerate(schedule):
        if thr >= 1.0:
            sched_lines.append(f"    te = {te}")
        else:
            kw = "if" if i == 0 else "elif"
            sched_lines.append(f"    {kw} inv_ratio < {thr}:\n        te = {te}")
    if not any(t >= 1.0 for t, _ in schedule):
        sched_lines.append(f"    else:\n        te = 0")
    sched_code = "\n".join(sched_lines)

    return f'''\
def trade_ash(state, ts):
    product = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(product)
    if od is None:
        return []
    pos = state.position.get(product, 0)
    limit = {limit}
    best_bid, best_ask = get_best_bid_ask(od)
    if best_bid is None or best_ask is None:
        return []
    fair_r = 10000
    inv_ratio = abs(pos) / limit if limit > 0 else 0
    orders = []
    # Take schedule
{sched_code}
    for ap in sorted(od.sell_orders.keys()):
        if ap <= fair_r - te:
            vol = abs(od.sell_orders[ap])
            qty = min(vol, limit - pos)
            if qty > 0:
                orders.append(Order(product, ap, qty))
                pos += qty
        else:
            break
    for bp in sorted(od.buy_orders.keys(), reverse=True):
        if bp >= fair_r + te:
            vol = od.buy_orders[bp]
            qty = min(vol, limit + pos)
            if qty > 0:
                orders.append(Order(product, bp, -qty))
                pos -= qty
        else:
            break
    res = fair_r - {k_inv} * (pos / limit) if limit > 0 else fair_r
    res_r = round(res)
    bid_p = res_r - {hs}
    ask_p = res_r + {hs}
    if best_bid is not None:
        bid_p = min(best_bid + 1, bid_p)
    if best_ask is not None:
        ask_p = max(best_ask - 1, ask_p)
    bid_p = min(bid_p, best_ask - 1)
    ask_p = max(ask_p, best_bid + 1)
    buy_qty = min({bs}, limit - pos)
    sell_qty = min({bs}, limit + pos)
    if buy_qty > 0:
        orders.append(Order(product, bid_p, buy_qty))
    if sell_qty > 0:
        orders.append(Order(product, ask_p, -sell_qty))
    if abs(pos) >= 0.9 * limit:
        if pos > 0 and best_bid is not None:
            orders.append(Order(product, best_bid, -min(10, pos)))
        elif pos < 0 and best_ask is not None:
            orders.append(Order(product, best_ask, min(10, -pos)))
    return orders'''


def gen_size_schedule_ash(cfg: dict) -> str:
    """Generate ASH code for S3 size-schedule configs."""
    t1, t2 = cfg["t1"], cfg["t2"]
    bs0, bs1, bs2 = cfg["bs0"], cfg["bs1"], cfg["bs2"]

    return f'''\
def trade_ash(state, ts):
    product = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(product)
    if od is None:
        return []
    pos = state.position.get(product, 0)
    limit = 50
    best_bid, best_ask = get_best_bid_ask(od)
    if best_bid is None or best_ask is None:
        return []
    fair_r = 10000
    inv_ratio = abs(pos) / limit if limit > 0 else 0
    orders = []
    te = 1 if inv_ratio < 0.30 else 0
    for ap in sorted(od.sell_orders.keys()):
        if ap <= fair_r - te:
            vol = abs(od.sell_orders[ap])
            qty = min(vol, limit - pos)
            if qty > 0:
                orders.append(Order(product, ap, qty))
                pos += qty
        else:
            break
    for bp in sorted(od.buy_orders.keys(), reverse=True):
        if bp >= fair_r + te:
            vol = od.buy_orders[bp]
            qty = min(vol, limit + pos)
            if qty > 0:
                orders.append(Order(product, bp, -qty))
                pos -= qty
        else:
            break
    # Size schedule
    if inv_ratio < {t1}:
        bs = {bs0}
    elif inv_ratio < {t2}:
        bs = {bs1}
    else:
        bs = {bs2}
    res = fair_r - 2.5 * (pos / limit) if limit > 0 else fair_r
    res_r = round(res)
    bid_p = res_r - 1
    ask_p = res_r + 1
    if best_bid is not None:
        bid_p = min(best_bid + 1, bid_p)
    if best_ask is not None:
        ask_p = max(best_ask - 1, ask_p)
    bid_p = min(bid_p, best_ask - 1)
    ask_p = max(ask_p, best_bid + 1)
    buy_qty = min(bs, limit - pos)
    sell_qty = min(bs, limit + pos)
    if buy_qty > 0:
        orders.append(Order(product, bid_p, buy_qty))
    if sell_qty > 0:
        orders.append(Order(product, ask_p, -sell_qty))
    if abs(pos) >= 0.9 * limit:
        if pos > 0 and best_bid is not None:
            orders.append(Order(product, best_bid, -min(10, pos)))
        elif pos < 0 and best_ask is not None:
            orders.append(Order(product, best_ask, min(10, -pos)))
    return orders'''


def gen_pepper_coupling_ash(cfg: dict) -> str:
    """Generate ASH code for S4 PEPPER-load-aware configs."""
    load_cut = cfg["load_cut"]
    t1 = cfg["t1"]
    mode = cfg["mode"]
    boost = cfg["boost"]

    if mode == "take":
        boost_code = f'''\
    if pepper_ratio >= {load_cut} and inv_ratio < {t1}:
        te = min(te + {boost}, 2)'''
    else:  # size
        boost_code = f'''\
    if pepper_ratio >= {load_cut} and inv_ratio < {t1}:
        bs = bs + {boost}'''

    return f'''\
def trade_ash(state, ts):
    product = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(product)
    if od is None:
        return []
    pos = state.position.get(product, 0)
    limit = 50
    best_bid, best_ask = get_best_bid_ask(od)
    if best_bid is None or best_ask is None:
        return []
    fair_r = 10000
    inv_ratio = abs(pos) / limit if limit > 0 else 0
    pepper_pos = state.position.get("INTARIAN_PEPPER_ROOT", 0)
    pepper_ratio = abs(pepper_pos) / 80
    orders = []
    te = 1 if inv_ratio < 0.30 else 0
    bs = 20
{boost_code}
    for ap in sorted(od.sell_orders.keys()):
        if ap <= fair_r - te:
            vol = abs(od.sell_orders[ap])
            qty = min(vol, limit - pos)
            if qty > 0:
                orders.append(Order(product, ap, qty))
                pos += qty
        else:
            break
    for bp in sorted(od.buy_orders.keys(), reverse=True):
        if bp >= fair_r + te:
            vol = od.buy_orders[bp]
            qty = min(vol, limit + pos)
            if qty > 0:
                orders.append(Order(product, bp, -qty))
                pos -= qty
        else:
            break
    res = fair_r - 2.5 * (pos / limit) if limit > 0 else fair_r
    res_r = round(res)
    bid_p = res_r - 1
    ask_p = res_r + 1
    if best_bid is not None:
        bid_p = min(best_bid + 1, bid_p)
    if best_ask is not None:
        ask_p = max(best_ask - 1, ask_p)
    bid_p = min(bid_p, best_ask - 1)
    ask_p = max(ask_p, best_bid + 1)
    buy_qty = min(bs, limit - pos)
    sell_qty = min(bs, limit + pos)
    if buy_qty > 0:
        orders.append(Order(product, bid_p, buy_qty))
    if sell_qty > 0:
        orders.append(Order(product, ask_p, -sell_qty))
    if abs(pos) >= 0.9 * limit:
        if pos > 0 and best_bid is not None:
            orders.append(Order(product, best_bid, -min(10, pos)))
        elif pos < 0 and best_ask is not None:
            orders.append(Order(product, best_ask, min(10, -pos)))
    return orders'''


def gen_s5_confirm(cfg: dict) -> str:
    """Generate ASH code for S5 failure confirmation configs."""
    mode = cfg.get("mode", "")
    if mode == "pepper_defensive":
        return '''\
def trade_ash(state, ts):
    product = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(product)
    if od is None:
        return []
    pos = state.position.get(product, 0)
    limit = 50
    best_bid, best_ask = get_best_bid_ask(od)
    if best_bid is None or best_ask is None:
        return []
    fair_r = 10000
    inv_ratio = abs(pos) / limit if limit > 0 else 0
    pepper_pos = state.position.get("INTARIAN_PEPPER_ROOT", 0)
    pepper_ratio = abs(pepper_pos) / 80
    orders = []
    te = 1 if inv_ratio < 0.30 else 0
    hs = 1
    if pepper_ratio >= 0.50:
        te = 0
        hs = 2
    for ap in sorted(od.sell_orders.keys()):
        if ap <= fair_r - te:
            vol = abs(od.sell_orders[ap])
            qty = min(vol, limit - pos)
            if qty > 0:
                orders.append(Order(product, ap, qty))
                pos += qty
        else:
            break
    for bp in sorted(od.buy_orders.keys(), reverse=True):
        if bp >= fair_r + te:
            vol = od.buy_orders[bp]
            qty = min(vol, limit + pos)
            if qty > 0:
                orders.append(Order(product, bp, -qty))
                pos -= qty
        else:
            break
    res = fair_r - 2.5 * (pos / limit) if limit > 0 else fair_r
    res_r = round(res)
    bid_p = res_r - hs
    ask_p = res_r + hs
    if best_bid is not None:
        bid_p = min(best_bid + 1, bid_p)
    if best_ask is not None:
        ask_p = max(best_ask - 1, ask_p)
    bid_p = min(bid_p, best_ask - 1)
    ask_p = max(ask_p, best_bid + 1)
    buy_qty = min(20, limit - pos)
    sell_qty = min(20, limit + pos)
    if buy_qty > 0:
        orders.append(Order(product, bid_p, buy_qty))
    if sell_qty > 0:
        orders.append(Order(product, ask_p, -sell_qty))
    return orders'''
    elif mode == "pepper_defensive_wide":
        return '''\
def trade_ash(state, ts):
    product = "ASH_COATED_OSMIUM"
    od = state.order_depths.get(product)
    if od is None:
        return []
    pos = state.position.get(product, 0)
    limit = 50
    best_bid, best_ask = get_best_bid_ask(od)
    if best_bid is None or best_ask is None:
        return []
    fair_r = 10000
    inv_ratio = abs(pos) / limit if limit > 0 else 0
    pepper_pos = state.position.get("INTARIAN_PEPPER_ROOT", 0)
    pepper_ratio = abs(pepper_pos) / 80
    orders = []
    te = 1 if inv_ratio < 0.30 else 0
    hs = 2  # always wide ask
    if pepper_ratio >= 0.50:
        te = 0
    for ap in sorted(od.sell_orders.keys()):
        if ap <= fair_r - te:
            vol = abs(od.sell_orders[ap])
            qty = min(vol, limit - pos)
            if qty > 0:
                orders.append(Order(product, ap, qty))
                pos += qty
        else:
            break
    for bp in sorted(od.buy_orders.keys(), reverse=True):
        if bp >= fair_r + te:
            vol = od.buy_orders[bp]
            qty = min(vol, limit + pos)
            if qty > 0:
                orders.append(Order(product, bp, -qty))
                pos -= qty
        else:
            break
    res = fair_r - 2.5 * (pos / limit) if limit > 0 else fair_r
    res_r = round(res)
    bid_p = res_r - hs
    ask_p = res_r + hs
    if best_bid is not None:
        bid_p = min(best_bid + 1, bid_p)
    if best_ask is not None:
        ask_p = max(best_ask - 1, ask_p)
    bid_p = min(bid_p, best_ask - 1)
    ask_p = max(ask_p, best_bid + 1)
    buy_qty = min(20, limit - pos)
    sell_qty = min(20, limit + pos)
    if buy_qty > 0:
        orders.append(Order(product, bid_p, buy_qty))
    if sell_qty > 0:
        orders.append(Order(product, ask_p, -sell_qty))
    return orders'''
    return gen_take_schedule_ash(cfg)


def generate_config(cfg: dict) -> str:
    """Route to the correct code generator based on config family."""
    name = cfg["name"]
    family = cfg.get("family", "")

    if family in ("baseline", "S1_2band", "S2_3band"):
        ash_code = gen_take_schedule_ash(cfg)
    elif family == "S3_size":
        ash_code = gen_size_schedule_ash(cfg)
    elif family == "S4_pepper":
        ash_code = gen_pepper_coupling_ash(cfg)
    elif family == "S5_confirm":
        ash_code = gen_s5_confirm(cfg)
    else:
        ash_code = gen_take_schedule_ash(cfg)

    return write_strategy(name, ash_code)


# ═══════════════════════════════════════════════════════════
# MAIN EXECUTION
# ═══════════════════════════════════════════════════════════

total = len(ALL_S1S5)
logger.info("S1-S5 matrix: %d configs", total)

# Verify all compile
for cfg in ALL_S1S5:
    generate_config(cfg)
logger.info("All %d configs compile OK", total)

results = []
baseline_pnl = None

for i, cfg in enumerate(ALL_S1S5, 1):
    name = cfg["name"]
    family = cfg.get("family", "unknown")
    logger.info("[%d/%d] %s (%s)", i, total, name, family)

    path = generate_config(cfg)
    summary = run_fn(path)

    if summary:
        pnl = summary.final_pnl
        if name == "B0_01":
            baseline_pnl = pnl
        delta = pnl - baseline_pnl if baseline_pnl else 0
        results.append({"name": name, "family": family, "pnl": pnl, "delta": delta})
        logger.info("[%d/%d] %s PnL=%.1f delta=%+.0f", i, total, name, pnl, delta)
    else:
        results.append({"name": name, "family": family, "pnl": None, "delta": None})
        logger.warning("[%d/%d] %s FAILED", i, total, name)

    if i < total:
        time.sleep(20)

# ═══════════════════════════════════════════════════════════
# RESULTS
# ═══════════════════════════════════════════════════════════

valid = [r for r in results if r["pnl"] is not None]
valid.sort(key=lambda r: r["pnl"], reverse=True)

print()
print("=" * 80)
print("S1-S5 STRUCTURED MATRIX RESULTS")
print("=" * 80)
print(f"{'Rank':<5}{'Config':<15}{'Family':<12}{'PnL':>10}{'Delta':>8}")
print("-" * 50)
for rank, r in enumerate(valid, 1):
    print(f"{rank:<5}{r['name']:<15}{r['family']:<12}{r['pnl']:>10,.1f}{r['delta']:>+8,.0f}")
print("=" * 80)
print(f"Baseline: {baseline_pnl:,.1f}")
if valid:
    print(f"Best: {valid[0]['name']} = {valid[0]['pnl']:,.1f} ({valid[0]['delta']:+,.0f})")

# Per-family top 3
for fam in ["S1_2band", "S2_3band", "S3_size", "S4_pepper", "S5_confirm"]:
    fam_r = [r for r in valid if r["family"] == fam][:3]
    if fam_r:
        print(f"\nTop 3 {fam}:")
        for r in fam_r:
            print(f"  {r['name']:<15} PnL={r['pnl']:>10,.1f}  delta={r['delta']:>+6,.0f}")

# Save
save_json({"baseline": baseline_pnl, "results": valid},
          f"results/s1s5_{timestamp_slug()}.json")

csv_path = Path("results/s1s5_results.csv")
csv_path.parent.mkdir(parents=True, exist_ok=True)
with open(csv_path, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["name", "family", "pnl", "delta"])
    w.writeheader()
    for r in valid:
        w.writerow(r)

db.close()
logger.info("S1-S5 complete. %d/%d successful.", len(valid), total)
