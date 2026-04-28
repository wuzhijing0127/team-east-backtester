#!/usr/bin/env python3
"""
Alpha sweep on r4_momentum_v1 base — find best critical alpha values.

Single-axis perturbations on momentum_v1's alpha params. Discipline (target_pos,
asymmetric quoting, k_relax) inherited from momentum_v1 unchanged.

Probes:
  Baseline = momentum_v1 alpha
  HG ema_alpha: 0.05 -> 0.0, 0.10, 0.20
  HG trend_coef: 0.30 -> 0.20, 0.45, 0.60
  HG imb_coef: 0.40 -> 0.0, 0.20
  HG micro_coef: 0.50 -> 0.30, 0.70
  HG vel_coef: 0.20 -> 0.10, 0.30
  VFE ema_alpha: 0.0 -> 0.05, 0.10
  VFE trend_coef: 0.15 -> 0.0, 0.30
  VFE imb_coef: 0.30 -> 0.0
  VFE micro_coef: 0.20 -> 0.40

Each upload = ~2 min. Total ~50 min for ~24 probes.
"""

from __future__ import annotations
import argparse, json, time
from datetime import datetime
from pathlib import Path
import research_loop as rl

HERE = Path(__file__).parent
RESULTS = HERE / "alpha_sweep_momentum_results.json"

# Baseline = momentum_v1 (HG with discipline ON, VFE with discipline ON but pos_gap_threshold high)
def base_hg():
    return {
        "limit": 200, "soft_cap": 150, "half_spread": 4,
        "base_size": 20, "quote_size": 20, "take_size": 20,
        "take_edge": 8, "k_inv": 1.0,
        "micro_coef": 0.50, "imb_coef": 0.40, "ema_alpha": 0.05,
        "trend_coef": 0.30, "trend_lag": 5,
        "vel_coef": 0.20, "vel_alpha": 0.10,
        # Discipline from momentum_v1
        "target_pos_scaler": 10.0, "alpha_threshold": 1.5,
        "alpha_take_size": 50, "alpha_quote_size": 40, "mm_size_reduced": 5,
        "k_relax_coef": 0.5, "pos_gap_threshold": 10,
        # New-engine fields (Step 4-9 inert defaults)
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


def variants():
    bh, bv = base_hg(), base_vf()
    out = [("baseline_mom", "momentum_v1 baseline", bh, bv)]

    # HG alpha sweep
    for ea in [0.0, 0.10, 0.20]:
        h = dict(bh); h["ema_alpha"] = ea
        out.append((f"hg_ea_{str(ea).replace('.','p')}", f"HG ema_alpha 0.05→{ea}", h, bv))
    for tc in [0.20, 0.45, 0.60]:
        h = dict(bh); h["trend_coef"] = tc
        out.append((f"hg_tc_{str(tc).replace('.','p')}", f"HG trend_coef 0.30→{tc}", h, bv))
    for ic in [0.0, 0.20]:
        h = dict(bh); h["imb_coef"] = ic
        out.append((f"hg_ic_{str(ic).replace('.','p')}", f"HG imb_coef 0.40→{ic}", h, bv))
    for mc in [0.30, 0.70]:
        h = dict(bh); h["micro_coef"] = mc
        out.append((f"hg_mc_{str(mc).replace('.','p')}", f"HG micro_coef 0.50→{mc}", h, bv))
    for vc in [0.10, 0.30]:
        h = dict(bh); h["vel_coef"] = vc
        out.append((f"hg_vc_{str(vc).replace('.','p')}", f"HG vel_coef 0.20→{vc}", h, bv))

    # VFE alpha sweep
    for ea in [0.05, 0.10]:
        v = dict(bv); v["ema_alpha"] = ea
        out.append((f"vf_ea_{str(ea).replace('.','p')}", f"VFE ema_alpha 0.0→{ea}", bh, v))
    for tc in [0.0, 0.30]:
        v = dict(bv); v["trend_coef"] = tc
        out.append((f"vf_tc_{str(tc).replace('.','p')}", f"VFE trend_coef 0.15→{tc}", bh, v))
    for ic in [0.0]:
        v = dict(bv); v["imb_coef"] = ic
        out.append((f"vf_ic_{str(ic).replace('.','p')}", f"VFE imb_coef 0.30→{ic}", bh, v))
    for mc in [0.40]:
        v = dict(bv); v["micro_coef"] = mc
        out.append((f"vf_mc_{str(mc).replace('.','p')}", f"VFE micro_coef 0.20→{mc}", bh, v))
    return out


def load_results():
    if RESULTS.exists():
        return json.loads(RESULTS.read_text())
    return {"started_at": datetime.now().isoformat(), "results": {}}


def save_results(d):
    RESULTS.write_text(json.dumps(d, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inter-upload-delay", type=int, default=15)
    ap.add_argument("--rate-limit-backoff", type=int, default=60)
    args = ap.parse_args()

    probes = variants()
    rl.logmsg(f"=== alpha_sweep on momentum_v1 — {len(probes)} probes ===")
    results = load_results()

    for i, (vid, label, hg, vf) in enumerate(probes, 1):
        if vid in results["results"]:
            rl.logmsg(f"[{i}/{len(probes)}] {vid} — already tested, skipping")
            continue
        rl.logmsg(f"\n[{i}/{len(probes)}] {vid}: {label}")
        path = rl.write_variant(vid, label, hg, vf)
        ok, msg = rl.local_sim_3day(path)
        if not ok:
            rl.logmsg(f"  guardrail FAIL: {msg}")
            results["results"][vid] = {"variant_id": vid, "label": label,
                                        "rejected_local": True, "reason": msg}
            save_results(results); continue
        rl.logmsg(f"  guardrail: {msg}")

        while True:
            res = rl.upload_and_get_pnl(path)
            if res is None:
                rl.logmsg(f"  upload FAILED")
                results["results"][vid] = {"variant_id": vid, "label": label,
                                            "upload_failed": True}
                save_results(results); break
            if res.get("rate_limited"):
                rl.logmsg(f"  rate limited; sleeping {args.rate_limit_backoff}s")
                time.sleep(args.rate_limit_backoff); continue
            pnl = res["asset_pnl"]; dd = res.get("max_drawdown") or 0
            score = pnl - 0.5 * dd
            rl.logmsg(f"  pnl={pnl:+.0f}  dd={dd:.0f}  score={score:+.0f}")
            results["results"][vid] = {"variant_id": vid, "label": label,
                                        "asset_pnl": pnl, "max_drawdown": dd,
                                        "score": score, "hg": hg, "vf": vf,
                                        "when": datetime.now().isoformat()}
            save_results(results); break

        if i < len(probes):
            time.sleep(args.inter_upload_delay)

    # Summary
    rl.logmsg("\n=== alpha sweep summary ===")
    bl = results["results"].get("baseline_mom", {}).get("score", 0)
    rl.logmsg(f"baseline_mom score: {bl:+.0f}")
    rows = []
    for vid, r in results["results"].items():
        if r.get("score") is not None:
            rows.append((r["score"] - bl, vid, r["score"], r.get("asset_pnl", 0),
                         r.get("max_drawdown", 0)))
    rows.sort(reverse=True)
    rl.logmsg(f"\n{'rank':<5} {'variant':<22} {'score':>8} {'pnl':>7} {'dd':>5}  Δ vs baseline")
    for i, (d, vid, sc, p, dd) in enumerate(rows, 1):
        tag = "✓ WIN" if d > 30 else ("✗ LOSS" if d < -30 else "·")
        rl.logmsg(f"  {i:<3}  {vid:<22} {sc:>+8.0f} {p:>+7.0f} {dd:>5.0f}  Δ={d:+.0f} {tag}")


if __name__ == "__main__":
    main()
