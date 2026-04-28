#!/usr/bin/env python3
"""
Alpha-only search loop.

Per the round-4 simplification directive:
  - Vary ONLY the 7 alpha-signal knobs per asset
  - All inventory/risk/structural knobs are FIXED at sensible defaults
    (HG = P4.C+insurance values; VFE = defensive tetrad values)

The 7 alpha knobs per asset:
  trend_coef     trend strength (NEVER NEGATIVE — round-3 contra-trap lesson)
  trend_lag      momentum lookback ticks
  vel_coef       EMA velocity strength
  vel_alpha      velocity smoothing
  micro_coef     microprice tilt
  imb_coef       L1 imbalance
  ema_alpha      mid smoothing

Each iteration:
  1. Pick one of the 14 (asset × axis) axes at random
  2. Pick a value not yet tried for that axis (current-best + ε)
  3. Local guardrail; if pass, upload
  4. Accept if score > best_score; rollback otherwise
  5. Save state to alpha_state.json (separate from legacy state.json)

Run:
    python3 alpha_search.py --hours 2
    python3 alpha_search.py --max-iters 5    # short test
"""

from __future__ import annotations

import argparse
import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

import research_loop as rl

HERE = Path(__file__).parent
ALPHA_STATE = HERE / "alpha_state.json"
ALPHA_RESULTS = HERE / "alpha_search_results.json"

# ────────── Alpha axes (per asset, varied) ──────────

ALPHA_AXES = {
    "trend_coef":  [0.0, 0.10, 0.20, 0.30, 0.45, 0.60],     # NEVER negative
    "trend_lag":   [3, 5, 8, 12],
    "vel_coef":    [0.0, 0.10, 0.20, 0.30, 0.45],
    "vel_alpha":   [0.05, 0.10, 0.20],
    "micro_coef":  [0.0, 0.20, 0.40, 0.60, 0.80],
    "imb_coef":    [0.0, 0.20, 0.40, 0.60],
    "ema_alpha":   [0.0, 0.05, 0.10, 0.20],
}

# ────────── Fixed structural / risk values (per asset, NEVER varied) ──────────

FIXED_HG = {
    # structural
    "limit": 200, "soft_cap": 150,
    "half_spread": 4, "base_size": 20, "take_edge": 8,
    "k_inv": 1.0,
    # alpha (overridden by ALPHA_AXES)
    "micro_trend_coef": 0.10,
    # insurance
    "stop_pos_threshold": 100,
    "stop_drawdown_threshold": 4000.0,
    "stop_min_ticks_at_pos": 200,
    "stop_unwind_size": 30,
}

FIXED_VF = {
    "limit": 200, "soft_cap": 150,
    "half_spread": 1, "base_size": 15, "take_edge": 2,
    "k_inv": 0.7,
    "micro_trend_coef": 0.0,
    "stop_pos_threshold": 100,
    "stop_drawdown_threshold": 4000.0,
    "stop_min_ticks_at_pos": 200,
    "stop_unwind_size": 30,
}


def make_params(hg_alpha: dict, vf_alpha: dict) -> tuple[dict, dict]:
    """Combine fixed structural values with the variable alpha knobs."""
    hg = dict(FIXED_HG); hg.update(hg_alpha)
    vf = dict(FIXED_VF); vf.update(vf_alpha)
    return hg, vf


def init_alpha_from_seed() -> tuple[dict, dict]:
    """Initial alpha knobs — seeded from prior best where available."""
    state = json.loads((HERE / "state.json").read_text())
    bh, bv = state.get("best_hg", {}), state.get("best_vf", {})

    def pick(d, key, fallback):
        v = d.get(key, fallback)
        # snap to nearest legal value
        if key not in ALPHA_AXES:
            return v
        opts = ALPHA_AXES[key]
        return min(opts, key=lambda x: abs(x - v))

    hg_alpha = {k: pick(bh, k, ALPHA_AXES[k][0]) for k in ALPHA_AXES}
    # VFE was contra-momentum in round 3 — force the seed to a SAFE positive value
    vf_alpha = {k: pick(bv, k, ALPHA_AXES[k][0]) for k in ALPHA_AXES}
    if vf_alpha["trend_coef"] < 0.0:
        vf_alpha["trend_coef"] = 0.20
    return hg_alpha, vf_alpha


def load_state() -> dict:
    if ALPHA_STATE.exists():
        return json.loads(ALPHA_STATE.read_text())
    hg_alpha, vf_alpha = init_alpha_from_seed()
    return {
        "started_at": datetime.now().isoformat(),
        "best_hg_alpha": hg_alpha,
        "best_vf_alpha": vf_alpha,
        "best_pnl": None,
        "best_dd": None,
        "best_score": None,
        "best_variant_id": None,
        "tried_keys": [],
        "history": [],
    }


def save_state(s): ALPHA_STATE.write_text(json.dumps(s, indent=2))


def load_results() -> dict:
    if ALPHA_RESULTS.exists():
        return json.loads(ALPHA_RESULTS.read_text())
    return {"started_at": datetime.now().isoformat(), "results": []}


def save_results(r): ALPHA_RESULTS.write_text(json.dumps(r, indent=2))


def axis_key(asset: str, param: str, value) -> str:
    return f"{asset}.{param}={value}"


def propose(state: dict, rng: random.Random) -> tuple[str, dict, dict, str, str, object]:
    """Pick (asset, param, value) not yet tried. Returns (vid, hg, vf, asset, param, val)."""
    tried = set(state["tried_keys"])
    bh = dict(state["best_hg_alpha"]); bv = dict(state["best_vf_alpha"])

    candidates = []
    for asset_name, base in [("hg", bh), ("vf", bv)]:
        for param, opts in ALPHA_AXES.items():
            cur = base[param]
            for v in opts:
                if v == cur: continue
                k = axis_key(asset_name, param, v)
                if k not in tried:
                    candidates.append((asset_name, param, v))
    if not candidates:
        return None
    asset, param, value = rng.choice(candidates)

    if asset == "hg":
        bh[param] = value
    else:
        bv[param] = value

    val_tag = str(value).replace(".", "p").replace("-", "n")
    vid = f"alpha_{asset}_{param}_{val_tag}"
    label = f"{asset.upper()} {param} → {value}"
    hg, vf = make_params(bh, bv)
    return vid, label, hg, vf, asset, param, value


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--max-iters", type=int, default=200)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--inter-upload-delay", type=int, default=30)
    ap.add_argument("--rate-limit-backoff", type=int, default=60)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    state = load_state()
    results = load_results()

    deadline = datetime.now() + timedelta(hours=args.hours)
    rl.logmsg(f"=== Alpha search — budget {args.hours}h or {args.max_iters} iters ===")
    if state.get("best_pnl") is None:
        rl.logmsg("No baseline yet — first iter will run baseline (no perturbation).")
    else:
        rl.logmsg(f"Best so far: pnl={state['best_pnl']:.0f} dd={state['best_dd']:.0f} score={state['best_score']:.0f}")

    iters = 0
    while datetime.now() < deadline and iters < args.max_iters:
        iters += 1

        # First iteration after fresh state: run BASELINE (no perturbation)
        if state.get("best_pnl") is None:
            vid = "alpha_baseline_v1"
            label = "Alpha baseline — fixed structural + seeded alpha"
            hg, vf = make_params(state["best_hg_alpha"], state["best_vf_alpha"])
            asset = param = value = None
            tried_key = "baseline_v1"
        else:
            proposal = propose(state, rng)
            if proposal is None:
                rl.logmsg("All single-axis perturbations exhausted.")
                break
            vid, label, hg, vf, asset, param, value = proposal
            tried_key = axis_key(asset, param, value)

        rl.logmsg(f"\n[iter {iters}] {vid}: {label}")

        path = rl.write_variant(vid, label, hg, vf)
        ok, msg = rl.local_sim_3day(path)
        if not ok:
            rl.logmsg(f"  REJECTED by guardrail: {msg}")
            state["tried_keys"].append(tried_key); save_state(state)
            continue
        rl.logmsg(f"  guardrail: {msg}")

        # Upload
        retries = 0
        while True:
            res = rl.upload_and_get_pnl(path)
            if res is None:
                rl.logmsg(f"  upload FAILED")
                state["tried_keys"].append(tried_key); save_state(state)
                res = None; break
            if res.get("rate_limited"):
                rl.logmsg(f"  rate limited; sleeping {args.rate_limit_backoff}s")
                time.sleep(args.rate_limit_backoff); continue
            break
        if res is None:
            continue

        pnl = res["asset_pnl"]; dd = res.get("max_drawdown") or 0
        vol = res.get("pnl_volatility") or 0
        score = pnl - 0.5 * dd

        # Add to results
        result = {
            "variant_id": vid, "label": label,
            "asset_pnl": pnl, "max_drawdown": dd, "pnl_volatility": vol,
            "score": score, "hg": hg, "vf": vf,
            "axis": {"asset": asset, "param": param, "value": value},
            "when": datetime.now().isoformat(),
        }
        results["results"].append(result); save_results(results)
        state["tried_keys"].append(tried_key)

        # Compare to best
        is_baseline = state.get("best_pnl") is None
        accepted = False
        if is_baseline:
            accepted = True
            verdict = "BASELINE"
        else:
            d_sc = score - state["best_score"]
            if score > state["best_score"]:
                accepted = True
                verdict = f"ACCEPT  Δscore={d_sc:+.0f}"
            else:
                verdict = f"reject  Δscore={d_sc:+.0f}"

        rl.logmsg(f"  pnl=+{pnl:.0f} dd={dd:.0f} vol={vol:.1f} score=+{score:.0f}  [{verdict}]")

        if accepted:
            state["best_hg_alpha"] = {k: hg[k] for k in ALPHA_AXES}
            state["best_vf_alpha"] = {k: vf[k] for k in ALPHA_AXES}
            state["best_pnl"] = pnl
            state["best_dd"] = dd
            state["best_score"] = score
            state["best_variant_id"] = vid
            state["history"].append({
                "variant_id": vid, "label": label,
                "asset_pnl": pnl, "max_drawdown": dd, "score": score,
                "hg_alpha": dict(state["best_hg_alpha"]),
                "vf_alpha": dict(state["best_vf_alpha"]),
                "when": datetime.now().isoformat(),
            })
        save_state(state)

        time.sleep(args.inter_upload_delay)

    rl.logmsg(f"\n=== Alpha search done. {iters} iters. ===")
    if state.get("best_pnl") is not None:
        rl.logmsg(f"Best: {state['best_variant_id']}  pnl={state['best_pnl']:.0f} dd={state['best_dd']:.0f} score={state['best_score']:.0f}")
        rl.logmsg(f"  HG alpha: {state['best_hg_alpha']}")
        rl.logmsg(f"  VF alpha: {state['best_vf_alpha']}")


if __name__ == "__main__":
    main()
