#!/usr/bin/env python3
"""
Phase 8: probe unexplored mechanisms beyond inventory shade.

Phase 6 confirmed the k_inv / soft_cap axis is saturated. This phase
tests three single-knob axes that have never been validated and target
different mechanisms:

  P8.A  HG take_edge 8 → 6      (partially relax the take barrier;
                                  P4.A at 3 was catastrophic but 6 untested)
  P8.B  HG asymm_skew 1.0 → 1.3 (long-side gets stronger shade;
                                  every observed stuck-pos was LONG)
  P8.C  HG revert_window 120 → 80 (shorter mean-revert window;
                                    may dodge the revert-into-trend trap)

Baseline: P4.C + catastrophic insurance (+8,305 / 4,224 / +6,193).
Acceptance: Δscore > +50 = winner; Δscore in [-50, +50] = neutral; Δ<-50 = loser.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import research_loop as rl

HERE = Path(__file__).parent
RESULTS_FILE = HERE / "phase8_results.json"

BASE_PNL, BASE_DD = 8305, 4224
BASE_SCORE = BASE_PNL - 0.5 * BASE_DD  # 6193


def load_best_from_state() -> tuple[dict, dict]:
    state = json.loads((HERE / "state.json").read_text())
    hg, vf = dict(state["best_hg"]), dict(state["best_vf"])
    for d in (hg, vf):
        d.setdefault("revert_gate_threshold", 0.0)
        d.setdefault("stop_pos_threshold", 0)
        d.setdefault("stop_drawdown_threshold", 800.0)
        d.setdefault("stop_min_ticks_at_pos", 100)
        d.setdefault("stop_unwind_size", 20)
    return hg, vf


def build_probes(best_hg: dict, best_vf: dict) -> list:
    probes = []

    hgA = dict(best_hg); hgA["take_edge"] = 6
    probes.append(("p8a_hg_take_edge_6",
                   "HG take_edge 8 → 6 (partial barrier relaxation)",
                   hgA, best_vf))

    hgB = dict(best_hg); hgB["asymm_skew"] = 1.3
    probes.append(("p8b_hg_asymm_skew_1p3",
                   "HG asymm_skew 1.0 → 1.3 (stronger long-side shade)",
                   hgB, best_vf))

    hgC = dict(best_hg); hgC["revert_window"] = 80
    probes.append(("p8c_hg_revert_window_80",
                   "HG revert_window 120 → 80 (shorter revert lag)",
                   hgC, best_vf))

    return probes


def load_existing() -> dict:
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text())
    return {"started_at": datetime.now().isoformat(),
            "baseline": {"pnl": BASE_PNL, "dd": BASE_DD, "score": BASE_SCORE,
                         "tag": "p7a_p4c_with_insurance"},
            "results": {}}


def save(d): RESULTS_FILE.write_text(json.dumps(d, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--inter-upload-delay", type=int, default=30)
    ap.add_argument("--rate-limit-backoff", type=int, default=60)
    args = ap.parse_args()

    best_hg, best_vf = load_best_from_state()
    probes = build_probes(best_hg, best_vf)

    if args.ids:
        wanted = {x.lower().replace(".", "").replace("-", "") for x in args.ids}
        probes = [p for p in probes if p[0].lower().replace("_", "").startswith(tuple(wanted))]

    rl.logmsg(f"=== Phase 8 — {len(probes)} new-axis probes off P4.C+insurance ===")
    rl.logmsg(f"Baseline: pnl=+{BASE_PNL} dd={BASE_DD} score=+{BASE_SCORE:.0f}")

    results = load_existing()

    for i, (vid, label, hg, vf) in enumerate(probes, 1):
        if vid in results["results"]:
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
            save(results); continue
        rl.logmsg(f"  guardrail: {msg}")

        while True:
            res = rl.upload_and_get_pnl(path)
            if res is None:
                rl.logmsg(f"  upload FAILED")
                results["results"][vid] = {"variant_id": vid, "label": label,
                                            "upload_failed": True,
                                            "when": datetime.now().isoformat()}
                save(results); break
            if res.get("rate_limited"):
                rl.logmsg(f"  rate limited; sleeping {args.rate_limit_backoff}s")
                time.sleep(args.rate_limit_backoff); continue
            pnl = res["asset_pnl"]; dd = res.get("max_drawdown") or 0
            vol = res.get("pnl_volatility") or 0
            score = pnl - 0.5 * dd
            d_pnl = pnl - BASE_PNL; d_dd = dd - BASE_DD; d_sc = score - BASE_SCORE
            verdict = "WIN" if d_sc > 50 else ("LOSS" if d_sc < -50 else "NEUTRAL")
            rl.logmsg(f"  pnl=+{pnl:.0f} dd={dd:.0f} vol={vol:.1f} score=+{score:.0f}")
            rl.logmsg(f"  Δpnl={d_pnl:+.0f} Δdd={d_dd:+.0f} Δscore={d_sc:+.0f}  [{verdict}]")
            results["results"][vid] = {
                "variant_id": vid, "label": label,
                "asset_pnl": pnl, "max_drawdown": dd, "pnl_volatility": vol,
                "score": score, "hg": hg, "vf": vf,
                "run_dir": res.get("run_dir"),
                "when": datetime.now().isoformat(),
                "verdict": verdict,
            }
            save(results); break

        if i < len(probes):
            time.sleep(args.inter_upload_delay)

    rl.logmsg("\n=== Phase 8 summary ===")
    winners = []
    for vid, _, _, _ in probes:
        r = results["results"].get(vid, {})
        if r.get("rejected_local"):
            rl.logmsg(f"  {vid:<30} REJECTED")
        elif r.get("upload_failed"):
            rl.logmsg(f"  {vid:<30} upload failed")
        elif r.get("asset_pnl") is not None:
            d = r["score"] - BASE_SCORE
            v = r.get("verdict", "?")
            rl.logmsg(f"  {vid:<30} score={r['score']:+.0f}  Δ={d:+.0f}  [{v}]")
            if d > 50: winners.append((vid, r['score']))
    if winners:
        rl.logmsg(f"\n  WINNERS:")
        for v, s in sorted(winners, key=lambda x: -x[1]):
            rl.logmsg(f"    {v}: score={s:+.0f}")


if __name__ == "__main__":
    main()
