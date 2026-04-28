#!/usr/bin/env python3
"""
Phase 3: engine-level VF/HG fixes.

P3.A — trend-gated revert (E1): revert_coef gets scaled down when trend signal
       is strong. Targets the "buy-the-dip-into-real-trend" trap that hurt HG
       in the 80k-90k window. Engine change in assets_template.py adds
       `revert_gate_threshold` param.

P3.B — k_inv 0.50 on BOTH HG and VF (the user-requested "Option 1 with VF").
       P2.A showed HG k_inv=0.50 gave +139 score; testing whether also pushing
       VF inventory shade up helps further.

Run:
    python3 phase3_engine_test.py
    python3 phase3_engine_test.py P3.A
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import research_loop as rl

HERE = Path(__file__).parent
RESULTS_FILE = HERE / "phase3_results.json"

# Baseline (the current best variant)
BASE_PNL, BASE_DD = 8220, 4607
BASE_SCORE = BASE_PNL - 0.5 * BASE_DD


def load_best_from_state() -> tuple[dict, dict]:
    state = json.loads((HERE / "state.json").read_text())
    hg, vf = dict(state["best_hg"]), dict(state["best_vf"])
    # Ensure new fields exist with safe defaults
    for d in (hg, vf):
        d.setdefault("revert_gate_threshold", 0.0)
    return hg, vf


def build_probes(best_hg: dict, best_vf: dict) -> list:
    probes = []

    # P3.A — trend-gated revert (HG only; VF unchanged)
    hgA = dict(best_hg); hgA["revert_gate_threshold"] = 3.0
    probes.append((
        "p3a_hg_revert_gated",
        "HG revert_gate_threshold=3.0 (gates revert when |trend|≥3)",
        hgA, best_vf,
    ))

    # P3.B — k_inv 0.50 on BOTH HG and VF
    hgB = dict(best_hg); hgB["k_inv"] = 0.50
    vfB = dict(best_vf); vfB["k_inv"] = 0.50
    probes.append((
        "p3b_both_k_inv_0p5",
        "HG+VF k_inv → 0.50",
        hgB, vfB,
    ))

    # P3.C — combine: trend-gated revert + k_inv=0.5 both. Stack the two.
    hgC = dict(hgA); hgC["k_inv"] = 0.50
    vfC = dict(vfB)  # already k_inv=0.5
    probes.append((
        "p3c_combined_e1_kinv",
        "trend-gated revert + k_inv=0.5 on both (combined defensive)",
        hgC, vfC,
    ))

    return probes


def load_existing_results() -> dict:
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text())
    return {"started_at": datetime.now().isoformat(), "results": {}}


def save_results(d: dict) -> None:
    RESULTS_FILE.write_text(json.dumps(d, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--inter-upload-delay", type=int, default=30)
    ap.add_argument("--rate-limit-backoff", type=int, default=60)
    ap.add_argument("--rerun", action="store_true")
    args = ap.parse_args()

    best_hg, best_vf = load_best_from_state()
    probes = build_probes(best_hg, best_vf)

    if args.ids:
        wanted = {x.lower().replace(".", "").replace("-", "") for x in args.ids}
        probes = [p for p in probes if p[0].lower().replace("_", "").startswith(tuple(wanted))]

    rl.logmsg(f"=== Phase 3 engine-level probes — {len(probes)} variants ===")
    rl.logmsg(f"Baseline: pnl=+{BASE_PNL} dd={BASE_DD} score=+{BASE_SCORE:.0f}")

    results = load_existing_results()

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
            d_pnl = pnl - BASE_PNL; d_dd = dd - BASE_DD; d_sc = score - BASE_SCORE
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

    rl.logmsg("\n=== Phase 3 results ===")
    rl.logmsg(f"  baseline pnl=+{BASE_PNL} dd={BASE_DD} score=+{BASE_SCORE:.0f}")
    for vid, _, _, _ in probes:
        r = results["results"].get(vid, {})
        if r.get("rejected_local"):
            rl.logmsg(f"  {vid:<28} REJECTED: {r.get('reason','')}")
        elif r.get("upload_failed"):
            rl.logmsg(f"  {vid:<28} upload failed")
        elif r.get("asset_pnl") is not None:
            pnl = r["asset_pnl"]; dd = r["max_drawdown"]; sc = r["score"]
            d_pnl = pnl - BASE_PNL; d_dd = dd - BASE_DD; d_sc = sc - BASE_SCORE
            rl.logmsg(f"  {vid:<28} pnl={pnl:>+7.0f} dd={dd:>5.0f} score={sc:>+7.0f}  "
                      f"Δpnl={d_pnl:+.0f} Δdd={d_dd:+.0f} Δscore={d_sc:+.0f}")


if __name__ == "__main__":
    main()
