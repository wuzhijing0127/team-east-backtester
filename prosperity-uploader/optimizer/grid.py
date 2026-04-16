"""Grid search over parameter combinations."""

from __future__ import annotations

import itertools
import logging
import time
from pathlib import Path
from typing import Any, Optional

from optimizer.codegen import DEFAULTS, SEARCH_PARAMS, generate_strategy, merge_params, params_to_name

logger = logging.getLogger(__name__)


def grid_search(
    run_fn,  # callable(file_path: str) -> Optional[SummaryMetrics]
    param_grid: dict[str, list[Any]],
    base_params: Optional[dict[str, Any]] = None,
    output_dir: str | Path = "generated",
    upload_interval: float = 30.0,
    defaults: Optional[dict[str, Any]] = None,
    generate_fn=None,  # callable(params, name, output_dir) -> Path
    name_fn=None,  # callable(params) -> str
    merge_fn=None,  # callable(overrides) -> dict
) -> list[tuple[dict[str, Any], Any]]:
    """Run a grid search over parameter combinations.

    Args:
        run_fn: Callable that takes a file path and returns SummaryMetrics or None.
        param_grid: Dict of param_name → list of values to try.
        base_params: Override defaults for non-grid params.
        output_dir: Where to write generated .py files.
        upload_interval: Seconds to wait between uploads.
        defaults: Default parameter dict (overrides module-level DEFAULTS).
        generate_fn: Strategy file generator (overrides codegen.generate_strategy).
        name_fn: Name generator (overrides codegen.params_to_name).
        merge_fn: Param merge function (overrides codegen.merge_params).

    Returns:
        List of (params_dict, SummaryMetrics) tuples, sorted by final_pnl descending.
    """
    _defaults = defaults or DEFAULTS
    _generate = generate_fn or generate_strategy
    _name = name_fn or params_to_name
    _merge = merge_fn or merge_params

    base = dict(_defaults)
    if base_params:
        base.update(base_params)

    # Validate grid params
    for key in param_grid:
        if key not in _defaults:
            raise ValueError(f"Unknown parameter in grid: {key}")

    # Generate all combinations
    keys = sorted(param_grid.keys())
    value_lists = [param_grid[k] for k in keys]
    combos = list(itertools.product(*value_lists))
    total = len(combos)

    est_minutes = total * (upload_interval + 120) / 60
    logger.info(
        "Grid search: %d combinations over %s (est. %.0f min)",
        total, keys, est_minutes,
    )

    results: list[tuple[dict[str, Any], Any]] = []

    for i, values in enumerate(combos, 1):
        overrides = dict(zip(keys, values))
        params = _merge({**base, **overrides})
        name = _name(params)

        diff_str = ", ".join(f"{k}={v}" for k, v in overrides.items())
        logger.info("[%d/%d] %s", i, total, diff_str)

        # Generate .py file
        file_path = _generate(params, name=name, output_dir=output_dir)

        # Upload + poll + get metrics
        summary = run_fn(str(file_path))

        if summary:
            results.append((overrides, summary))
            logger.info(
                "[%d/%d] PnL=%.2f  MaxDD=%.2f  (%s)",
                i, total, summary.final_pnl, summary.max_drawdown, diff_str,
            )
        else:
            logger.warning("[%d/%d] FAILED (%s)", i, total, diff_str)

        # Wait between uploads (skip after last)
        if i < total:
            logger.info("Waiting %.0fs...", upload_interval)
            time.sleep(upload_interval)

    # Sort by PnL descending
    results.sort(key=lambda r: r[1].final_pnl, reverse=True)

    # Print summary
    if results:
        print("\n" + "=" * 80)
        print("GRID SEARCH RESULTS")
        print("=" * 80)
        print(f"{'Rank':<5}{'PnL':>12}{'MaxDD':>10}{'Slope':>10}  Params")
        print("-" * 80)
        for rank, (overrides, s) in enumerate(results, 1):
            diff = ", ".join(f"{k}={v}" for k, v in overrides.items())
            print(f"{rank:<5}{s.final_pnl:>12,.2f}{s.max_drawdown:>10,.2f}{s.pnl_slope:>10,.4f}  {diff}")
        print("=" * 80)

    return results


def parse_grid_arg(arg: str, search_params: Optional[dict] = None) -> tuple[str, list[Any]]:
    """Parse a CLI grid argument like 'ash_L1_size=10,14,18' into (name, values).

    Auto-detects int vs float based on search_params type info.
    """
    _sp = search_params or SEARCH_PARAMS
    name, vals_str = arg.split("=", 1)
    name = name.strip()

    if name not in _sp:
        raise ValueError(
            f"Unknown search param: {name}. "
            f"Available: {', '.join(sorted(_sp.keys()))}"
        )

    ptype = _sp[name].get("type", "float")
    raw_values = [v.strip() for v in vals_str.split(",")]

    if ptype == "int":
        return name, [int(v) for v in raw_values]
    else:
        return name, [float(v) for v in raw_values]
