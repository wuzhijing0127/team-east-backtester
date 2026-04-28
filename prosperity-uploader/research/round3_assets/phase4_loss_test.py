#!/usr/bin/env python3
"""
Phase 4: external-analysis-driven HG fixes + stop-loss engine feature.

After Phase 2 and 3 showed that single-knob k_inv/soft_cap/revert tuning was
insufficient (revert can't be disabled without losing too much PnL; gate at
threshold=3 was effectively == revert=0 because median |trend_signal|=3 on HG),
test the external analyst's three suggestions plus a higher gate threshold and
the new engine-level stop-loss.

Probes:
  P4.A  HG take_edge   8 → 3      (allow aggressive sells when long)
  P4.B  HG half_spread 6 → 3      (responsive quotes, smaller skew distance)
  P4.C  HG k_inv       0.10 → 1.0 (4× P2.A; force shade past the take_edge wall)
  P4.D  HG revert_gate_threshold 0 → 8  (gate fires only on REAL trends, not noise)
  P4.E  bundle: take_edge=3 + half_spread=3 + k_inv=1.0
  P4.F  stop-loss only: HG stop_pos_threshold=80 + stop_drawdown_threshold=800

Run:
    python3 phase4_loss_test.py
    python3 phase4_loss_test.py P4.A P4.D
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import research_loop as rl

HERE = Path(__file__).parent
RESULTS_FILE = HERE / "phase4_results.json"

BASE_PNL, BASE_DD = 8220, 4607
BASE_SCORE = BASE_PNL - 0.5 * BASE_DD


def load_best_from_state() -> tuple[dict, dict]:
    state = json.loads((HERE / "state.json").read_text())
    hg, vf = dict(state["best_hg"]), dict(state["best_vf"])
    # Ensure new fields exist (for backward-compat with older state files)
    for d in (hg, vf):
        d.setdefault("revert_gate_threshold", 0.0)
        d.setdefault("stop_pos_threshold", 0)
        d.setdefault("stop_drawdown_threshold", 800.0)
        d.setdefault("stop_min_ticks_at_pos", 100)
        d.setdefault("stop_unwind_size", 20)
    return hg, vf


def build_probes(best_hg: dict, best_vf: dict) -> list:
    probes = []

    # P4.A — lower take_edge so inventory pressure can trigger aggressive sells
    hgA = dict(best_hg); hgA["take_edge"] = 3
    probes.append(("p4a_hg_take_edge_3", "HG take_edge 8 → 3", hgA, best_vf))

    # P4.B — narrower spread, more responsive quotes
    hgB = dict(best_hg); hgB["half_spread"] = 3
    probes.append(("p4b_hg_half_spread_3", "HG half_spread 6 → 3", hgB, best_vf))

    # P4.C — much stronger inventory shade
    hgC = dict(best_hg); hgC["k_inv"] = 1.0
    probes.append(("p4c_hg_k_inv_1p0", "HG k_inv 0.10 → 1.0", hgC, best_vf))

    # P4.D — gate revert at threshold=8 (only real trends, not normal noise)
    hgD = dict(best_hg); hgD["revert_gate_threshold"] = 8.0
    probes.append(("p4d_hg_revert_gate_8", "HG revert_gate_threshold=8 (real trends only)", hgD, best_vf))

    # P4.E — bundle of A+B+C
    hgE = dict(best_hg)
    hgE["take_edge"] = 3
    hgE["half_spread"] = 3
    hgE["k_inv"] = 1.0
    probes.append(("p4e_hg_aggressive_bundle", "HG take_edge=3 + half_spread=3 + k_inv=1.0", hgE, best_vf))

    # P4.F — stop-loss feature only (engine), conservative thresholds
    hgF = dict(best_hg)
    hgF["stop_pos_threshold"] = 80
    hgF["stop_drawdown_threshold"] = 800.0
    hgF["stop_min_ticks_at_pos"] = 100
    hgF["stop_unwind_size"] = 20
    probes.append(("p4f_hg_stop_loss_basic", "HG stop-loss: pos≥80, dd≥800, ticks≥100, size=20", hgF, best_vf))

    # P4.G — combine winning P4.D revert-gate-8 + stop-loss
    hgG = dict(hgD)
    hgG["stop_pos_threshold"] = 80
    hgG["stop_drawdown_threshold"] = 800.0
    hgG["stop_min_ticks_at_pos"] = 100
    hgG["stop_unwind_size"] = 20
    probes.append(("p4g_hg_gate8_plus_stop", "HG revert_gate=8 + stop-loss", hgG, best_vf))

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

    rl.logmsg(f"=== Phase 4 — {len(probes)} variants ===")
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

    rl.logmsg("\n=== Phase 4 results ===")
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
