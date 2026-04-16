"""Staged search framework — coarse random → local refinement → robustness.

Composite scoring: not just PnL, but drawdown/inventory/stability weighted.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional

from optimizer.ash_config import (
    ASHConfig,
    PARAM_RANGES,
    config_diff,
    latin_hypercube_configs,
    perturb_config,
    random_config,
)
from optimizer.codegen_ash_v2 import generate_ash_v2
from utils import save_json, timestamp_slug

logger = logging.getLogger(__name__)


# ── Composite scoring ─────────────────────────────────────────

@dataclass
class RunResult:
    """Full result from one strategy evaluation."""
    config: ASHConfig
    config_diff: dict[str, Any]
    final_pnl: float = 0.0
    max_drawdown: float = 0.0
    pnl_slope: float = 0.0
    pnl_volatility: float = 0.0
    num_points: int = 0
    score: float = 0.0  # composite score

    def to_dict(self) -> dict:
        d = asdict(self)
        d["config"] = self.config.to_dict()
        return d


def composite_score(
    final_pnl: float,
    max_drawdown: float,
    pnl_volatility: float,
    *,
    pnl_weight: float = 1.0,
    drawdown_weight: float = 0.5,
    volatility_weight: float = 0.1,
) -> float:
    """Weighted score that penalizes drawdown and volatility.

    score = pnl_weight * final_pnl
            - drawdown_weight * max_drawdown
            - volatility_weight * pnl_volatility
    """
    return (
        pnl_weight * final_pnl
        - drawdown_weight * max_drawdown
        - volatility_weight * pnl_volatility
    )


# ── Stage 1: Coarse random search ─────────────────────────────

def coarse_search(
    run_fn,  # callable(file_path: str) -> Optional[SummaryMetrics]
    n_configs: int = 200,
    output_dir: str | Path = "generated",
    upload_interval: float = 20.0,
    method: str = "lhc",  # "lhc" or "random"
    seed: int = 42,
) -> list[RunResult]:
    """Stage 1: broad exploration with random or LHC sampling."""

    if method == "lhc":
        configs = latin_hypercube_configs(n_configs, seed=seed)
        logger.info("Stage 1: Latin Hypercube sampling, %d configs", n_configs)
    else:
        configs = [random_config(seed=seed + i) for i in range(n_configs)]
        logger.info("Stage 1: Random sampling, %d configs", n_configs)

    return _evaluate_configs(run_fn, configs, output_dir, upload_interval, "coarse")


def _evaluate_configs(
    run_fn,
    configs: list[ASHConfig],
    output_dir: str | Path,
    upload_interval: float,
    stage_name: str,
) -> list[RunResult]:
    """Evaluate a list of configs and return scored results."""
    total = len(configs)
    results: list[RunResult] = []

    for i, config in enumerate(configs, 1):
        diff = config_diff(config)
        diff_str = ", ".join(f"{k}={v}" for k, v in list(diff.items())[:4])
        logger.info("[%s %d/%d] %s", stage_name, i, total, diff_str or "defaults")

        file_path = generate_ash_v2(config, output_dir=output_dir)
        summary = run_fn(str(file_path))

        if summary:
            score = composite_score(
                summary.final_pnl,
                summary.max_drawdown,
                summary.pnl_volatility,
            )
            result = RunResult(
                config=config,
                config_diff=diff,
                final_pnl=summary.final_pnl,
                max_drawdown=summary.max_drawdown,
                pnl_slope=summary.pnl_slope,
                pnl_volatility=summary.pnl_volatility,
                num_points=summary.num_points,
                score=score,
            )
            results.append(result)
            logger.info(
                "[%s %d/%d] PnL=%.1f DD=%.1f Score=%.1f (%s)",
                stage_name, i, total,
                summary.final_pnl, summary.max_drawdown, score, diff_str,
            )
        else:
            logger.warning("[%s %d/%d] FAILED", stage_name, i, total)

        if i < total:
            time.sleep(upload_interval)

    results.sort(key=lambda r: r.score, reverse=True)
    return results


# ── Stage 2: Local refinement ─────────────────────────────────

def local_refinement(
    run_fn,
    top_results: list[RunResult],
    top_k: int = 10,
    neighbors_per_config: int = 5,
    n_rounds: int = 3,
    output_dir: str | Path = "generated",
    upload_interval: float = 20.0,
) -> list[RunResult]:
    """Stage 2: perturb top configs from coarse search."""

    all_results = list(top_results)
    logger.info(
        "Stage 2: Local refinement — %d rounds, %d neighbors per top-%d config",
        n_rounds, neighbors_per_config, top_k,
    )

    seen_hashes: set[str] = {str(r.config.to_dict()) for r in all_results}

    for rnd in range(1, n_rounds + 1):
        all_results.sort(key=lambda r: r.score, reverse=True)
        seeds = all_results[:top_k]

        logger.info(
            "Round %d: top score=%.1f (PnL=%.1f)",
            rnd, seeds[0].score, seeds[0].final_pnl,
        )

        new_configs: list[ASHConfig] = []
        for result in seeds:
            for j in range(neighbors_per_config):
                neighbor = perturb_config(result.config, n_changes=2, seed=rnd * 1000 + j)
                h = str(neighbor.to_dict())
                if h not in seen_hashes:
                    new_configs.append(neighbor)
                    seen_hashes.add(h)

        if not new_configs:
            logger.info("No new neighbors to try — converged")
            break

        logger.info("Round %d: testing %d new neighbors", rnd, len(new_configs))
        round_results = _evaluate_configs(
            run_fn, new_configs, output_dir, upload_interval, f"refine_r{rnd}"
        )
        all_results.extend(round_results)

    all_results.sort(key=lambda r: r.score, reverse=True)
    return all_results


# ── Stage 3: Robustness check ─────────────────────────────────
# (Placeholder — runs the same config multiple times to check consistency)

def robustness_check(
    run_fn,
    config: ASHConfig,
    n_runs: int = 3,
    output_dir: str | Path = "generated",
    upload_interval: float = 20.0,
) -> list[RunResult]:
    """Stage 3: run the same config multiple times to check stability."""
    logger.info("Stage 3: Robustness check — %d runs of best config", n_runs)

    results = []
    for i in range(n_runs):
        logger.info("[robustness %d/%d]", i + 1, n_runs)
        file_path = generate_ash_v2(
            config,
            name=f"robustness_{i}",
            output_dir=output_dir,
        )
        summary = run_fn(str(file_path))
        if summary:
            score = composite_score(
                summary.final_pnl, summary.max_drawdown, summary.pnl_volatility,
            )
            results.append(RunResult(
                config=config,
                config_diff=config_diff(config),
                final_pnl=summary.final_pnl,
                max_drawdown=summary.max_drawdown,
                pnl_slope=summary.pnl_slope,
                pnl_volatility=summary.pnl_volatility,
                num_points=summary.num_points,
                score=score,
            ))
        time.sleep(upload_interval)

    if results:
        pnls = [r.final_pnl for r in results]
        mean_pnl = sum(pnls) / len(pnls)
        std_pnl = (sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls)) ** 0.5
        logger.info(
            "Robustness: mean PnL=%.1f, std=%.1f, min=%.1f, max=%.1f",
            mean_pnl, std_pnl, min(pnls), max(pnls),
        )

    return results


# ── Full pipeline ─────────────────────────────────────────────

def run_staged_search(
    run_fn,
    n_coarse: int = 200,
    n_refine_rounds: int = 3,
    n_refine_neighbors: int = 5,
    top_k: int = 10,
    n_robustness: int = 3,
    output_dir: str | Path = "generated",
    upload_interval: float = 20.0,
    method: str = "lhc",
    seed: int = 42,
) -> list[RunResult]:
    """Run the full 3-stage optimization pipeline."""

    # Stage 1
    coarse_results = coarse_search(
        run_fn, n_configs=n_coarse, output_dir=output_dir,
        upload_interval=upload_interval, method=method, seed=seed,
    )
    _print_leaderboard("STAGE 1: COARSE SEARCH", coarse_results)
    _save_stage("coarse", coarse_results, output_dir)

    if not coarse_results:
        return []

    # Stage 2
    refined_results = local_refinement(
        run_fn, coarse_results, top_k=top_k,
        neighbors_per_config=n_refine_neighbors, n_rounds=n_refine_rounds,
        output_dir=output_dir, upload_interval=upload_interval,
    )
    _print_leaderboard("STAGE 2: LOCAL REFINEMENT", refined_results)
    _save_stage("refined", refined_results, output_dir)

    # Stage 3
    best_config = refined_results[0].config
    robust_results = robustness_check(
        run_fn, best_config, n_runs=n_robustness,
        output_dir=output_dir, upload_interval=upload_interval,
    )
    _print_leaderboard("STAGE 3: ROBUSTNESS CHECK", robust_results)
    _save_stage("robustness", robust_results, output_dir)

    # Final report
    print("\n" + "=" * 80)
    print("BEST CONFIG")
    print("=" * 80)
    diff = config_diff(best_config)
    for k, v in sorted(diff.items()):
        print(f"  {k}: {v}")
    print(f"\n  Score: {refined_results[0].score:.1f}")
    print(f"  PnL:   {refined_results[0].final_pnl:.1f}")
    print(f"  MaxDD: {refined_results[0].max_drawdown:.1f}")
    print("=" * 80)

    save_json(best_config.to_dict(), Path(output_dir) / "best_config.json")
    return refined_results


def _print_leaderboard(title: str, results: list[RunResult], limit: int = 15) -> None:
    if not results:
        return
    print(f"\n{'=' * 90}")
    print(title)
    print(f"{'=' * 90}")
    print(f"{'Rank':<5}{'Score':>10}{'PnL':>12}{'MaxDD':>10}{'Vol':>10}  Config diff")
    print("-" * 90)
    for rank, r in enumerate(results[:limit], 1):
        diff = ", ".join(f"{k}={v}" for k, v in list(r.config_diff.items())[:4])
        print(
            f"{rank:<5}{r.score:>10,.1f}{r.final_pnl:>12,.1f}"
            f"{r.max_drawdown:>10,.1f}{r.pnl_volatility:>10,.1f}  {diff or 'defaults'}"
        )
    print(f"{'=' * 90}")


def _save_stage(name: str, results: list[RunResult], output_dir: str | Path) -> None:
    out = [r.to_dict() for r in results]
    path = Path(output_dir) / f"stage_{name}_{timestamp_slug()}.json"
    save_json(out, path)
    logger.info("Saved %d results to %s", len(results), path)
