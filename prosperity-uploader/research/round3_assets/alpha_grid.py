#!/usr/bin/env python3
"""
Two-phase grid search for best alpha on r4_momentum_v1 base.

Phase 1: HG alpha grid (Cartesian product of ema_alpha × trend_coef × imb_coef × vel_coef)
Phase 2: VFE alpha grid (with best HG fixed)

Auto-runs both phases. Tracks best by score in alpha_grid_state.json.
Each upload ~2 min on platform; total ~40-60 min for ~20 probes.

Run:
    python3 alpha_grid.py
    python3 alpha_grid.py --phase 1   # HG only
"""

from __future__ import annotations
import argparse, itertools, json, time
from datetime import datetime
from pathlib import Path
import research_loop as rl

HERE = Path(__file__).parent
STATE = HERE / "alpha_grid_state.json"
RESULTS = HERE / "alpha_grid_results.json"

# ── Grids ──
HG_GRID = {
    "ema_alpha":  [0.05, 0.10, 0.20],
    "trend_coef": [0.30, 0.45],
    "imb_coef":   [0.0, 0.40],
    "vel_coef":   [0.20, 0.30],
}  # 3*2*2*2 = 24 combos

VF_GRID = {
    "ema_alpha":  [0.0, 0.05, 0.10],
    "trend_coef": [0.0, 0.15, 0.30],
    "imb_coef":   [0.0, 0.30],
}  # 3*3*2 = 18 combos


def base_hg():
    return {
        "limit": 200, "soft_cap": 150, "half_spread": 4,
        "base_size": 20, "quote_size": 20, "take_size": 20,
        "take_edge": 8, "k_inv": 1.0,
        "micro_coef": 0.50, "imb_coef": 0.40, "ema_alpha": 0.05,
        "trend_coef": 0.30, "trend_lag": 5,
        "vel_coef": 0.20, "vel_alpha": 0.10,
        "target_pos_scaler": 10.0, "alpha_threshold": 1.5,
        "alpha_take_size": 50, "alpha_quote_size": 40, "mm_size_reduced": 5,
        "k_relax_coef": 0.5, "pos_gap_threshold": 10,
        "alpha_scale": 0.0, "target_cap": 150,
        "size_step": 1_000_000_000,
        "take_size_big": 20, "take_trigger": 1_000_000_000,
        "conviction_norm": 1_000_000_000, "conviction_relief": 0.0, "k_inv_floor": 1.0,
        "one_sided_trigger": 1_000_000_000, "opposite_min_size": 20,
        "chop_threshold": 0.0, "chop_target_cap_scale": 1.0,
        "chop_take_scale": 1.0, "chop_relief_scale": 1.0,
        "diag_print_every": 0,
        "stop_pos_threshold": 100, "stop_drawdown_threshold": 4000.0,
        "stop_min_ticks_at_pos": 200, "stop_unwind_size": 30,
    }

def base_vf():
    return {
        "limit": 200, "soft_cap": 150, "half_spread": 1,
        "base_size": 15, "quote_size": 15, "take_size": 15,
        "take_edge": 2, "k_inv": 0.7,
        "micro_coef": 0.20, "imb_coef": 0.30, "ema_alpha": 0.0,
        "trend_coef": 0.15, "trend_lag": 5,
        "vel_coef": 0.20, "vel_alpha": 0.10,
        "target_pos_scaler": 10.0, "alpha_threshold": 1.0,
        "alpha_take_size": 40, "alpha_quote_size": 30, "mm_size_reduced": 3,
        "k_relax_coef": 0.5, "pos_gap_threshold": 10,
        "alpha_scale": 0.0, "target_cap": 150,
        "size_step": 1_000_000_000,
        "take_size_big": 15, "take_trigger": 1_000_000_000,
        "conviction_norm": 1_000_000_000, "conviction_relief": 0.0, "k_inv_floor": 1.0,
        "one_sided_trigger": 1_000_000_000, "opposite_min_size": 15,
        "chop_threshold": 0.0, "chop_target_cap_scale": 1.0,
        "chop_take_scale": 1.0, "chop_relief_scale": 1.0,
        "diag_print_every": 0,
        "stop_pos_threshold": 100, "stop_drawdown_threshold": 4000.0,
        "stop_min_ticks_at_pos": 200, "stop_unwind_size": 30,
    }


def grid_combos(grid):
    keys = list(grid)
    for vals in itertools.product(*(grid[k] for k in keys)):
        yield dict(zip(keys, vals))


def fmt_combo(c):
    return "_".join(f"{k[:2]}{str(v).replace('.','p').replace('-','n')}" for k, v in c.items())


def load_state():
    if STATE.exists(): return json.loads(STATE.read_text())
    return {
        "phase": 1,
        "best_hg_alpha": {"ema_alpha": 0.05, "trend_coef": 0.30,
                          "imb_coef": 0.40, "vel_coef": 0.20},
        "best_vf_alpha": {"ema_alpha": 0.0, "trend_coef": 0.15,
                          "imb_coef": 0.30},
        "best_score": None,
        "best_pnl": None,
        "best_dd": None,
        "best_variant_id": None,
        "tried": {},
    }

def save_state(s): STATE.write_text(json.dumps(s, indent=2))

def load_results():
    if RESULTS.exists(): return json.loads(RESULTS.read_text())
    return {"started_at": datetime.now().isoformat(), "results": {}}

def save_results(r): RESULTS.write_text(json.dumps(r, indent=2))


def upload_and_record(vid, label, hg, vf, results, state):
    rl.logmsg(f"\n  {vid}: {label}")
    path = rl.write_variant(vid, label, hg, vf)
    ok, msg = rl.local_sim_3day(path)
    if not ok:
        rl.logmsg(f"  guardrail FAIL: {msg}")
        results["results"][vid] = {"variant_id": vid, "rejected_local": True}
        save_results(results); return None
    while True:
        res = rl.upload_and_get_pnl(path)
        if res is None:
            rl.logmsg(f"  upload FAILED")
            results["results"][vid] = {"variant_id": vid, "upload_failed": True}
            save_results(results); return None
        if res.get("rate_limited"):
            rl.logmsg(f"  rate limited; sleeping 60s")
            time.sleep(60); continue
        pnl = res["asset_pnl"]; dd = res.get("max_drawdown") or 0
        score = pnl - 0.5 * dd
        delta = (score - state["best_score"]) if state["best_score"] is not None else 0
        verdict = "ACCEPT ✓" if state["best_score"] is None or score > state["best_score"] else f"reject (Δ={delta:+.0f})"
        rl.logmsg(f"  pnl={pnl:+.0f} dd={dd:.0f} score={score:+.0f}  [{verdict}]")
        results["results"][vid] = {
            "variant_id": vid, "label": label,
            "asset_pnl": pnl, "max_drawdown": dd, "score": score,
            "hg": hg, "vf": vf,
            "when": datetime.now().isoformat(),
        }
        save_results(results)
        if state["best_score"] is None or score > state["best_score"]:
            state["best_score"] = score
            state["best_pnl"] = pnl
            state["best_dd"] = dd
            state["best_variant_id"] = vid
            save_state(state)
        return score


def run_phase1(state, results, args):
    """HG alpha grid — VFE held at baseline."""
    rl.logmsg(f"\n=== PHASE 1: HG alpha grid ({3*2*2*2}=24 combos) ===")
    bv = base_vf()
    for c in grid_combos(HG_GRID):
        vid = f"g1_{fmt_combo(c)}"
        if vid in state["tried"]:
            rl.logmsg(f"  {vid} — already tried, skipping")
            continue
        bh = base_hg(); bh.update(c)
        score = upload_and_record(vid, f"HG grid {c}", bh, bv, results, state)
        state["tried"][vid] = score
        if score is not None:
            # Track which combo matched best_hg_alpha
            if state["best_score"] == score:
                state["best_hg_alpha"] = c
                save_state(state)
        time.sleep(args.inter_upload_delay)


def run_phase2(state, results, args):
    """VFE grid with best HG fixed."""
    rl.logmsg(f"\n=== PHASE 2: VFE alpha grid ({3*3*2}=18 combos), HG fixed at best ===")
    rl.logmsg(f"  best HG alpha: {state['best_hg_alpha']}")
    bh = base_hg(); bh.update(state["best_hg_alpha"])
    for c in grid_combos(VF_GRID):
        vid = f"g2_{fmt_combo(c)}"
        if vid in state["tried"]:
            rl.logmsg(f"  {vid} — already tried, skipping")
            continue
        bv = base_vf(); bv.update(c)
        score = upload_and_record(vid, f"VF grid {c}, HG={state['best_hg_alpha']}", bh, bv, results, state)
        state["tried"][vid] = score
        if score is not None:
            if state["best_score"] == score:
                state["best_vf_alpha"] = c
                save_state(state)
        time.sleep(args.inter_upload_delay)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", type=int, default=0, help="0=both, 1=HG only, 2=VF only")
    ap.add_argument("--inter-upload-delay", type=int, default=15)
    args = ap.parse_args()

    state = load_state()
    results = load_results()

    rl.logmsg(f"=== Alpha grid search starting ===")
    rl.logmsg(f"  current best: score={state.get('best_score')}  variant={state.get('best_variant_id')}")

    if args.phase in (0, 1):
        run_phase1(state, results, args)
    if args.phase in (0, 2):
        run_phase2(state, results, args)

    rl.logmsg(f"\n=== DONE ===")
    rl.logmsg(f"  best score: {state['best_score']:+.0f}")
    rl.logmsg(f"  best variant: {state['best_variant_id']}")
    rl.logmsg(f"  best HG alpha: {state['best_hg_alpha']}")
    rl.logmsg(f"  best VFE alpha: {state['best_vf_alpha']}")

    # Top 5 leaderboard
    rows = []
    for vid, r in results["results"].items():
        if r.get("score") is not None:
            rows.append((r["score"], vid, r.get("asset_pnl", 0), r.get("max_drawdown", 0)))
    rows.sort(reverse=True)
    rl.logmsg(f"\nTop 10 by score:")
    for sc, vid, p, dd in rows[:10]:
        rl.logmsg(f"  {vid:<40} score={sc:>+8.0f} pnl={p:>+7.0f} dd={dd:>5.0f}")


if __name__ == "__main__":
    main()
