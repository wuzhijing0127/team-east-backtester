"""Result analysis — parameter sensitivity and reporting."""

from __future__ import annotations

import logging
from typing import Any

from optimizer.codegen import DEFAULTS, SEARCH_PARAMS

logger = logging.getLogger(__name__)


def parameter_sensitivity(
    results: list[tuple[dict[str, Any], Any]],
) -> dict[str, float]:
    """Compute correlation between each parameter value and final_pnl.

    Returns dict of param_name → Pearson correlation coefficient.
    Only considers parameters that vary across results.
    """
    if len(results) < 3:
        return {}

    sensitivities: dict[str, float] = {}

    for param in SEARCH_PARAMS:
        values = []
        pnls = []
        for overrides, summary in results:
            if summary is None:
                continue
            val = overrides.get(param, DEFAULTS.get(param))
            if val is not None:
                values.append(float(val))
                pnls.append(summary.final_pnl)

        # Skip if param didn't vary
        if len(set(values)) < 2:
            continue

        corr = _pearson(values, pnls)
        if corr is not None:
            sensitivities[param] = corr

    # Sort by absolute correlation
    return dict(sorted(sensitivities.items(), key=lambda x: abs(x[1]), reverse=True))


def sensitivity_report(results: list[tuple[dict[str, Any], Any]]) -> str:
    """Generate a formatted sensitivity report."""
    sens = parameter_sensitivity(results)
    if not sens:
        return "Not enough data for sensitivity analysis (need 3+ varied results)."

    lines = ["Parameter Sensitivity (correlation with PnL):", "=" * 50]
    for param, corr in sens.items():
        direction = "+" if corr > 0 else "-"
        bar_len = int(abs(corr) * 20)
        bar = "#" * bar_len + "." * (20 - bar_len)
        lines.append(f"  {param:<28} {direction}{abs(corr):.3f}  [{bar}]")
    return "\n".join(lines)


def suggest_next_grid(
    results: list[tuple[dict[str, Any], Any]],
    top_n: int = 3,
) -> dict[str, list[Any]]:
    """Suggest the next grid search based on sensitivity analysis.

    Strategy: For the top N most sensitive params, zoom into the region
    around the best-performing value.
    """
    if not results:
        return {}

    sens = parameter_sensitivity(results)
    if not sens:
        return {}

    # Find the best config
    results_sorted = sorted(results, key=lambda r: r[1].final_pnl, reverse=True)
    best_overrides = results_sorted[0][0]

    suggested: dict[str, list] = {}
    for param in list(sens.keys())[:top_n]:
        info = SEARCH_PARAMS.get(param)
        if not info:
            continue

        best_val = best_overrides.get(param, info["default"])
        full_range = info["range"]

        try:
            idx = full_range.index(best_val)
        except ValueError:
            suggested[param] = full_range
            continue

        # Take 3-5 values centered around the best
        lo = max(0, idx - 2)
        hi = min(len(full_range), idx + 3)
        suggested[param] = full_range[lo:hi]

    return suggested


def top_configs_report(
    results: list[tuple[dict[str, Any], Any]],
    limit: int = 10,
) -> str:
    """Generate a formatted leaderboard of top configurations."""
    if not results:
        return "No results yet."

    results_sorted = sorted(results, key=lambda r: r[1].final_pnl, reverse=True)

    lines = [
        f"{'Rank':<5}{'PnL':>12}{'MaxDD':>10}{'Slope':>10}  Config",
        "-" * 80,
    ]
    for rank, (overrides, s) in enumerate(results_sorted[:limit], 1):
        diff = ", ".join(
            f"{k}={v}" for k, v in overrides.items()
            if v != DEFAULTS.get(k)
        )
        lines.append(
            f"{rank:<5}{s.final_pnl:>12,.2f}{s.max_drawdown:>10,.2f}"
            f"{s.pnl_slope:>10,.4f}  {diff or 'defaults'}"
        )

    return "\n".join(lines)


def _pearson(x: list[float], y: list[float]) -> float | None:
    """Compute Pearson correlation coefficient."""
    n = len(x)
    if n < 2:
        return None

    mx = sum(x) / n
    my = sum(y) / n

    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    dx = sum((xi - mx) ** 2 for xi in x) ** 0.5
    dy = sum((yi - my) ** 2 for yi in y) ** 0.5

    if dx == 0 or dy == 0:
        return None

    return num / (dx * dy)
