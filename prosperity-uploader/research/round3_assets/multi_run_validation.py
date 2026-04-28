#!/usr/bin/env python3
"""
Multi-run validation — estimate platform variance per variant.

Runs each of 3 candidate HG configs 3 times. VFE held at baseline.
Reports mean / min / max / std per variant.

Hypothesis: round 4 platform is non-deterministic. We need to know
whether the grid winner's +1,308 score is signal or noise.

Each upload ~2 min. Total ~18 min for 9 uploads.
"""

from __future__ import annotations
import json, statistics, time
from datetime import datetime
from pathlib import Path
import research_loop as rl

HERE = Path(__file__).parent
RESULTS = HERE / "multi_run_validation.json"


def base_hg(em, tc, ic=0.0, vc=0.2):
    return {
        "limit": 200, "soft_cap": 150, "half_spread": 4,
        "base_size": 20, "quote_size": 20, "take_size": 20,
        "take_edge": 8, "k_inv": 1.0,
        "micro_coef": 0.50, "imb_coef": ic, "ema_alpha": em,
        "trend_coef": tc, "trend_lag": 5,
        "vel_coef": vc, "vel_alpha": 0.10,
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


VARIANTS = [
    ("A_winner",   "HG ema=0.20 trend=0.45 (grid winner)",  base_hg(0.20, 0.45)),
    ("B_quote",    "HG ema=0.05 trend=0.45 (round_quote)",  base_hg(0.05, 0.45)),
    ("C_baseline", "HG ema=0.05 trend=0.30 (slim baseline)", base_hg(0.05, 0.30)),
]
N_REPS = 3


def main():
    bv = base_vf()
    if RESULTS.exists():
        all_data = json.loads(RESULTS.read_text())
    else:
        all_data = {"started_at": datetime.now().isoformat(), "runs": []}

    rl.logmsg(f"=== Multi-run validation: 3 variants × {N_REPS} reps ===\n")

    for vid, label, hg in VARIANTS:
        rl.logmsg(f"\n--- {vid} : {label} ---")
        for rep in range(1, N_REPS + 1):
            run_id = f"{vid}_rep{rep}"
            if any(r["run_id"] == run_id for r in all_data["runs"]):
                rl.logmsg(f"  rep{rep} — already done, skipping")
                continue
            path = rl.write_variant(run_id, f"{label} rep{rep}", hg, bv)
            ok, msg = rl.local_sim_3day(path)
            if not ok:
                rl.logmsg(f"  rep{rep} guardrail FAIL: {msg}")
                continue
            while True:
                res = rl.upload_and_get_pnl(path)
                if res is None: rl.logmsg(f"  rep{rep} upload FAILED"); break
                if res.get("rate_limited"): time.sleep(60); continue
                pnl = res["asset_pnl"]; dd = res.get("max_drawdown") or 0
                score = pnl - 0.5 * dd
                rl.logmsg(f"  rep{rep}: pnl={pnl:+.0f} dd={dd:.0f} score={score:+.0f}")
                all_data["runs"].append({
                    "run_id": run_id, "variant": vid, "rep": rep,
                    "pnl": pnl, "dd": dd, "score": score,
                    "when": datetime.now().isoformat(),
                })
                RESULTS.write_text(json.dumps(all_data, indent=2))
                break
            time.sleep(15)

    # Summary stats
    rl.logmsg(f"\n\n=== VARIANCE SUMMARY ===")
    rl.logmsg(f"{'variant':<14} {'reps':>4} {'pnl_mean':>9} {'pnl_min':>8} {'pnl_max':>8} {'pnl_std':>8} {'score_mean':>10}")
    for vid, label, _ in VARIANTS:
        rs = [r for r in all_data["runs"] if r["variant"] == vid]
        if not rs: continue
        pnls = [r["pnl"] for r in rs]
        scores = [r["score"] for r in rs]
        std = statistics.stdev(pnls) if len(pnls) > 1 else 0
        rl.logmsg(f"  {vid:<12} {len(rs):>4} {statistics.mean(pnls):>+9.0f} "
                  f"{min(pnls):>+8.0f} {max(pnls):>+8.0f} {std:>8.0f} "
                  f"{statistics.mean(scores):>+10.0f}")
    rl.logmsg(f"\n  → spread within variant + spread between variants tells us "
              f"how much of the +$129 grid 'winner' is real signal vs noise.")


if __name__ == "__main__":
    main()
