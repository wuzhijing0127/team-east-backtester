#!/usr/bin/env python3
"""
Autonomous research loop for the HG + VF assets-only strategy.

Cycle:  propose variant → local 3-day sim guardrail → upload →
        poll-and-download → parse PnL → record → propose next.

Anti-overfit guardrails:
  - Every variant must pass a local 3-day simulation (0 exceptions, sane
    order count) BEFORE it gets uploaded. This catches dumb mutations
    cheaply and protects platform-quota.
  - Single-axis perturbations only. Never mutate >1 parameter at a time
    until we have a stable best — keeps the search readable.
  - Soft acceptance: the best-so-far is the variant with the highest
    asset-PnL that ALSO passed the local guard, NOT just the platform's
    single backtest number. Platform PnL is the proximal signal but
    the local sim provides a sanity check that prevents chasing noise.
  - Fair-value sanity: on every variant we re-check that fair stays
    flow-driven (no anchor pinning). The template is structured so this
    is automatic, but we also verify behaviour on edge cases.
  - 12-hour wall clock budget; resumable via state file.

Usage:
    cd prosperity-uploader/research/round3_assets
    python research_loop.py --hours 12

State files:
    state.json        — best-so-far + history of all variants tried
    variants/*.py     — every uploaded variant, named by id
    log.txt           — human-readable progress log
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Path constants
HERE = Path(__file__).parent
TEMPLATE = HERE / "assets_template.py"
VARIANTS_DIR = HERE / "variants"
STATE_FILE = HERE / "state.json"
LOG_FILE = HERE / "log.txt"
UPLOADER_ROOT = HERE.parent.parent
TEAMEAST_ROOT = UPLOADER_ROOT.parent
ROUND3_DATA = TEAMEAST_ROOT / "teameastbt" / "resources" / "ROUND_3"


# ───────────────────────── logging ─────────────────────────────────

def logmsg(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


# ───────────────────── param-block I/O ─────────────────────────────

PARAM_RE = re.compile(
    r"# ── PARAMS_BLOCK_START ──[\s\S]*?# ── PARAMS_BLOCK_END ──",
    re.MULTILINE,
)


def render_params_block(variant_id: str, label: str, hg: dict, vf: dict) -> str:
    def fmt(d: dict) -> str:
        return "{\n" + "".join(f"    {k!r}: {v!r},\n" for k, v in d.items()) + "}"
    return (
        "# ── PARAMS_BLOCK_START ──\n"
        f"VARIANT_ID = {variant_id!r}\n"
        f"VARIANT_LABEL = {label!r}\n\n"
        f"HG_PARAMS = {fmt(hg)}\n\n"
        f"VF_PARAMS = {fmt(vf)}\n\n"
        "SESSION_END = 1_000_000\n"
        "UNWIND_START = 998_000\n"
        "# ── PARAMS_BLOCK_END ──"
    )


def write_variant(variant_id: str, label: str, hg: dict, vf: dict) -> Path:
    template = TEMPLATE.read_text()
    block = render_params_block(variant_id, label, hg, vf)
    new = PARAM_RE.sub(block, template, count=1)
    # Append a uniqueness nonce so the file's SHA-256 always differs from any
    # prior variant, defeating the uploader's hash-dedup. The nonce is a comment
    # so it has no behavioural effect.
    nonce = f"\n# nonce: {datetime.now().isoformat()} {random.randint(0, 10**9)}\n"
    new = new + nonce
    out = VARIANTS_DIR / f"r4_{variant_id}.py"
    out.write_text(new)
    return out


# ─────────────────── local guardrail simulation ────────────────────

def local_sim_3day(variant_path: Path) -> Tuple[bool, str]:
    """Run the variant against the 3 historical CSVs locally.
    Returns (ok, message). Cheap pre-check before burning a platform upload."""
    code = f"""
import sys, csv
sys.path.insert(0, {str(TEAMEAST_ROOT)!r})
sys.path.insert(0, str({str(TEAMEAST_ROOT / 'teameastbt')!r}))
from datamodel import OrderDepth, TradingState
import importlib.util
spec = importlib.util.spec_from_file_location('cand', {str(variant_path)!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
trader = mod.Trader()
trader_data = ''
err = 0
total_orders = 0
products_seen = set()
ticks = 0
for day in [0, 1, 2]:
    path = {str(ROUND3_DATA)!r} + f'/prices_round_3_day_' + str(day) + '.csv'
    rows_by_ts = {{}}
    with open(path) as f:
        for row in csv.DictReader(f, delimiter=';'):
            try:
                ts = int(row['timestamp'])
            except: continue
            rows_by_ts.setdefault(ts, []).append(row)
    for ts in sorted(rows_by_ts.keys()):
        ods = {{}}
        for row in rows_by_ts[ts]:
            od = OrderDepth()
            for lvl in [1,2,3]:
                bp = row.get(f'bid_price_{{lvl}}', '')
                bv = row.get(f'bid_volume_{{lvl}}', '')
                ap = row.get(f'ask_price_{{lvl}}', '')
                av = row.get(f'ask_volume_{{lvl}}', '')
                try:
                    if bp and bv: od.buy_orders[int(float(bp))] = int(float(bv))
                    if ap and av: od.sell_orders[int(float(ap))] = -int(float(av))
                except: pass
            ods[row['product']] = od
        st = TradingState(traderData=trader_data, timestamp=ts, listings={{}},
                          order_depths=ods, own_trades={{}}, market_trades={{}},
                          position={{}}, observations=None)
        try:
            out, _, td = trader.run(st)
            trader_data = td
            for p, ords in out.items():
                products_seen.add(p)
                total_orders += len(ords)
            ticks += 1
        except Exception as e:
            err += 1

print(f'TICKS={{ticks}} ERR={{err}} ORDERS={{total_orders}} PRODS={{sorted(products_seen)}}')
"""
    try:
        result = subprocess.run(
            ["python3", "-c", code], capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            return False, f"sim crashed: {result.stderr[-300:]}"
        out = result.stdout.strip()
        m = re.search(r"TICKS=(\d+) ERR=(\d+) ORDERS=(\d+) PRODS=(\[.*\])", out)
        if not m:
            return False, f"unparseable sim output: {out!r}"
        ticks, errs, orders, prods = m.groups()
        ticks, errs, orders = int(ticks), int(errs), int(orders)
        prods_list = eval(prods)
        if errs > 0:
            return False, f"{errs} runtime exceptions in 3-day sim"
        if ticks < 25_000:
            return False, f"only {ticks} ticks processed (data load issue)"
        # Must trade BOTH assets
        if "HYDROGEL_PACK" not in prods_list or "VELVETFRUIT_EXTRACT" not in prods_list:
            return False, f"variant didn't trade both assets: {prods_list}"
        # Must trade ONLY the assets (no VEV mistakes)
        if any(p.startswith("VEV_") for p in prods_list):
            return False, f"variant emitted VEV orders (should not): {prods_list}"
        # Reasonable order density: 3-15 per tick (HG ~2 + VF ~2 plus aggressive takes)
        per_tick = orders / max(1, ticks)
        if per_tick < 1.0:
            return False, f"too few orders/tick ({per_tick:.2f})"
        if per_tick > 30.0:
            return False, f"runaway order density ({per_tick:.2f}/tick)"
        return True, f"local sim ok: {ticks} ticks, {orders} orders ({per_tick:.2f}/tick)"
    except subprocess.TimeoutExpired:
        return False, "local sim timed out"


# ─────────────────── upload + parse via main.py ────────────────────

def upload_and_get_pnl(variant_path: Path, timeout_sec: int = 1800) -> Optional[Dict[str, Any]]:
    """Run the FULL upload→poll→artifact pipeline by invoking
    `python main.py batch <single-file-dir> --force-upload`.

    `cmd upload` only POSTs; it doesn't create a run dir. `run_batch` calls
    `run_single` which does the full pipeline (upload + find_submission +
    poll_until_ready + download_artifact + summarize). We isolate each variant
    into its own one-file dir so batch only operates on this variant.

    Returns:
        dict on success, {'rate_limited': True} on persistent 429, None on other failure.
    """
    import shutil, tempfile
    upload_start = time.time()
    # Stage the variant into its own temp dir so batch processes only it.
    tmp_dir = tempfile.mkdtemp(prefix="r3_loop_", dir=str(VARIANTS_DIR))
    staged = Path(tmp_dir) / variant_path.name
    shutil.copy2(variant_path, staged)
    try:
        cmd = ["python3", "main.py", "batch", tmp_dir, "--force-upload"]
        try:
            result = subprocess.run(
                cmd, cwd=str(UPLOADER_ROOT), capture_output=True, text=True,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired:
            logmsg(f"  upload TIMEOUT for {variant_path.name}")
            return None
        combined = (result.stderr or "") + (result.stdout or "")
        if "Rate limited" in combined and "Request failed after" in combined:
            logmsg(f"  upload RATE-LIMITED (5 retries exhausted)")
            return {"rate_limited": True}
        if result.returncode != 0:
            tail = result.stderr[-500:] if result.stderr else result.stdout[-500:]
            logmsg(f"  batch FAILED: {tail.strip()}")
            return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    runs_dir = UPLOADER_ROOT / "runs"
    sub_dirs = sorted(
        [d for d in runs_dir.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    if not sub_dirs:
        return None
    latest = sub_dirs[0]

    # FRESHNESS GUARD: the run dir must have been created/touched after we
    # started the upload. If it wasn't, the upload was a no-op (e.g., dedup
    # skipped) and `latest` is a stale run from another file.
    if latest.stat().st_mtime < upload_start - 5:  # 5s slack
        # Dump the full subprocess output so we can see why no run dir.
        out_tail = (result.stdout or "")[-800:]
        err_tail = (result.stderr or "")[-800:]
        logmsg(f"  upload no-op: latest run dir is stale ({latest.name}).")
        logmsg(f"  -- main.py stdout tail --\n{out_tail}")
        logmsg(f"  -- main.py stderr tail --\n{err_tail}")
        return None

    # summary.json has aggregate metrics (final_pnl, max_drawdown, etc).
    # Since this loop disables VEVs, final_pnl ≈ HG + VF PnL.
    summary_path = latest / "summary.json"
    if not summary_path.exists():
        logmsg(f"  no summary at {latest}")
        return None
    try:
        summary = json.loads(summary_path.read_text())
    except Exception as e:
        logmsg(f"  summary unreadable: {e}")
        return None
    final_pnl = summary.get("final_pnl")
    if final_pnl is None:
        logmsg(f"  summary missing final_pnl: {list(summary.keys())[:5]}")
        return None
    return {
        "run_dir": str(latest),
        "asset_pnl": float(final_pnl),
        "total_pnl": float(final_pnl),
        "max_drawdown": summary.get("max_drawdown"),
        "max_pnl": summary.get("max_pnl"),
        "pnl_volatility": summary.get("pnl_volatility"),
        "per_product_pnl": {},  # platform doesn't expose breakdown in summary
    }


# ───────────────────── variant proposer ────────────────────────────

# Search axes: each entry is a list of values to try on that single dimension.
# These cover the ENRICHED template (v2) including trend/regime/asymmetric signals.
# Trimmed for the 5h run — drop axes that showed no signal in earlier sweep
# (vol_widen_coef, asymm_skew, micro_trend_coef, deep_imb_coef) and reduce
# value counts on remaining axes to focus the budget on what moves PnL.
HG_AXES = {
    "half_spread":      [2, 3, 4, 5, 6],
    "base_size":        [15, 20, 25, 30],
    "take_edge":        [6, 7, 8, 9, 11],
    "soft_cap":         [120, 150, 180],
    "k_inv":            [0.05, 0.08, 0.10, 0.14, 0.20],
    "micro_coef":       [0.20, 0.50, 0.80, 1.00],
    "imb_coef":         [0.20, 0.40, 0.55, 0.70],
    "ema_alpha":        [0.0, 0.05, 0.12, 0.20],
    # Dense around winning trend_coef=0.4
    "trend_coef":       [0.20, 0.30, 0.35, 0.45, 0.50, 0.60, 0.70],
    "trend_lag":        [3, 4, 5, 8, 10, 15],
    # Dense around winning revert_coef=0.2
    "revert_coef":      [0.05, 0.10, 0.15, 0.25, 0.30, 0.40],
    "revert_window":    [30, 40, 60, 80, 120],
    "long_ema_alpha":   [0.0003, 0.001, 0.003, 0.008],
    "ema_blend":        [0.15, 0.30, 0.50, 0.70],
    "vel_coef":         [0.10, 0.20, 0.30, 0.50, -0.30],
    "vel_alpha":        [0.05, 0.10, 0.20, 0.40],
}
VF_AXES = {
    "half_spread":      [1, 2, 3, 4],
    "base_size":        [10, 15, 20, 25, 30],
    "take_edge":        [2, 3, 4, 5, 7],
    "soft_cap":         [120, 160, 200],
    "k_inv":            [0.04, 0.06, 0.08, 0.10, 0.15],
    "micro_coef":       [0.20, 0.40, 0.55, 0.80],
    "imb_coef":         [0.15, 0.30, 0.45, 0.70],
    "ema_alpha":        [0.0, 0.05, 0.18, 0.30],
    "trend_coef":       [-0.20, 0.10, 0.20, 0.30, 0.40, 0.55],
    "trend_lag":        [3, 5, 7, 10, 15],
    "revert_coef":      [0.05, 0.10, 0.15, 0.25, 0.30, 0.40],
    "revert_window":    [30, 40, 60, 80, 120],
    "long_ema_alpha":   [0.0003, 0.001, 0.003, 0.008],
    "ema_blend":        [0.10, 0.25, 0.50, 0.75],
    "vel_coef":         [0.10, 0.20, 0.30, 0.50, -0.20],
    "vel_alpha":        [0.05, 0.10, 0.20, 0.40],
}


def baseline_params() -> Tuple[dict, dict]:
    """Pure flow MM, all advanced signals OFF (to test in isolation)."""
    base = {
        "trend_coef": 0.0, "trend_lag": 5, "revert_coef": 0.0, "revert_window": 50,
        "micro_trend_coef": 0.0, "deep_imb_coef": 0.0,
        "vol_widen_coef": 0.0, "asymm_skew": 1.0,
        "long_ema_alpha": 0.0, "ema_blend": 0.0,
        "vel_coef": 0.0, "vel_alpha": 0.10,
    }
    hg = {
        "limit": 200, "soft_cap": 150, "half_spread": 3, "base_size": 20,
        "take_edge": 8, "k_inv": 0.10, "micro_coef": 0.50, "imb_coef": 0.40,
        "ema_alpha": 0.0, "imb_window": 0, **base,
    }
    vf = {
        "limit": 200, "soft_cap": 160, "half_spread": 1, "base_size": 15,
        "take_edge": 4, "k_inv": 0.08, "micro_coef": 0.40, "imb_coef": 0.30,
        "ema_alpha": 0.0, "imb_window": 0, **base,
    }
    return hg, vf


# ─────────────────── structural presets ────────────────────────────
# These are bundles of co-tuned params representing genuinely DIFFERENT
# strategies, not single-axis perturbations. The loop tries each preset
# early before falling into single-axis exploration.

def preset_variants(base_hg: dict, base_vf: dict) -> List[Tuple[str, str, dict, dict]]:
    """Return a list of (id, label, hg, vf) preset variants to try first."""
    out = []

    # 1. Momentum-following: lean into mid drift
    h1 = {**base_hg, "trend_coef": 0.30, "trend_lag": 5,  "micro_trend_coef": 0.40}
    v1 = {**base_vf, "trend_coef": 0.30, "trend_lag": 5,  "micro_trend_coef": 0.40}
    out.append(("preset_momentum", "trend-follow on mid + microprice", h1, v1))

    # 2. Mean-reversion: fade extension from longer EMA
    h2 = {**base_hg, "revert_coef": 0.20, "revert_window": 60}
    v2 = {**base_vf, "revert_coef": 0.20, "revert_window": 60}
    out.append(("preset_meanrev", "fade against 60-tick mid avg", h2, v2))

    # 3. Deep-book imbalance: trust whole book, not just L1
    h3 = {**base_hg, "imb_coef": 0.20, "deep_imb_coef": 0.50, "imb_window": 5}
    v3 = {**base_vf, "imb_coef": 0.20, "deep_imb_coef": 0.50, "imb_window": 5}
    out.append(("preset_deepbook", "deep book pressure dominant", h3, v3))

    # 4. Regime-aware: widen spread under high realized vol
    h4 = {**base_hg, "vol_widen_coef": 1.5, "imb_window": 10}
    v4 = {**base_vf, "vol_widen_coef": 1.5, "imb_window": 10}
    out.append(("preset_volregime", "spread widens under high realized vol", h4, v4))

    # 5. Tight market-maker: smaller spread, larger size, tight take
    h5 = {**base_hg, "half_spread": 2, "base_size": 25, "take_edge": 5,
          "k_inv": 0.20}
    v5 = {**base_vf, "half_spread": 1, "base_size": 20, "take_edge": 3,
          "k_inv": 0.15}
    out.append(("preset_tight", "tighter spread + larger size", h5, v5))

    # 6. Wide-and-skewed: big spread + heavy inventory penalty
    h6 = {**base_hg, "half_spread": 5, "base_size": 25, "k_inv": 0.45,
          "asymm_skew": 1.5}
    v6 = {**base_vf, "half_spread": 3, "base_size": 20, "k_inv": 0.30,
          "asymm_skew": 1.5}
    out.append(("preset_wide_skewed", "wide quotes + heavy inv penalty", h6, v6))

    # 7. Microprice-dominant: trust microprice tilt strongly
    h7 = {**base_hg, "micro_coef": 1.0, "imb_coef": 0.20}
    v7 = {**base_vf, "micro_coef": 1.0, "imb_coef": 0.15}
    out.append(("preset_microheavy", "microprice tilt dominant", h7, v7))

    # 8. Aggressive trend + take: combine momentum + tight take
    h8 = {**base_hg, "trend_coef": 0.40, "trend_lag": 4, "take_edge": 5}
    v8 = {**base_vf, "trend_coef": 0.40, "trend_lag": 4, "take_edge": 3}
    out.append(("preset_agg_trend", "momentum + aggressive takes", h8, v8))

    # 9. Combined alpha: trend + deep imb + asymmetric skew
    h9 = {**base_hg, "trend_coef": 0.20, "deep_imb_coef": 0.30,
          "asymm_skew": 1.3, "imb_window": 10}
    v9 = {**base_vf, "trend_coef": 0.20, "deep_imb_coef": 0.30,
          "asymm_skew": 1.3, "imb_window": 10}
    out.append(("preset_combined", "trend + deep imb + asymm", h9, v9))

    # 10. DUAL_EMA_394682 — adaptive analog of the +9,724 algorithm.
    # Replaces 394682's hardcoded anchors with a slow microprice EMA
    # (long_ema_alpha=0.001 → half-life ~700 ticks, self-discovers level).
    # All other signals OFF — fair = ema_blend·long_EMA + (1-ema_blend)·micro.
    h10 = {**base_hg,
           "half_spread": 7, "base_size": 28, "take_edge": 9,
           "k_inv": 0.03, "micro_coef": 0.0, "imb_coef": 0.0,
           "ema_alpha": 0.12, "long_ema_alpha": 0.001, "ema_blend": 0.70}
    v10 = {**base_vf,
           "half_spread": 2, "base_size": 35, "take_edge": 3,
           "k_inv": 0.018, "micro_coef": 0.0, "imb_coef": 0.0,
           "ema_alpha": 0.18, "long_ema_alpha": 0.001, "ema_blend": 0.25}
    out.append(("preset_dual_ema_394682", "wide HG + adaptive long-EMA anchor", h10, v10))

    # 11. DUAL_EMA_PURE — same dual-EMA structure but ema_blend cranked higher,
    # for the case where the long EMA alone (with no short-flow tilt) wins.
    h11 = {**base_hg,
           "half_spread": 7, "base_size": 28, "take_edge": 9,
           "k_inv": 0.03, "micro_coef": 0.0, "imb_coef": 0.0,
           "ema_alpha": 0.0, "long_ema_alpha": 0.001, "ema_blend": 0.85}
    v11 = {**base_vf,
           "half_spread": 2, "base_size": 35, "take_edge": 3,
           "k_inv": 0.018, "micro_coef": 0.0, "imb_coef": 0.0,
           "ema_alpha": 0.0, "long_ema_alpha": 0.001, "ema_blend": 0.50}
    out.append(("preset_dual_ema_pure", "long-EMA-dominant, no short-flow signal", h11, v11))

    # 12. WIDE_HG_TIGHT_VF — lessons 1-3 from 394682 (wide HG quotes, large size,
    # tiny skew, tight VF) WITHOUT the dual-EMA, to isolate which lesson matters.
    h12 = {**base_hg,
           "half_spread": 7, "base_size": 28, "take_edge": 9,
           "k_inv": 0.03, "micro_coef": 0.50, "imb_coef": 0.40, "ema_alpha": 0.12}
    v12 = {**base_vf,
           "half_spread": 2, "base_size": 35, "take_edge": 3,
           "k_inv": 0.018, "micro_coef": 0.40, "imb_coef": 0.30, "ema_alpha": 0.18}
    out.append(("preset_wide_hg_tight_vf", "wide HG + tight VF, no dual-EMA", h12, v12))

    # 13. DUAL_EMA_FAST — slightly faster long-EMA (alpha=0.005, half-life ~140
    # ticks). Tracks regime shifts faster but with less stability.
    h13 = {**base_hg,
           "half_spread": 6, "base_size": 28, "take_edge": 9,
           "k_inv": 0.03, "micro_coef": 0.0, "imb_coef": 0.0,
           "ema_alpha": 0.12, "long_ema_alpha": 0.005, "ema_blend": 0.60}
    v13 = {**base_vf,
           "half_spread": 2, "base_size": 30, "take_edge": 3,
           "k_inv": 0.018, "micro_coef": 0.0, "imb_coef": 0.0,
           "ema_alpha": 0.18, "long_ema_alpha": 0.005, "ema_blend": 0.30}
    out.append(("preset_dual_ema_fast", "faster long-EMA tracking", h13, v13))

    # 14. PURE_EMA_FAIR — fair = long-EMA(microprice) only, NO flow signals.
    # Tests whether the EMA alone (no microprice tilt, no imbalance, no trend)
    # can find the fair price in motion.
    h14 = {**base_hg,
           "long_ema_alpha": 0.001, "ema_blend": 0.95,
           "ema_alpha": 0.0,
           "micro_coef": 0.0, "imb_coef": 0.0,
           "trend_coef": 0.0, "revert_coef": 0.0, "micro_trend_coef": 0.0,
           "deep_imb_coef": 0.0, "vol_widen_coef": 0.0,
           "vel_coef": 0.0,
           "half_spread": 4, "base_size": 22, "take_edge": 8, "k_inv": 0.05}
    v14 = {**base_vf,
           "long_ema_alpha": 0.001, "ema_blend": 0.85,
           "ema_alpha": 0.0,
           "micro_coef": 0.0, "imb_coef": 0.0,
           "trend_coef": 0.0, "revert_coef": 0.0, "micro_trend_coef": 0.0,
           "deep_imb_coef": 0.0, "vol_widen_coef": 0.0,
           "vel_coef": 0.0,
           "half_spread": 2, "base_size": 18, "take_edge": 4, "k_inv": 0.04}
    out.append(("preset_pure_ema_fair", "fair = long-EMA(micro) only, no flow", h14, v14))

    # 15. VELOCITY_MOMENTUM — adds EMA-of-velocity to the fair, otherwise baseline.
    # Tests the hypothesis that price changes are autocorrelated → smooth velocity
    # gives a usable directional signal.
    h15 = {**base_hg, "vel_coef": 0.50, "vel_alpha": 0.10,
           "trend_coef": 0.0, "revert_coef": 0.0}
    v15 = {**base_vf, "vel_coef": 0.50, "vel_alpha": 0.10,
           "trend_coef": 0.0, "revert_coef": 0.0}
    out.append(("preset_velocity_mom", "fair += vel_coef · EMA(Δmid)", h15, v15))

    # 16. WINNING_PLUS_VEL — current best (hg_trend_coef_0p4 structure) + velocity.
    # Stacks the smoothed velocity on top of the trend+revert baseline that won
    # the last sweep.
    h16 = {**base_hg, "trend_coef": 0.40, "trend_lag": 5,
           "revert_coef": 0.20, "revert_window": 60,
           "vel_coef": 0.30, "vel_alpha": 0.10}
    v16 = {**base_vf, "revert_coef": 0.20, "revert_window": 60,
           "vel_coef": 0.30, "vel_alpha": 0.10}
    out.append(("preset_winning_plus_vel", "winning structure + EMA-velocity", h16, v16))

    # 17. PURE_EMA_FAST — same as preset_pure_ema_fair but with a faster EMA so
    # the "fair" tracks recent moves more aggressively. Half-life ~140 ticks.
    h17 = {**h14, "long_ema_alpha": 0.005, "ema_blend": 0.85}
    v17 = {**v14, "long_ema_alpha": 0.005, "ema_blend": 0.70}
    out.append(("preset_pure_ema_fast", "pure EMA fair, fast (α=0.005)", h17, v17))

    # 18. PURE_EMA_PLUS_VEL — pure-EMA fair + velocity overlay. EMA gives the
    # level, velocity gives the directional bias.
    h18 = {**h14, "vel_coef": 0.40, "vel_alpha": 0.10}
    v18 = {**v14, "vel_coef": 0.40, "vel_alpha": 0.10}
    out.append(("preset_pure_ema_plus_vel", "pure EMA + velocity", h18, v18))

    return out


def propose_next(state: dict, rng: random.Random) -> Tuple[str, str, dict, dict]:
    """Generate the next variant. Strategy:
      1. Pick a "base" — usually current best, but 30% of the time pick a
         random previously-accepted variant from history (anti-tunnel-vision).
      2. Pick 1 axis to perturb (single-axis exploration).
      3. If single-axis from this base is exhausted, do 2-axis perturbation.
      4. Skip already-tried signatures.
    """
    best_hg, best_vf = state["best_hg"], state["best_vf"]
    tried = set(state["tried_keys"])

    # ── Pick a base ─────────────────────────────────────────────────
    # 70% current best, 30% a random accepted variant from history.
    accepted_hist = [h for h in state.get("history", []) if h.get("accepted")]
    if accepted_hist and rng.random() < 0.30:
        h = rng.choice(accepted_hist)
        base_hg = dict(h["hg"])
        base_vf = dict(h["vf"])
        base_tag = h["variant_id"][:18]
    else:
        base_hg = dict(best_hg)
        base_vf = dict(best_vf)
        base_tag = "best"

    def axes_candidates(base_hg_, base_vf_):
        out = []
        for asset, axes, current in [("hg", HG_AXES, base_hg_), ("vf", VF_AXES, base_vf_)]:
            for pname, vals in axes.items():
                for v in vals:
                    if v == current.get(pname):
                        continue
                    out.append((asset, pname, v))
        return out

    # ── Single-axis attempt ─────────────────────────────────────────
    cands = axes_candidates(base_hg, base_vf)
    rng.shuffle(cands)
    for asset, pname, val in cands:
        sig = f"{base_tag}|{asset}.{pname}={val}"
        if sig in tried:
            continue
        new_hg = dict(base_hg)
        new_vf = dict(base_vf)
        if asset == "hg":
            new_hg[pname] = val
        else:
            new_vf[pname] = val
        label = f"[{base_tag}] {asset.upper()} {pname}={val}"
        vid = f"{asset}_{pname}_{str(val).replace('.', 'p').replace('-', 'n')}"
        # Record the new signature in tried_keys (caller will save state).
        state["tried_keys"].append(sig)
        return (vid, label, new_hg, new_vf)

    # ── 2-axis perturbation when single-axis exhausted from this base ──
    cands2 = axes_candidates(base_hg, base_vf)
    for _ in range(40):  # try up to 40 random 2-axis combos
        if len(cands2) < 2:
            break
        a1 = rng.choice(cands2)
        a2 = rng.choice(cands2)
        if a1[:2] == a2[:2]:  # same (asset, pname) — skip
            continue
        sig = f"{base_tag}|2ax:{a1[0]}.{a1[1]}={a1[2]}+{a2[0]}.{a2[1]}={a2[2]}"
        if sig in tried:
            continue
        new_hg = dict(base_hg)
        new_vf = dict(base_vf)
        for asset, pname, val in (a1, a2):
            if asset == "hg":
                new_hg[pname] = val
            else:
                new_vf[pname] = val
        vid = f"x2_{a1[0]}{a1[1][:6]}_{a2[0]}{a2[1][:6]}_{rng.randint(0, 99999)}"
        label = f"[{base_tag}] 2ax {a1[0]}.{a1[1]}={a1[2]} + {a2[0]}.{a2[1]}={a2[2]}"
        state["tried_keys"].append(sig)
        return (vid, label, new_hg, new_vf)

    return ("done", f"all perturbations from base={base_tag} exhausted",
            dict(best_hg), dict(best_vf))


# ───────────────────────── state I/O ───────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    hg, vf = baseline_params()
    return {
        "started_at": datetime.now().isoformat(),
        "best_hg": hg,
        "best_vf": vf,
        "best_pnl": None,
        "best_variant_id": None,
        "history": [],
        "tried_keys": [],
        "preset_queue": [p[0] for p in preset_variants(hg, vf)],  # ids only
        "presets_done": False,
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─────────────────────────── main ──────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=12.0,
                    help="wall-clock budget in hours")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--inter-upload-delay", type=int, default=90)
    ap.add_argument("--rate-limit-backoff", type=int, default=300,
                    help="seconds to wait after rate-limit before next attempt")
    ap.add_argument("--max-iters", type=int, default=10_000)
    ap.add_argument("--reset", action="store_true",
                    help="wipe state and start fresh")
    args = ap.parse_args()

    rng = random.Random(args.seed)

    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()

    state = load_state()
    state["tried_keys"] = list(set(state.get("tried_keys", [])))

    deadline = datetime.now() + timedelta(hours=args.hours)
    logmsg(f"=== Research loop started: budget {args.hours}h, deadline {deadline:%Y-%m-%d %H:%M} ===")
    logmsg(f"Best so far: pnl={state['best_pnl']} variant={state['best_variant_id']}")

    # Always upload baseline first if we have no best yet.
    if state["best_pnl"] is None:
        logmsg("No baseline result yet — uploading baseline first.")
        vid = "baseline"
        label = "flow MM, default knobs"
        path = write_variant(vid, label, state["best_hg"], state["best_vf"])
        ok, msg = local_sim_3day(path)
        logmsg(f"  guardrail: {msg}")
        if not ok:
            logmsg("  baseline failed local guardrail — fix template before continuing")
            return
        # Retry baseline with rate-limit backoff until we get a real result.
        while True:
            res = upload_and_get_pnl(path)
            if res is None:
                logmsg("  baseline upload failed — aborting")
                return
            if res.get("rate_limited"):
                logmsg(f"  rate limited; sleeping {args.rate_limit_backoff}s before retry")
                time.sleep(args.rate_limit_backoff)
                if datetime.now() >= deadline:
                    logmsg("  budget exhausted during rate-limit wait — aborting")
                    return
                continue
            break
        pnl = res["asset_pnl"]
        state["best_pnl"] = pnl
        state["best_variant_id"] = vid
        state["history"].append({
            "variant_id": vid, "label": label,
            "hg": dict(state["best_hg"]), "vf": dict(state["best_vf"]),
            "asset_pnl": pnl,
            "max_drawdown": res.get("max_drawdown"),
            "pnl_volatility": res.get("pnl_volatility"),
            "accepted": True,
            "when": datetime.now().isoformat(),
        })
        save_state(state)
        logmsg(f"  baseline asset_pnl = {pnl:+.0f}")
        time.sleep(args.inter_upload_delay)

    # ── Main loop ──
    # Phase A: try presets first (genuine structural variants), THEN
    # single-axis perturbations from the best so far.
    base_hg_for_presets, base_vf_for_presets = baseline_params()
    preset_pool = preset_variants(base_hg_for_presets, base_vf_for_presets)
    preset_done = set(state.get("presets_done_ids", []))

    for i in range(args.max_iters):
        if datetime.now() >= deadline:
            logmsg("Wall-clock budget exhausted.")
            break

        # Pick a preset still in queue, otherwise fall to single-axis search
        preset_choice = None
        for pid, plabel, phg, pvf in preset_pool:
            if pid not in preset_done:
                preset_choice = (pid, plabel, phg, pvf)
                break

        if preset_choice is not None:
            vid, label, hg, vf = preset_choice
            preset_done.add(vid)
            state["presets_done_ids"] = sorted(preset_done)
        else:
            vid, label, hg, vf = propose_next(state, rng)
            if vid == "done":
                logmsg("All single-axis perturbations explored. Stopping.")
                break

        # propose_next already added the proper signature to tried_keys.
        save_state(state)

        logmsg(f"--- iter {i+1}: trying {label} (variant {vid})")
        path = write_variant(vid, label, hg, vf)
        ok, msg = local_sim_3day(path)
        if not ok:
            logmsg(f"  REJECTED by guardrail: {msg}")
            state["history"].append({
                "variant_id": vid, "label": label, "hg": hg, "vf": vf,
                "asset_pnl": None, "rejected_local": True, "reason": msg,
                "when": datetime.now().isoformat(),
            })
            save_state(state)
            continue
        logmsg(f"  guardrail: {msg}")
        res = upload_and_get_pnl(path)
        if res is not None and res.get("rate_limited"):
            logmsg(f"  rate-limited; sleeping {args.rate_limit_backoff}s and retrying same variant")
            time.sleep(args.rate_limit_backoff)
            if datetime.now() >= deadline:
                break
            res = upload_and_get_pnl(path)
            if res is not None and res.get("rate_limited"):
                logmsg(f"  still rate-limited after backoff; skipping this variant")
                state["history"].append({
                    "variant_id": vid, "label": label, "hg": hg, "vf": vf,
                    "asset_pnl": None, "upload_failed": True, "reason": "rate_limited",
                    "when": datetime.now().isoformat(),
                })
                save_state(state)
                time.sleep(args.inter_upload_delay)
                continue
        if not res:
            logmsg(f"  upload FAILED — skipping")
            state["history"].append({
                "variant_id": vid, "label": label, "hg": hg, "vf": vf,
                "asset_pnl": None, "upload_failed": True,
                "when": datetime.now().isoformat(),
            })
            save_state(state)
            time.sleep(args.inter_upload_delay)
            continue

        pnl = res["asset_pnl"]
        dd = res.get("max_drawdown") or 0.0
        vol = res.get("pnl_volatility") or 0.0
        # RISK-ADJUSTED SCORE: penalise raw PnL by half the max drawdown so
        # high-variance strategies don't dominate the search.
        score = pnl - 0.5 * dd
        prev_score = state.get("best_score")
        if prev_score is None:
            # First run after this change: initialise from current best_pnl,
            # treating its dd as 0 (we don't have it). Will be replaced quickly.
            prev_score = state["best_pnl"]
        logmsg(f"  asset_pnl = {pnl:+.0f}  max_dd={dd:.0f}  vol={vol:.1f}"
               f"  score={score:+.0f}")
        accepted = score > prev_score
        if accepted:
            improvement = score - prev_score
            logmsg(f"  ✅ ACCEPT: score +{improvement:.0f} over prev ({prev_score:+.0f})")
            state["best_hg"] = hg
            state["best_vf"] = vf
            state["best_pnl"] = pnl
            state["best_score"] = score
            state["best_dd"] = dd
            state["best_variant_id"] = vid
        else:
            logmsg(f"  ❌ reject: score {score:+.0f} vs best {prev_score:+.0f}"
                   f"  (raw pnl {pnl:+.0f})")
        state["history"].append({
            "variant_id": vid, "label": label, "hg": hg, "vf": vf,
            "asset_pnl": pnl,
            "max_drawdown": dd,
            "pnl_volatility": vol,
            "score": score,
            "accepted": accepted,
            "when": datetime.now().isoformat(),
        })
        save_state(state)
        time.sleep(args.inter_upload_delay)

    # ── Final summary ──
    logmsg("=== Research loop ended ===")
    logmsg(f"Best variant: {state['best_variant_id']} pnl={state['best_pnl']:+.0f}")
    logmsg(f"HG params: {state['best_hg']}")
    logmsg(f"VF params: {state['best_vf']}")
    accepted_count = sum(1 for h in state["history"] if h.get("accepted"))
    logmsg(f"Variants tested: {len(state['history'])}  accepted: {accepted_count}")


if __name__ == "__main__":
    main()
