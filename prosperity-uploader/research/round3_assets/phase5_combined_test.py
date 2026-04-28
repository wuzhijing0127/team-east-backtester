#!/usr/bin/env python3
"""
Phase 5: combine P4.C winner (HG k_inv=1.0) with retuned stop-loss
and the new 3-layer environment-agnostic unwind (already in engine).

Single probe — no grid. If this beats P4.C's +6,193 score we promote it
as the new baseline.

Probe P5.A:
  - HG k_inv = 1.0                    (from P4.C)
  - HG stop_pos_threshold = 60        (was 80; catch loaded inv earlier)
  - HG stop_drawdown_threshold = 1500 (was 800; filter MM noise)
  - HG stop_min_ticks_at_pos = 20     (was 100; ~2k ts, not 10k)
  - HG stop_unwind_size = 30          (was 20; faster flush)
  - VF unchanged
  - 3-layer unwind: now ALWAYS active (engine-level)

Run:
    python3 phase5_combined_test.py
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import research_loop as rl

HERE = Path(__file__).parent
RESULTS_FILE = HERE / "phase5_results.json"

# Compare against P4.C (the current best after Phase 4)
P4C_PNL, P4C_DD = 8305, 4224
P4C_SCORE = P4C_PNL - 0.5 * P4C_DD  # 6193


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
    """Two isolation probes after P5.A failed (-2191 score):
    P5.B = unwind only (stop OFF) -> isolates Layer 2 cost
    P5.C = unwind + dialed-back stop -> cure stuck without sell-into-recovery
    """
    out = []

    # P5.B: just k_inv=1.0 + 3-layer unwind. Stop-loss disabled.
    hgB = dict(best_hg)
    hgB["k_inv"] = 1.0
    hgB["stop_pos_threshold"] = 0  # OFF
    out.append(("p5b_kinv1_unwindonly",
                "HG k_inv=1.0 + 3-layer unwind ONLY (stop OFF)", hgB, best_vf))

    # P5.C: k_inv=1.0 + 3-layer unwind + dialed-back stop
    hgC = dict(best_hg)
    hgC["k_inv"] = 1.0
    hgC["stop_pos_threshold"] = 100         # only catch truly loaded inv
    hgC["stop_drawdown_threshold"] = 3000.0 # filter MM noise
    hgC["stop_min_ticks_at_pos"] = 30       # ~3k ts, not 2k
    hgC["stop_unwind_size"] = 30
    out.append(("p5c_kinv1_unwind_safer_stop",
                "HG k_inv=1.0 + 3-layer unwind + stop(100,3000,30,30)",
                hgC, best_vf))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate-limit-backoff", type=int, default=60)
    args = ap.parse_args()

    best_hg, best_vf = load_best_from_state()
    probes = build_probes(best_hg, best_vf)

    # Load existing results so we keep P5.A
    if RESULTS_FILE.exists():
        all_results = json.loads(RESULTS_FILE.read_text())
    else:
        all_results = {"results": {}}

    rl.logmsg(f"=== Phase 5 — {len(probes)} probes ===")
    rl.logmsg(f"P4.C reference: pnl=+{P4C_PNL} dd={P4C_DD} score=+{P4C_SCORE:.0f}")

    for i, (vid, label, hg, vf) in enumerate(probes, 1):
        if vid in all_results.get("results", {}):
            rl.logmsg(f"[{i}/{len(probes)}] {vid} — already tested, skipping")
            continue
        rl.logmsg(f"\n[{i}/{len(probes)}] {vid}: {label}")

        path = rl.write_variant(vid, label, hg, vf)
        ok, msg = rl.local_sim_3day(path)
        if not ok:
            rl.logmsg(f"  REJECTED by guardrail: {msg}")
            continue
        rl.logmsg(f"  guardrail: {msg}")

        while True:
            res = rl.upload_and_get_pnl(path)
            if res is None:
                rl.logmsg(f"  upload FAILED")
                break
            if res.get("rate_limited"):
                rl.logmsg(f"  rate limited; sleeping {args.rate_limit_backoff}s")
                time.sleep(args.rate_limit_backoff)
                continue
            pnl = res["asset_pnl"]
            dd = res.get("max_drawdown") or 0
            vol = res.get("pnl_volatility") or 0
            score = pnl - 0.5 * dd
            d_pnl = pnl - P4C_PNL; d_dd = dd - P4C_DD; d_sc = score - P4C_SCORE
            rl.logmsg(f"  pnl=+{pnl:.0f} dd={dd:.0f} vol={vol:.1f} score=+{score:.0f}")
            rl.logmsg(f"  vs P4.C: Δpnl={d_pnl:+.0f} Δdd={d_dd:+.0f} Δscore={d_sc:+.0f}")
            all_results["results"][vid] = {
                "variant_id": vid, "label": label,
                "asset_pnl": pnl, "max_drawdown": dd, "pnl_volatility": vol,
                "score": score, "hg": hg, "vf": vf,
                "run_dir": res.get("run_dir"),
                "when": datetime.now().isoformat(),
            }
            RESULTS_FILE.write_text(json.dumps(all_results, indent=2))
            break

        if i < len(probes):
            time.sleep(30)


if __name__ == "__main__":
    main()
