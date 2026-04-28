#!/usr/bin/env python3
"""
Phase 1 VF stabilization probes.

Tests 6 single-axis VF perturbations from the current best
(`x2_hgtrend__hgvel_co_86761`) to identify which knob reduces the
50k-70k drawdown without sacrificing PnL.

Each probe = one VF param changed. HG params are kept identical so
HG performance is preserved.

Run:
    cd prosperity-uploader/research/round3_assets
    python3 phase1_vf_test.py            # run all 6
    python3 phase1_vf_test.py P1.C P1.E  # run a subset

Results → phase1_results.json. Does NOT touch state.json or affect
the main research_loop.py search.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import research_loop as rl

HERE = Path(__file__).parent
RESULTS_FILE = HERE / "phase1_results.json"


def load_best_from_state() -> tuple[dict, dict]:
    """Read current best HG/VF params from the loop's state.json."""
    state = json.loads((HERE / "state.json").read_text())
    return dict(state["best_hg"]), dict(state["best_vf"])


def build_probes(best_hg: dict, best_vf: dict) -> list:
    """Return list of (id, label, hg_params, vf_params) probes."""
    probes = []

    # P1.A — gentler velocity (user-suggested)
    vfA = dict(best_vf); vfA["vel_coef"] = 0.20
    probes.append(("p1a_vf_vel_0p20", "VF vel_coef 0.30 → 0.20", best_hg, vfA))

    # P1.B — even gentler velocity
    vfB = dict(best_vf); vfB["vel_coef"] = 0.15
    probes.append(("p1b_vf_vel_0p15", "VF vel_coef 0.30 → 0.15", best_hg, vfB))

    # P1.C — direct spread widening
    vfC = dict(best_vf); vfC["half_spread"] = 2
    probes.append(("p1c_vf_half_spread_2", "VF half_spread 1 → 2", best_hg, vfC))

    # P1.D — slower mean-rev window
    vfD = dict(best_vf); vfD["revert_window"] = 120
    probes.append(("p1d_vf_revert_window_120", "VF revert_window 60 → 120", best_hg, vfD))

    # P1.E — turn off short-term anti-momentum
    vfE = dict(best_vf); vfE["trend_coef"] = 0.0
    probes.append(("p1e_vf_trend_coef_0", "VF trend_coef -0.20 → 0", best_hg, vfE))

    # P1.F — actual slow-anchor dual-EMA (half-life ~700)
    vfF = dict(best_vf)
    vfF["long_ema_alpha"] = 0.001
    vfF["ema_blend"] = 0.30
    probes.append(("p1f_vf_slow_anchor", "VF long_ema_alpha=0.001 ema_blend=0.30", best_hg, vfF))

    # P1.G — tighten inventory shade via k_inv (user-suggested next move)
    vfG = dict(best_vf); vfG["k_inv"] = 0.12
    probes.append(("p1g_vf_k_inv_0p12", "VF k_inv 0.08 → 0.12", best_hg, vfG))

    # P1.H — earlier inventory cap via soft_cap
    vfH = dict(best_vf); vfH["soft_cap"] = 150
    probes.append(("p1h_vf_soft_cap_150", "VF soft_cap 200 → 150", best_hg, vfH))

    # P1.I — combined inventory tightening (user's full defensive layer)
    vfI = dict(best_vf); vfI["k_inv"] = 0.12; vfI["soft_cap"] = 150
    probes.append(("p1i_vf_inv_combined", "VF k_inv 0.12 + soft_cap 150", best_hg, vfI))

    return probes


def load_existing_results() -> dict:
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text())
    return {"started_at": datetime.now().isoformat(), "results": {}}


def save_results(d: dict) -> None:
    RESULTS_FILE.write_text(json.dumps(d, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", help="Optional subset of probe ids (e.g., P1.A P1.C)")
    ap.add_argument("--inter-upload-delay", type=int, default=30,
                    help="seconds between uploads")
    ap.add_argument("--rate-limit-backoff", type=int, default=60)
    ap.add_argument("--rerun", action="store_true",
                    help="re-test probes that already have results")
    args = ap.parse_args()

    best_hg, best_vf = load_best_from_state()
    probes = build_probes(best_hg, best_vf)

    if args.ids:
        wanted = {x.lower().replace(".", "").replace("-", "") for x in args.ids}
        probes = [p for p in probes if p[0].lower().replace("_", "").startswith(tuple(wanted))]

    rl.logmsg(f"=== Phase 1 VF probes — {len(probes)} variants ===")
    rl.logmsg(f"Baseline HG/VF loaded from state.json (best variant)")

    results = load_existing_results()

    for i, (vid, label, hg, vf) in enumerate(probes, 1):
        if vid in results["results"] and not args.rerun:
            rl.logmsg(f"[{i}/{len(probes)}] {vid} — already tested, skipping (use --rerun to retest)")
            continue
        rl.logmsg(f"\n[{i}/{len(probes)}] {vid}: {label}")

        # Write + local guardrail
        path = rl.write_variant(vid, label, hg, vf)
        ok, msg = rl.local_sim_3day(path)
        if not ok:
            rl.logmsg(f"  REJECTED by local guardrail: {msg}")
            results["results"][vid] = {"variant_id": vid, "label": label,
                                        "rejected_local": True, "reason": msg,
                                        "when": datetime.now().isoformat()}
            save_results(results)
            continue
        rl.logmsg(f"  guardrail: {msg}")

        # Upload
        while True:
            res = rl.upload_and_get_pnl(path)
            if res is None:
                rl.logmsg(f"  upload FAILED — moving on")
                results["results"][vid] = {"variant_id": vid, "label": label,
                                            "upload_failed": True,
                                            "when": datetime.now().isoformat()}
                save_results(results)
                break
            if res.get("rate_limited"):
                rl.logmsg(f"  rate limited; sleeping {args.rate_limit_backoff}s")
                time.sleep(args.rate_limit_backoff)
                continue
            # success
            pnl = res["asset_pnl"]
            dd = res.get("max_drawdown") or 0
            vol = res.get("pnl_volatility") or 0
            score = pnl - 0.5 * dd
            rl.logmsg(f"  pnl=+{pnl:.0f}  dd={dd:.0f}  vol={vol:.1f}  score=+{score:.0f}")
            results["results"][vid] = {
                "variant_id": vid, "label": label,
                "asset_pnl": pnl, "max_drawdown": dd, "pnl_volatility": vol,
                "score": score, "hg": hg, "vf": vf,
                "run_dir": res.get("run_dir"),
                "when": datetime.now().isoformat(),
            }
            save_results(results)
            break

        if i < len(probes):
            time.sleep(args.inter_upload_delay)

    # Summary table
    rl.logmsg("\n=== Phase 1 results ===")
    rl.logmsg(f"{'probe':<32} {'pnl':>7} {'dd':>6} {'score':>7}  vs baseline")
    base_pnl, base_dd = 8220, 4607  # x2_hgtrend__hgvel_co_86761
    base_score = base_pnl - 0.5 * base_dd
    rl.logmsg(f"{'(baseline)':<32} {base_pnl:>+7} {base_dd:>6} {base_score:>+7}  --")
    for vid, _, _, _ in probes:
        r = results["results"].get(vid, {})
        if r.get("rejected_local"):
            rl.logmsg(f"  {vid:<30} REJECTED: {r.get('reason','')}")
        elif r.get("upload_failed"):
            rl.logmsg(f"  {vid:<30} upload failed")
        elif r.get("asset_pnl") is not None:
            pnl = r["asset_pnl"]; dd = r["max_drawdown"]; sc = r["score"]
            d_pnl = pnl - base_pnl; d_dd = dd - base_dd; d_sc = sc - base_score
            rl.logmsg(f"  {vid:<30} {pnl:>+7.0f} {dd:>6.0f} {sc:>+7.0f}  "
                      f"Δpnl={d_pnl:+.0f} Δdd={d_dd:+.0f} Δscore={d_sc:+.0f}")


if __name__ == "__main__":
    main()
