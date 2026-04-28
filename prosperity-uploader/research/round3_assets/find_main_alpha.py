#!/usr/bin/env python3
"""
Find main alpha signal — sensitivity analysis.

For each alpha knob, run a "turn it off" probe (set to 0) and measure
both LOCAL (round 4 CSV via teameastbt) and PLATFORM (upload) PnL.
The knob whose turn-off causes the biggest drop is the dominant signal.

Probes (each = baseline with ONE alpha knob set to 0):
  baseline       — slim engine, hg_tight_v1 alpha values
  no_trend       — trend_coef → 0
  no_vel         — vel_coef → 0
  no_micro       — micro_coef → 0
  no_micro_trend — micro_trend_coef → 0
  no_imb         — imb_coef → 0
  no_ema         — ema_alpha → 0

Run:
    python3 find_main_alpha.py
    python3 find_main_alpha.py --no-upload   # local-only fast sweep
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

import research_loop as rl

HERE = Path(__file__).parent
TEAMEAST = Path("/Users/jim/Desktop/team-east-backtester")
RESULTS = HERE / "find_main_alpha_results.json"

PROFIT_RE = re.compile(r"Round 4 day (\d+):\s+([+-]?[\d,]+)")
TOTAL_RE = re.compile(r"Total profit:\s+([+-]?[\d,]+)")
DD_RE = re.compile(r"max_drawdown_abs:\s+([+-]?[\d,]+)")


def baseline_params() -> tuple[dict, dict]:
    """Slim baseline = current best (hg_tight_v1) alpha values."""
    hg = {
        "limit": 200, "soft_cap": 150, "half_spread": 4,
        "base_size": 20, "take_edge": 8, "k_inv": 1.0,
        "micro_coef": 0.50, "imb_coef": 0.40, "ema_alpha": 0.05,
        "trend_coef": 0.30, "trend_lag": 5,
        "micro_trend_coef": 0.10, "vel_coef": 0.20, "vel_alpha": 0.10,
        "stop_pos_threshold": 100, "stop_drawdown_threshold": 4000.0,
        "stop_min_ticks_at_pos": 200, "stop_unwind_size": 30,
    }
    vf = {
        "limit": 200, "soft_cap": 150, "half_spread": 1,
        "base_size": 15, "take_edge": 2, "k_inv": 0.7,
        "micro_coef": 0.20, "imb_coef": 0.30, "ema_alpha": 0.0,
        "trend_coef": 0.15, "trend_lag": 5,
        "micro_trend_coef": 0.0, "vel_coef": 0.20, "vel_alpha": 0.10,
        "stop_pos_threshold": 100, "stop_drawdown_threshold": 4000.0,
        "stop_min_ticks_at_pos": 200, "stop_unwind_size": 30,
    }
    return hg, vf


def variants() -> list[tuple[str, str, dict, dict]]:
    out = []
    bh, bv = baseline_params()

    out.append(("baseline_slim", "slim baseline (=hg_tight_v1 alpha)", bh, bv))

    # Turn off each alpha knob on BOTH HG and VFE (whichever has it active)
    for axis in ["trend_coef", "vel_coef", "micro_coef", "micro_trend_coef",
                 "imb_coef", "ema_alpha"]:
        h = dict(bh); h[axis] = 0.0
        v = dict(bv); v[axis] = 0.0
        out.append((f"no_{axis}", f"baseline with {axis} = 0 (turned OFF)", h, v))

    return out


def run_local(variant_path: Path, days: str = "4") -> dict:
    cmd = [
        "python3", "-m", "teameastbt", str(variant_path),
        days, "--merge-pnl", "--no-out", "--match-trades", "all",
    ]
    res = subprocess.run(cmd, cwd=str(TEAMEAST), capture_output=True,
                          text=True, timeout=120)
    if res.returncode != 0:
        return {"error": res.stderr[-300:]}
    out = res.stdout
    days_pnl = {int(m.group(1)): int(m.group(2).replace(",", ""))
                for m in PROFIT_RE.finditer(out)}
    totals = list(TOTAL_RE.finditer(out))
    total = int(totals[-1].group(1).replace(",", "")) if totals else sum(days_pnl.values())
    dd_m = DD_RE.search(out)
    return {
        "days_pnl": days_pnl,
        "total": total,
        "max_dd": int(dd_m.group(1).replace(",", "")) if dd_m else 0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-upload", action="store_true",
                    help="local sim only, skip platform upload")
    ap.add_argument("--inter-upload-delay", type=int, default=15)
    args = ap.parse_args()

    probes = variants()
    print(f"=== find_main_alpha — {len(probes)} probes ===\n")

    results = {"started_at": datetime.now().isoformat(), "results": []}

    # PHASE 1 — local sim sweep (fast)
    print("--- PHASE 1: local backtest (round 4 CSV) ---")
    print(f"{'variant':<22} {'local total':>11} {'d1':>6} {'d2':>6} {'d3':>6} {'dd':>6}")
    local_results = {}
    for vid, label, hg, vf in probes:
        path = rl.write_variant(vid, label, hg, vf)
        r = run_local(path)
        if r.get("error"):
            print(f"{vid:<22}  ERROR: {r['error'][:80]}")
            continue
        d1 = r["days_pnl"].get(1, 0)
        d2 = r["days_pnl"].get(2, 0)
        d3 = r["days_pnl"].get(3, 0)
        local_results[vid] = {"total": r["total"], "d1": d1, "d2": d2, "d3": d3, "dd": r["max_dd"]}
        print(f"{vid:<22} {r['total']:>+11} {d1:>+6} {d2:>+6} {d3:>+6} {r['max_dd']:>6}")

    # Local diff vs baseline
    bl = local_results.get("baseline_slim")
    if bl:
        print(f"\n--- Local Δ vs baseline ({bl['total']:+}) ---")
        sorted_vids = sorted(
            (vid for vid in local_results if vid != "baseline_slim"),
            key=lambda v: local_results[v]["total"]
        )
        for vid in sorted_vids:
            r = local_results[vid]
            d_total = r["total"] - bl["total"]
            tag = "✓ kept signal!" if d_total < -100 else ("✗ removable" if d_total > 100 else "· flat")
            print(f"  {vid:<22} Δtotal={d_total:>+6}   [{tag}]")

    # PHASE 2 — platform uploads
    if args.no_upload:
        print("\n--no-upload set; skipping platform phase")
        # Save partial results
        for vid, label, hg, vf in probes:
            r = local_results.get(vid, {})
            results["results"].append({
                "variant_id": vid, "label": label, "hg": hg, "vf": vf,
                "local": r,
            })
        RESULTS.write_text(json.dumps(results, indent=2))
        return

    print(f"\n--- PHASE 2: platform uploads ({len(probes)} probes) ---")
    for i, (vid, label, hg, vf) in enumerate(probes, 1):
        print(f"\n[{i}/{len(probes)}] {vid}: {label}")
        path = rl.write_variant(vid, label, hg, vf)
        # local guardrail (round 3 sim, just for crash check)
        ok, msg = rl.local_sim_3day(path)
        if not ok:
            print(f"  guardrail FAIL: {msg}")
            continue

        while True:
            res = rl.upload_and_get_pnl(path)
            if res is None: print("  upload FAILED"); break
            if res.get("rate_limited"):
                print("  rate limited; sleeping 60s"); time.sleep(60); continue
            pnl = res["asset_pnl"]; dd = res.get("max_drawdown") or 0
            score = pnl - 0.5 * dd
            print(f"  PLATFORM: pnl={pnl:+.0f}  dd={dd:.0f}  score={score:+.0f}")
            results["results"].append({
                "variant_id": vid, "label": label, "hg": hg, "vf": vf,
                "local": local_results.get(vid),
                "platform": {"pnl": pnl, "dd": dd, "score": score,
                             "run_dir": res.get("run_dir")},
                "when": datetime.now().isoformat(),
            })
            RESULTS.write_text(json.dumps(results, indent=2))
            break

        if i < len(probes):
            time.sleep(args.inter_upload_delay)

    # Final comparison
    print("\n=== FINAL: local vs platform ===")
    print(f"{'variant':<22} {'local':>7} {'platform':>9}  Δlocal  Δplatform")
    bl_local = local_results.get("baseline_slim", {}).get("total", 0)
    bl_plat = next((r["platform"]["pnl"] for r in results["results"]
                    if r["variant_id"] == "baseline_slim" and r.get("platform")), 0)
    for r in results["results"]:
        loc = r.get("local", {}).get("total", "?")
        plat = r["platform"]["pnl"] if r.get("platform") else "?"
        d_loc = (loc - bl_local) if isinstance(loc, int) else "?"
        d_plat = (plat - bl_plat) if isinstance(plat, int) else "?"
        print(f"{r['variant_id']:<22} {str(loc):>7} {str(plat):>9}  {str(d_loc):>+6}  {str(d_plat):>+9}")


if __name__ == "__main__":
    main()
