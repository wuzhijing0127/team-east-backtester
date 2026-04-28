#!/usr/bin/env python3
"""
Phase 6: extend P4.C (HG k_inv=1.0, +8,305 PnL / +6,193 score).

Six single-knob probes, ranked by expected value × low risk. All target
the negative-feedback inventory mechanism that made P4.C win, in places
where it's not yet fully exploited.

  P6.A  HG k_inv 1.0 → 1.5            (extend winning axis)
  P6.B  HG soft_cap 150 → 100         (synergy: shade ramps faster)
  P6.C  VF k_inv 0.08 → 0.15          (apply HG's win to VF, conservatively)
  P6.D  VF soft_cap 200 → 150         (faster VF shade ramp)
  P6.E  HG asymm_skew 1.0 → 1.3       (long-side stronger; stuck always long)
  P6.F  HG vel_coef 0.20 → 0.10       (gentler trend chase, steadier)

Run:
    python3 phase6_p4c_extensions.py
    python3 phase6_p4c_extensions.py P6.A P6.B
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import research_loop as rl

HERE = Path(__file__).parent
RESULTS_FILE = HERE / "phase6_results.json"

# Reference: P4.C (current best baseline)
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

    hgA = dict(best_hg); hgA["k_inv"] = 1.5
    probes.append(("p6a_hg_k_inv_1p5", "HG k_inv 1.0 → 1.5 (extend winning axis)", hgA, best_vf))

    hgB = dict(best_hg); hgB["soft_cap"] = 100
    probes.append(("p6b_hg_soft_cap_100", "HG soft_cap 150 → 100 (faster shade ramp)", hgB, best_vf))

    vfC = dict(best_vf); vfC["k_inv"] = 0.15
    probes.append(("p6c_vf_k_inv_0p15", "VF k_inv 0.08 → 0.15 (apply HG's fix to VF)", best_hg, vfC))

    vfD = dict(best_vf); vfD["soft_cap"] = 150
    probes.append(("p6d_vf_soft_cap_150", "VF soft_cap 200 → 150", best_hg, vfD))

    hgE = dict(best_hg); hgE["asymm_skew"] = 1.3
    probes.append(("p6e_hg_asymm_skew_1p3", "HG asymm_skew 1.0 → 1.3 (long > short shade)", hgE, best_vf))

    hgF = dict(best_hg); hgF["vel_coef"] = 0.10
    probes.append(("p6f_hg_vel_coef_0p10", "HG vel_coef 0.20 → 0.10 (gentler trend chase)", hgF, best_vf))

    return probes


def load_existing_results() -> dict:
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text())
    return {"started_at": datetime.now().isoformat(),
            "baseline": {"pnl": BASE_PNL, "dd": BASE_DD, "score": BASE_SCORE,
                         "tag": "p4c_hg_k_inv_1p0"},
            "results": {}}


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

    rl.logmsg(f"=== Phase 6 — {len(probes)} probes off P4.C ===")
    rl.logmsg(f"Baseline P4.C: pnl=+{BASE_PNL} dd={BASE_DD} score=+{BASE_SCORE:.0f}")

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
                rl.logmsg(f"  upload FAILED")
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

    rl.logmsg("\n=== Phase 6 results ===")
    rl.logmsg(f"  P4.C baseline: pnl=+{BASE_PNL} dd={BASE_DD} score=+{BASE_SCORE:.0f}")
    winners = []
    for vid, _, _, _ in probes:
        r = results["results"].get(vid, {})
        if r.get("rejected_local"):
            rl.logmsg(f"  {vid:<28} REJECTED: {r.get('reason','')}")
        elif r.get("upload_failed"):
            rl.logmsg(f"  {vid:<28} upload failed")
        elif r.get("asset_pnl") is not None:
            pnl = r["asset_pnl"]; dd = r["max_drawdown"]; sc = r["score"]
            d_pnl = pnl - BASE_PNL; d_dd = dd - BASE_DD; d_sc = sc - BASE_SCORE
            tag = "  ✓" if d_sc > 50 else ("  ✗" if d_sc < -50 else "  ·")
            rl.logmsg(f"  {vid:<28} pnl={pnl:>+7.0f} dd={dd:>5.0f} score={sc:>+7.0f}  "
                      f"Δpnl={d_pnl:+5.0f} Δdd={d_dd:+5.0f} Δscore={d_sc:+5.0f}{tag}")
            if d_sc > 50: winners.append((vid, sc))
    if winners:
        rl.logmsg(f"\n  WINNERS (Δscore > +50):")
        for v, s in sorted(winners, key=lambda x: -x[1]):
            rl.logmsg(f"    {v}: score={s:+.0f}")


if __name__ == "__main__":
    main()
