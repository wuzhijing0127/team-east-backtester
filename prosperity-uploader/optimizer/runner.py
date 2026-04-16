#!/usr/bin/env python3
"""Master orchestration — CLI entry point for automated optimization."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Optional

# Add parent dir for sibling imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from auth import TokenProvider
from client import APIClient
from config import Config
from exceptions import AuthenticationError
from main import run_single, setup_logging
from models import SummaryMetrics
from storage import Database
from utils import save_json, timestamp_slug

from optimizer.adaptive import neighborhood_search, optuna_search
from optimizer.analysis import sensitivity_report, suggest_next_grid, top_configs_report
from optimizer.codegen import DEFAULTS, SEARCH_PARAMS
from optimizer.codegen_180750 import (
    DEFAULTS_180750,
    SEARCH_PARAMS_180750,
    generate_strategy_180750,
    merge_params_180750,
    params_to_name_180750,
)
from optimizer.grid import grid_search, parse_grid_arg

logger = logging.getLogger("optimizer")


def _make_run_fn(
    client: APIClient,
    config: Config,
    db: Database,
) -> callable:
    """Create the upload function that grid/adaptive search will call for each strategy."""

    def run_fn(file_path: str) -> Optional[SummaryMetrics]:
        try:
            return run_single(
                client, config, db,
                file_path,
                force_upload=True,
            )
        except AuthenticationError:
            logger.error(
                "Token expired! Refresh your token and restart with --resume. "
                "Results so far are saved."
            )
            return None

    return run_fn


def _load_prior_results(db: Database) -> list[tuple[dict[str, Any], Any]]:
    """Load previous results from SQLite as warm-start data.

    Returns list of (params_override_dict, SummaryMetrics) tuples.
    We reconstruct partial override dicts from strategy names where possible.
    """
    rows = db.get_leaderboard(limit=500)
    results = []
    for row in rows:
        # Create a minimal SummaryMetrics-like object with just the fields we need
        summary = _row_to_summary(row)
        # We don't have the exact param overrides from DB, but we store
        # the strategy name which encodes params. Use empty overrides for now.
        results.append(({}, summary))
    return results


def _row_to_summary(row: dict) -> SummaryMetrics:
    return SummaryMetrics(
        strategy_name=row.get("strategy_name", ""),
        submission_id=row.get("submission_id", ""),
        final_pnl=row.get("final_pnl") or 0.0,
        max_pnl=row.get("max_pnl") or 0.0,
        min_pnl=row.get("min_pnl") or 0.0,
        max_drawdown=row.get("max_drawdown") or 0.0,
        pnl_slope=row.get("pnl_slope") or 0.0,
        num_points=row.get("num_points") or 0,
    )


def cmd_grid(args: argparse.Namespace, run_fn, db: Database) -> None:
    """Run grid search."""
    param_grid = {}
    for grid_arg in args.grid:
        name, values = parse_grid_arg(grid_arg)
        param_grid[name] = values

    results = grid_search(
        run_fn,
        param_grid=param_grid,
        output_dir=args.output_dir,
        upload_interval=args.interval,
    )

    # Save results
    _save_results(results, "grid", args.output_dir)

    # Print analysis
    if len(results) >= 3:
        print("\n" + sensitivity_report(results))
        suggested = suggest_next_grid(results)
        if suggested:
            print("\nSuggested next grid:")
            for k, v in suggested.items():
                print(f"  {k}: {v}")


def cmd_adaptive(args: argparse.Namespace, run_fn, db: Database) -> None:
    """Run adaptive neighborhood search."""
    prior = _load_prior_results(db) if args.resume else []

    results = neighborhood_search(
        run_fn,
        results_so_far=prior,
        search_params=args.params.split(",") if args.params else None,
        num_rounds=args.rounds,
        candidates_per_round=args.candidates,
        output_dir=args.output_dir,
        upload_interval=args.interval,
    )

    _save_results(results, "adaptive", args.output_dir)

    if len(results) >= 3:
        print("\n" + sensitivity_report(results))


def cmd_optuna(args: argparse.Namespace, run_fn, db: Database) -> None:
    """Run Optuna Bayesian optimization."""
    param_names = args.params.split(",") if args.params else list(SEARCH_PARAMS.keys())[:5]
    prior = _load_prior_results(db) if args.resume else []

    results = optuna_search(
        run_fn,
        param_names=param_names,
        n_trials=args.trials,
        output_dir=args.output_dir,
        upload_interval=args.interval,
        warm_start_results=prior if prior else None,
    )

    _save_results(results, "optuna", args.output_dir)


def cmd_analyze(args: argparse.Namespace, run_fn, db: Database) -> None:
    """Analyze stored results without uploading."""
    rows = db.get_leaderboard(limit=args.top)
    results = [({}, _row_to_summary(r)) for r in rows]

    print(top_configs_report(results, limit=args.top))

    if len(results) >= 3:
        print("\n" + sensitivity_report(results))
        suggested = suggest_next_grid(results)
        if suggested:
            print("\nSuggested next grid:")
            for k, v in suggested.items():
                print(f"  {k}: {v}")


def _save_results(
    results: list[tuple[dict, Any]],
    mode: str,
    output_dir: str,
) -> None:
    """Save optimization results to JSON."""
    if not results:
        return

    out = []
    for overrides, summary in results:
        out.append({
            "params": overrides,
            "final_pnl": summary.final_pnl,
            "max_drawdown": summary.max_drawdown,
            "pnl_slope": summary.pnl_slope,
            "submission_id": summary.submission_id,
            "strategy_name": summary.strategy_name,
        })

    path = Path(output_dir) / f"{mode}_{timestamp_slug()}.json"
    save_json(out, path)
    logger.info("Results saved to %s", path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="optimizer",
        description="Automated strategy parameter optimization for IMC Prosperity.",
    )
    parser.add_argument("--token", type=str, help="Bearer token")
    parser.add_argument("--config", type=str, help="Config YAML path")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--output-dir", type=str, default="generated")
    parser.add_argument("--interval", type=float, default=30.0,
                        help="Seconds between uploads (default: 30)")
    parser.add_argument("--round-id", type=int, help="Competition round ID")

    sub = parser.add_subparsers(dest="command", required=True)

    # grid
    p_grid = sub.add_parser("grid", help="Grid search over parameter combinations")
    p_grid.add_argument("grid", nargs="+",
                        help="Grid specs: 'ash_L1_size=10,14,18' 'ash_k_inv=2.0,2.5,3.0'")

    # adaptive
    p_adapt = sub.add_parser("adaptive", help="Self-improving neighborhood search")
    p_adapt.add_argument("--rounds", type=int, default=5, help="Number of search rounds")
    p_adapt.add_argument("--candidates", type=int, default=4, help="Candidates per round")
    p_adapt.add_argument("--params", type=str,
                         help="Comma-separated params to optimize (default: all)")
    p_adapt.add_argument("--resume", action="store_true",
                         help="Warm-start from previous results")

    # optuna
    p_optuna = sub.add_parser("optuna", help="Bayesian optimization via Optuna")
    p_optuna.add_argument("--trials", type=int, default=50, help="Number of trials")
    p_optuna.add_argument("--params", type=str,
                          help="Comma-separated params to optimize")
    p_optuna.add_argument("--resume", action="store_true",
                          help="Warm-start from previous results")

    # analyze
    p_analyze = sub.add_parser("analyze", help="Analyze stored results (no uploads)")
    p_analyze.add_argument("--top", type=int, default=20)

    # list-params
    sub.add_parser("list-params", help="Show all searchable parameters and their ranges")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    setup_logging(debug=args.debug)

    # list-params doesn't need auth
    if args.command == "list-params":
        print(f"{'Parameter':<30}{'Default':>10}{'Type':>8}  Range")
        print("-" * 80)
        for name, info in sorted(SEARCH_PARAMS.items()):
            print(
                f"{name:<30}{info['default']:>10}  {info['type']:>6}  {info['range']}"
            )
        return

    # Load config
    cli_overrides: dict[str, Any] = {}
    if args.token:
        cli_overrides["bearer_token"] = args.token
    if args.round_id:
        cli_overrides["round_id"] = args.round_id

    config = Config.load(config_path=args.config, cli_overrides=cli_overrides)

    # Analyze doesn't need auth
    if args.command == "analyze":
        db = Database(config)
        cmd_analyze(args, None, db)
        db.close()
        return

    # All other commands need auth
    config.validate()
    config = Config.load(
        config_path=args.config,
        cli_overrides={**cli_overrides, "max_retries": 10, "retry_backoff_max": 120.0},
    )

    token_provider = TokenProvider(config.bearer_token)
    client = APIClient(config, token_provider)
    db = Database(config)

    run_fn = _make_run_fn(client, config, db)

    try:
        if args.command == "grid":
            cmd_grid(args, run_fn, db)
        elif args.command == "adaptive":
            cmd_adaptive(args, run_fn, db)
        elif args.command == "optuna":
            cmd_optuna(args, run_fn, db)
    except KeyboardInterrupt:
        logger.info("Interrupted. Results so far are saved. Use --resume to continue.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
