#!/usr/bin/env python3
"""
Phase 7: catastrophic-insurance stop-loss on top of P4.C (+8,305).

Goal: confirm a HIGH-bar stop-loss is essentially free in the current
100k env (never fires on observed regimes), so it can serve as insurance
against unknown future market regimes.

  P7.A  conservative (recommended)
        pos=100, dd=4000, ticks=200 (20k ts), size=30
        Should NEVER fire on this 100k day.

  P7.B  slightly more sensitive
        pos=100, dd=3000, ticks=150 (15k ts), size=30
        Might catch tail risk earlier; some chance it fires once.

Acceptance: insurance variant is "free" if Δscore vs P4.C >= -50.
If both pass, P7.A is the recommended baked-in default for HG.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import research_loop as rl

HERE = Path(__file__).parent
RESULTS_FILE = HERE / "phase7_results.json"

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

    hgA = dict(best_hg)
    hgA["stop_pos_threshold"] = 100
    hgA["stop_drawdown_threshold"] = 4000.0
    hgA["stop_min_ticks_at_pos"] = 200       # 20k ts
    hgA["stop_unwind_size"] = 30
    probes.append(("p7a_hg_insurance_conservative",
                   "HG insurance: pos=100, dd=4000, ticks=200 (20k ts), size=30",
                   hgA, best_vf))

    hgB = dict(best_hg)
    hgB["stop_pos_threshold"] = 100
    hgB["stop_drawdown_threshold"] = 3000.0
    hgB["stop_min_ticks_at_pos"] = 150       # 15k ts
    hgB["stop_unwind_size"] = 30
    probes.append(("p7b_hg_insurance_sensitive",
                   "HG insurance: pos=100, dd=3000, ticks=150 (15k ts), size=30",
                   hgB, best_vf))

    return probes


def load_existing() -> dict:
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text())
    return {"started_at": datetime.now().isoformat(),
            "baseline": {"pnl": BASE_PNL, "dd": BASE_DD, "score": BASE_SCORE,
                         "tag": "p4c_hg_k_inv_1p0"},
            "results": {}}


def save(d): RESULTS_FILE.write_text(json.dumps(d, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inter-upload-delay", type=int, default=30)
    ap.add_argument("--rate-limit-backoff", type=int, default=60)
    args = ap.parse_args()

    best_hg, best_vf = load_best_from_state()
    probes = build_probes(best_hg, best_vf)

    rl.logmsg(f"=== Phase 7 — {len(probes)} insurance probes off P4.C ===")
    rl.logmsg(f"Baseline P4.C: pnl=+{BASE_PNL} dd={BASE_DD} score=+{BASE_SCORE:.0f}")

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
            verdict = "FREE" if d_sc >= -50 else ("OK" if d_sc >= -200 else "COSTLY")
            rl.logmsg(f"  pnl=+{pnl:.0f} dd={dd:.0f} vol={vol:.1f} score=+{score:.0f}")
            rl.logmsg(f"  vs P4.C: Δpnl={d_pnl:+.0f} Δdd={d_dd:+.0f} Δscore={d_sc:+.0f}  [{verdict}]")
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

    rl.logmsg("\n=== Phase 7 summary ===")
    for vid, _, _, _ in probes:
        r = results["results"].get(vid, {})
        if r.get("asset_pnl") is not None:
            rl.logmsg(f"  {vid:<35} score={r['score']:+.0f}  Δscore={r['score']-BASE_SCORE:+.0f}  [{r.get('verdict','?')}]")


if __name__ == "__main__":
    main()
