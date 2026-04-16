"""Adaptive search — self-improving parameter optimization."""

from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Any, Optional

from optimizer.codegen import DEFAULTS, SEARCH_PARAMS, generate_strategy, merge_params, params_to_name

logger = logging.getLogger(__name__)


def neighborhood_search(
    run_fn,  # callable(file_path: str) -> Optional[SummaryMetrics]
    results_so_far: list[tuple[dict[str, Any], Any]],
    search_params: Optional[list[str]] = None,
    num_rounds: int = 5,
    candidates_per_round: int = 4,
    top_k: int = 3,
    output_dir: str | Path = "generated",
    upload_interval: float = 30.0,
) -> list[tuple[dict[str, Any], Any]]:
    """Self-improving search: perturb top configs, upload, learn, repeat.

    Args:
        run_fn: Callable that uploads and returns SummaryMetrics.
        results_so_far: Previous (params, summary) tuples for warm start.
        search_params: Which params to perturb. Defaults to all in SEARCH_PARAMS.
        num_rounds: Number of search rounds.
        candidates_per_round: How many variants to try per round.
        top_k: Use top K results as seeds each round.
        output_dir: Where to write generated files.
        upload_interval: Seconds between uploads.

    Returns:
        All results (including warm start), sorted by PnL.
    """
    all_results = list(results_so_far)
    params_to_search = search_params or list(SEARCH_PARAMS.keys())

    # Track which param combos we've already tested (by hash of overrides)
    seen = {_params_hash(overrides) for overrides, _ in all_results}

    for rnd in range(1, num_rounds + 1):
        logger.info("=== Adaptive Round %d/%d ===", rnd, num_rounds)

        # Sort and pick top K seeds
        all_results.sort(key=lambda r: r[1].final_pnl, reverse=True)
        seeds = all_results[:top_k] if all_results else [(dict(DEFAULTS), None)]

        if all_results:
            logger.info(
                "Top %d seeds: %s",
                min(top_k, len(all_results)),
                [(f"PnL={s.final_pnl:.0f}" if s else "baseline") for _, s in seeds[:top_k]],
            )

        # Generate candidates by perturbing seeds
        candidates: list[dict[str, Any]] = []
        for seed_overrides, _ in seeds:
            for _ in range(candidates_per_round):
                neighbor = _perturb(seed_overrides, params_to_search)
                h = _params_hash(neighbor)
                if h not in seen:
                    candidates.append(neighbor)
                    seen.add(h)

        if not candidates:
            logger.info("No new candidates to try — search space exhausted")
            break

        logger.info("Round %d: %d new candidates", rnd, len(candidates))

        # Upload each candidate
        for i, overrides in enumerate(candidates, 1):
            params = merge_params(overrides)
            name = params_to_name(params)
            diff_str = ", ".join(f"{k}={v}" for k, v in overrides.items() if v != DEFAULTS.get(k))

            logger.info("[R%d %d/%d] %s", rnd, i, len(candidates), diff_str)

            file_path = generate_strategy(params, name=name, output_dir=output_dir)
            summary = run_fn(str(file_path))

            if summary:
                all_results.append((overrides, summary))
                logger.info(
                    "[R%d %d/%d] PnL=%.2f (%s)",
                    rnd, i, len(candidates), summary.final_pnl, diff_str,
                )
            else:
                logger.warning("[R%d %d/%d] FAILED", rnd, i, len(candidates))

            if i < len(candidates):
                time.sleep(upload_interval)

        # Print round summary
        all_results.sort(key=lambda r: r[1].final_pnl, reverse=True)
        best = all_results[0]
        logger.info(
            "Round %d complete. Best so far: PnL=%.2f",
            rnd, best[1].final_pnl,
        )

    # Final leaderboard
    all_results.sort(key=lambda r: r[1].final_pnl, reverse=True)
    if all_results:
        print("\n" + "=" * 80)
        print("ADAPTIVE SEARCH RESULTS")
        print("=" * 80)
        print(f"{'Rank':<5}{'PnL':>12}{'MaxDD':>10}  Params")
        print("-" * 80)
        for rank, (overrides, s) in enumerate(all_results[:20], 1):
            diff = ", ".join(f"{k}={v}" for k, v in overrides.items() if v != DEFAULTS.get(k))
            print(f"{rank:<5}{s.final_pnl:>12,.2f}{s.max_drawdown:>10,.2f}  {diff or 'defaults'}")
        print("=" * 80)

    return all_results


def _perturb(
    overrides: dict[str, Any],
    params_to_search: list[str],
    num_changes: int = 2,
) -> dict[str, Any]:
    """Create a neighbor by randomly perturbing 1-2 parameters."""
    result = dict(overrides)

    # Pick which params to change
    to_change = random.sample(
        params_to_search,
        min(num_changes, len(params_to_search)),
    )

    for param in to_change:
        info = SEARCH_PARAMS[param]
        values = info["range"]
        current = result.get(param, info["default"])

        # Find current position in range and step ±1
        try:
            idx = values.index(current)
        except ValueError:
            # Current value not in range — pick randomly
            result[param] = random.choice(values)
            continue

        # Step up or down with equal probability
        if idx == 0:
            new_idx = 1
        elif idx == len(values) - 1:
            new_idx = idx - 1
        else:
            new_idx = idx + random.choice([-1, 1])

        result[param] = values[new_idx]

    return result


def _params_hash(overrides: dict[str, Any]) -> str:
    """Hash a parameter override dict for deduplication."""
    items = sorted(overrides.items())
    return str(items)


def optuna_search(
    run_fn,
    param_names: list[str],
    n_trials: int = 50,
    output_dir: str | Path = "generated",
    upload_interval: float = 30.0,
    warm_start_results: Optional[list[tuple[dict, Any]]] = None,
) -> list[tuple[dict[str, Any], Any]]:
    """Bayesian optimization using Optuna's TPE sampler.

    Requires: pip install optuna
    """
    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
    except ImportError:
        raise ImportError("optuna is required for Bayesian search: pip install optuna")

    all_results: list[tuple[dict[str, Any], Any]] = []

    def objective(trial: optuna.Trial) -> float:
        overrides: dict[str, Any] = {}
        for name in param_names:
            info = SEARCH_PARAMS[name]
            values = info["range"]
            if info["type"] == "int":
                overrides[name] = trial.suggest_int(name, min(values), max(values))
            else:
                overrides[name] = trial.suggest_float(name, min(values), max(values))

        params = merge_params(overrides)
        pname = params_to_name(params)
        file_path = generate_strategy(params, name=pname, output_dir=output_dir)

        summary = run_fn(str(file_path))
        if summary is None:
            return float("-inf")

        all_results.append((overrides, summary))
        time.sleep(upload_interval)
        return summary.final_pnl

    study = optuna.create_study(direction="maximize")

    # Warm-start with known results
    if warm_start_results:
        for overrides, summary in warm_start_results:
            if summary is not None:
                distributions = {}
                params_dict = {}
                for name in param_names:
                    info = SEARCH_PARAMS[name]
                    values = info["range"]
                    val = overrides.get(name, info["default"])
                    if info["type"] == "int":
                        distributions[name] = optuna.distributions.IntDistribution(min(values), max(values))
                        params_dict[name] = int(val)
                    else:
                        distributions[name] = optuna.distributions.FloatDistribution(min(values), max(values))
                        params_dict[name] = float(val)
                trial = optuna.trial.create_trial(
                    params=params_dict,
                    distributions=distributions,
                    values=[summary.final_pnl],
                )
                study.add_trial(trial)
        logger.info("Warm-started Optuna with %d prior results", len(warm_start_results))

    study.optimize(objective, n_trials=n_trials)

    all_results.sort(key=lambda r: r[1].final_pnl, reverse=True)
    return all_results
