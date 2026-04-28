#!/usr/bin/env python3
"""
Fast local sweep using teameastbt backtester on round 4 data.

Each variant ~3 sec per day × 3 days = ~10 sec. Way faster than 2-min platform
upload. Use this for breadth, then upload only the top-N to the platform.

Round 4 data is at `teameastbt/resources/round4/` (symlinked from
`strategies/round4/ROUND_4/`).

Run:
    python3 local_sweep.py
    python3 local_sweep.py --days 4-1     # single day
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
RESULTS = HERE / "local_sweep_results.json"


# ───── Define variants ─────

def base_params() -> tuple[dict, dict]:
    """Current best (hg_tight_v1) configuration."""
    state = json.loads((HERE / "state.json").read_text())
    hg, vf = dict(state["best_hg"]), dict(state["best_vf"])
    for d in (hg, vf):
        d.setdefault("revert_gate_threshold", 0.0)
        d.setdefault("stop_pos_threshold", 0)
        d.setdefault("stop_drawdown_threshold", 800.0)
        d.setdefault("stop_min_ticks_at_pos", 100)
        d.setdefault("stop_unwind_size", 20)
    # Apply hg_tight_v1 winning config
    hg["trend_coef"] = 0.30
    hg["micro_coef"] = 0.50
    hg["micro_trend_coef"] = 0.10
    hg["half_spread"] = 4
    hg["k_inv"] = 1.0
    vf["trend_coef"] = 0.15
    vf["revert_coef"] = 0.20
    vf["revert_window"] = 60
    vf["vel_coef"] = 0.20
    vf["deep_imb_coef"] = 0.10
    vf["half_spread"] = 1
    vf["take_edge"] = 2
    vf["k_inv"] = 0.7
    return hg, vf


def variants() -> list[tuple[str, str, dict, dict]]:
    """Single-axis perturbations off current best."""
    bh, bv = base_params()
    out = [("baseline_hg_tight", "current best (hg_tight_v1)", bh, bv)]

    # HG structural
    for hs in [3, 5]:
        h = dict(bh); h["half_spread"] = hs
        out.append((f"hg_hs_{hs}", f"HG half_spread {bh['half_spread']}→{hs}", h, bv))
    for te in [4, 6]:
        h = dict(bh); h["take_edge"] = te
        out.append((f"hg_te_{te}", f"HG take_edge {bh['take_edge']}→{te}", h, bv))

    # HG alpha
    for tc in [0.20, 0.45]:
        h = dict(bh); h["trend_coef"] = tc
        out.append((f"hg_tc_{str(tc).replace('.','p')}", f"HG trend_coef {bh['trend_coef']}→{tc}", h, bv))
    for vc in [0.0, 0.30]:
        h = dict(bh); h["vel_coef"] = vc
        out.append((f"hg_vc_{str(vc).replace('.','p')}", f"HG vel_coef {bh['vel_coef']}→{vc}", h, bv))

    # VFE structural
    for hs in [0, 2]:
        v = dict(bv); v["half_spread"] = hs
        out.append((f"vf_hs_{hs}", f"VF half_spread {bv['half_spread']}→{hs}", bh, v))
    for te in [1, 3]:
        v = dict(bv); v["take_edge"] = te
        out.append((f"vf_te_{te}", f"VF take_edge {bv['take_edge']}→{te}", bh, v))

    # VFE alpha
    for tc in [0.0, 0.30, 0.45]:
        v = dict(bv); v["trend_coef"] = tc
        out.append((f"vf_tc_{str(tc).replace('.','p')}", f"VF trend_coef {bv['trend_coef']}→{tc}", bh, v))
    for rc in [0.0, 0.40]:
        v = dict(bv); v["revert_coef"] = rc
        out.append((f"vf_rc_{str(rc).replace('.','p')}", f"VF revert_coef {bv['revert_coef']}→{rc}", bh, v))

    return out


# ───── Backtester invocation ─────

PROFIT_RE = re.compile(r"Round 4 day (\d+):\s+([+-]?[\d,]+)")
TOTAL_RE = re.compile(r"Total profit:\s+([+-]?[\d,]+)")
DD_RE = re.compile(r"max_drawdown_abs:\s+([+-]?[\d,]+)")


def run_local(variant_path: Path, days: str = "4") -> dict:
    """Run teameastbt backtester. Return dict with day-by-day PnL + total + DD."""
    cmd = [
        "python3", "-m", "teameastbt", str(variant_path),
        days, "--merge-pnl", "--no-out", "--match-trades", "all",
    ]
    res = subprocess.run(
        cmd, cwd=str(TEAMEAST), capture_output=True, text=True, timeout=120,
    )
    if res.returncode != 0:
        return {"error": res.stderr[-300:]}

    out = res.stdout
    days_pnl = {}
    for m in PROFIT_RE.finditer(out):
        days_pnl[int(m.group(1))] = int(m.group(2).replace(",", ""))
    # The merge-pnl output has TWO "Total profit:" lines — last is the merged total
    totals = list(TOTAL_RE.finditer(out))
    total = int(totals[-1].group(1).replace(",", "")) if totals else sum(days_pnl.values())
    dd_m = DD_RE.search(out)
    return {
        "days_pnl": days_pnl,
        "total": total,
        "max_dd": int(dd_m.group(1).replace(",", "")) if dd_m else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", default="4", help="day spec (e.g., '4', '4-1', '4-2')")
    ap.add_argument("--filter", help="only test variants whose id contains this substring")
    args = ap.parse_args()

    probes = variants()
    if args.filter:
        probes = [p for p in probes if args.filter in p[0]]

    print(f"=== local_sweep — {len(probes)} variants on round {args.days} ===\n")
    print(f"{'variant':<25} {'pnl total':>9} {'d1':>6} {'d2':>6} {'d3':>6} {'max_dd':>7}  label")

    results = []
    for vid, label, hg, vf in probes:
        path = rl.write_variant(vid, label, hg, vf)
        t0 = time.time()
        r = run_local(path, days=args.days)
        elapsed = time.time() - t0
        if r.get("error"):
            print(f"{vid:<25}  ERROR ({elapsed:.1f}s): {r['error'][:80]}")
            continue
        d1 = r["days_pnl"].get(1, 0)
        d2 = r["days_pnl"].get(2, 0)
        d3 = r["days_pnl"].get(3, 0)
        total = r["total"] if r["total"] is not None else (d1 + d2 + d3)
        dd = r["max_dd"] or 0
        print(f"{vid:<25} {total:>+9} {d1:>+6} {d2:>+6} {d3:>+6} {dd:>7}  {label}")
        results.append({
            "variant_id": vid, "label": label,
            "total_pnl": total, "day1": d1, "day2": d2, "day3": d3,
            "max_dd": dd, "score": total - 0.5 * dd,
            "hg": hg, "vf": vf, "elapsed": elapsed,
        })

    # Save
    RESULTS.write_text(json.dumps({
        "started_at": datetime.now().isoformat(),
        "results": results,
    }, indent=2))

    # Top winners
    if results:
        baseline = next((r for r in results if r["variant_id"] == "baseline_hg_tight"), None)
        if baseline:
            print(f"\n=== sorted by Δscore vs baseline ({baseline['score']:+.0f}) ===")
            sorted_r = sorted(results, key=lambda r: -r["score"])
            for r in sorted_r:
                d_sc = r["score"] - baseline["score"]
                tag = "✓" if d_sc > 100 else ("✗" if d_sc < -100 else "·")
                print(f"  {tag} {r['variant_id']:<25} score={r['score']:>+8.0f}  Δ={d_sc:>+6.0f}  total={r['total_pnl']:>+6}")


if __name__ == "__main__":
    main()
