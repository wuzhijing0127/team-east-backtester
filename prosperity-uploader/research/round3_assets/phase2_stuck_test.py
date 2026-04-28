#!/usr/bin/env python3
"""
Phase 2: HG stuck-position probes.

Tests three single-axis HG perturbations from the current best
(`x2_hgtrend__hgvel_co_86761`) targeting the 80k-90k stuck-and-bleeding behavior.
VF is intentionally unchanged in P2.A; VF probes are deferred until HG fixes
prove insufficient.

Run:
    cd prosperity-uploader/research/round3_assets
    python3 phase2_stuck_test.py            # all probes
    python3 phase2_stuck_test.py P2.A       # subset

Results → phase2_results.json. Does NOT touch state.json or the main loop.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import research_loop as rl

HERE = Path(__file__).parent
RESULTS_FILE = HERE / "phase2_results.json"


def load_best_from_state() -> tuple[dict, dict]:
    state = json.loads((HERE / "state.json").read_text())
    return dict(state["best_hg"]), dict(state["best_vf"])


def build_probes(best_hg: dict, best_vf: dict) -> list:
    """HG-first probes targeting the 80k-90k stuck behavior."""
    probes = []

    # P2.A — strengthen HG inventory shade (HIGHEST PRIORITY)
    hgA = dict(best_hg); hgA["k_inv"] = 0.50
    probes.append(("p2a_hg_k_inv_0p5", "HG k_inv 0.10 → 0.50", hgA, best_vf))

    # P2.B — earlier HG inventory cap so skew engages before max
    hgB = dict(best_hg); hgB["soft_cap"] = 100
    probes.append(("p2b_hg_soft_cap_100", "HG soft_cap 150 → 100", hgB, best_vf))

    # P2.C — disable HG mean-revert (the buy-the-dip-into-trend trap)
    hgC = dict(best_hg); hgC["revert_coef"] = 0.0
    probes.append(("p2c_hg_revert_coef_0", "HG revert_coef 0.20 → 0", hgC, best_vf))

    # ─── Deferred VF probes (only if A+B+C combined isn't enough) ───
    # P2.D — earlier VF inventory cap
    vfD = dict(best_vf); vfD["soft_cap"] = 150
    probes.append(("p2d_vf_soft_cap_150", "VF soft_cap 200 → 150", best_hg, vfD))

    # P2.E — disable VF mean-revert
    vfE = dict(best_vf); vfE["revert_coef"] = 0.0
    probes.append(("p2e_vf_revert_coef_0", "VF revert_coef 0.40 → 0", best_hg, vfE))

    return probes


def load_existing_results() -> dict:
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text())
    return {"started_at": datetime.now().isoformat(), "results": {}}


def save_results(d: dict) -> None:
    RESULTS_FILE.write_text(json.dumps(d, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", help="Optional subset of probe ids")
    ap.add_argument("--inter-upload-delay", type=int, default=30)
    ap.add_argument("--rate-limit-backoff", type=int, default=60)
    ap.add_argument("--rerun", action="store_true")
    ap.add_argument("--hg-only", action="store_true",
                    help="run only the HG probes (P2.A/B/C), skip the deferred VF ones")
    args = ap.parse_args()

    best_hg, best_vf = load_best_from_state()
    probes = build_probes(best_hg, best_vf)

    if args.hg_only:
        probes = [p for p in probes if p[0].startswith("p2a") or p[0].startswith("p2b") or p[0].startswith("p2c")]

    if args.ids:
        wanted = {x.lower().replace(".", "").replace("-", "") for x in args.ids}
        probes = [p for p in probes if p[0].lower().replace("_", "").startswith(tuple(wanted))]

    rl.logmsg(f"=== Phase 2 stuck-position probes — {len(probes)} variants ===")
    rl.logmsg(f"Baseline HG/VF loaded from state.json (best variant)")

    results = load_existing_results()
    base_pnl, base_dd = 8220, 4607
    base_score = base_pnl - 0.5 * base_dd

    for i, (vid, label, hg, vf) in enumerate(probes, 1):
        if vid in results["results"] and not args.rerun:
            rl.logmsg(f"[{i}/{len(probes)}] {vid} — already tested, skipping")
            continue
        rl.logmsg(f"\n[{i}/{len(probes)}] {vid}: {label}")

        path = rl.write_variant(vid, label, hg, vf)
        ok, msg = rl.local_sim_3day(path)
        if not ok:
            rl.logmsg(f"  REJECTED by guardrail: {msg}")
            results["results"][vid] = {"variant_id": vid, "label": label,
                                        "rejected_local": True, "reason": msg,
                                        "when": datetime.now().isoformat()}
            save_results(results)
            continue
        rl.logmsg(f"  guardrail: {msg}")

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
            pnl = res["asset_pnl"]
            dd = res.get("max_drawdown") or 0
            vol = res.get("pnl_volatility") or 0
            score = pnl - 0.5 * dd
            d_pnl = pnl - base_pnl; d_dd = dd - base_dd; d_sc = score - base_score
            rl.logmsg(f"  pnl=+{pnl:.0f} dd={dd:.0f} vol={vol:.1f} score=+{score:.0f}  "
                      f"Δpnl={d_pnl:+.0f} Δdd={d_dd:+.0f} Δscore={d_sc:+.0f}")
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

    rl.logmsg("\n=== Phase 2 results ===")
    rl.logmsg(f"  baseline (x2_hgtrend__hgvel_co_86761): pnl=+{base_pnl} dd={base_dd} score=+{base_score:.0f}")
    for vid, _, _, _ in probes:
        r = results["results"].get(vid, {})
        if r.get("rejected_local"):
            rl.logmsg(f"  {vid:<28} REJECTED: {r.get('reason','')}")
        elif r.get("upload_failed"):
            rl.logmsg(f"  {vid:<28} upload failed")
        elif r.get("asset_pnl") is not None:
            pnl = r["asset_pnl"]; dd = r["max_drawdown"]; sc = r["score"]
            d_pnl = pnl - base_pnl; d_dd = dd - base_dd; d_sc = sc - base_score
            rl.logmsg(f"  {vid:<28} pnl={pnl:>+7.0f} dd={dd:>6.0f} score={sc:>+7.0f}  "
                      f"Δpnl={d_pnl:+.0f} Δdd={d_dd:+.0f} Δscore={d_sc:+.0f}")


if __name__ == "__main__":
    main()
