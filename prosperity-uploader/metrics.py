"""Compute summary metrics from parsed artifacts."""

from __future__ import annotations

import logging
import math
from statistics import stdev, mean
from typing import Optional

from artifact_parser import ParsedArtifact, TimeSeriesPoint
from models import SummaryMetrics

logger = logging.getLogger(__name__)


def summarize_artifact(
    parsed: ParsedArtifact,
    strategy_name: str,
    submission_id: str,
) -> SummaryMetrics:
    """Compute all summary metrics from a parsed artifact.

    Handles empty/missing data gracefully.
    """
    series = parsed.pnl_series

    metrics = SummaryMetrics(
        strategy_name=strategy_name,
        submission_id=submission_id,
        products=parsed.detected_products,
        raw_fields_found=parsed.top_level_keys,
    )

    if not series:
        logger.warning("No PnL series data — returning empty metrics")
        return metrics

    values = [pt.value for pt in series]
    timestamps = [pt.timestamp for pt in series]

    metrics.num_points = len(values)
    metrics.final_pnl = values[-1]
    metrics.max_pnl = max(values)
    metrics.min_pnl = min(values)

    # Time horizon
    if len(timestamps) >= 2:
        metrics.time_horizon = timestamps[-1] - timestamps[0]

    # Max drawdown
    dd_abs, dd_pct = _max_drawdown(values)
    metrics.max_drawdown = dd_abs
    metrics.max_drawdown_pct = dd_pct

    # Peak to final drop
    metrics.peak_to_final_drop = metrics.max_pnl - metrics.final_pnl

    # Recovery after max drawdown
    metrics.recovery_after_drawdown = _recovery_after_drawdown(values)

    # First positive timestamp
    metrics.first_positive_ts = _first_positive_timestamp(series)

    # PnL slope (linear regression)
    if len(values) >= 2:
        metrics.pnl_slope = _linear_slope(timestamps, values)

    # Volatility of PnL increments
    if len(values) >= 3:
        increments = [values[i] - values[i - 1] for i in range(1, len(values))]
        metrics.pnl_volatility = stdev(increments)

    # Trade count
    if parsed.trades:
        metrics.trade_count = len(parsed.trades)

    logger.info(
        "Metrics for %s: PnL=%.2f, MaxDD=%.2f, Slope=%.4f, Points=%d",
        strategy_name,
        metrics.final_pnl,
        metrics.max_drawdown,
        metrics.pnl_slope,
        metrics.num_points,
    )

    return metrics


def _max_drawdown(values: list[float]) -> tuple[float, float]:
    """Compute max drawdown (absolute and percentage)."""
    if not values:
        return 0.0, 0.0

    hwm = values[0]
    max_dd_abs = 0.0
    max_dd_pct = 0.0

    for v in values:
        hwm = max(hwm, v)
        dd = hwm - v
        max_dd_abs = max(max_dd_abs, dd)
        if hwm > 0:
            pct = dd / hwm
            max_dd_pct = max(max_dd_pct, pct)

    return max_dd_abs, max_dd_pct


def _recovery_after_drawdown(values: list[float]) -> float:
    """How much PnL recovered from the trough after max drawdown."""
    if len(values) < 2:
        return 0.0

    # Find the trough point (lowest point after peak)
    hwm = values[0]
    max_dd = 0.0
    trough_idx = 0

    for i, v in enumerate(values):
        hwm = max(hwm, v)
        dd = hwm - v
        if dd > max_dd:
            max_dd = dd
            trough_idx = i

    if trough_idx >= len(values) - 1:
        return 0.0

    trough_val = values[trough_idx]
    post_trough_max = max(values[trough_idx:])
    return post_trough_max - trough_val


def _first_positive_timestamp(series: list[TimeSeriesPoint]) -> Optional[float]:
    """Find the first timestamp where PnL > 0."""
    for pt in series:
        if pt.value > 0:
            return pt.timestamp
    return None


def _linear_slope(x: list[float], y: list[float]) -> float:
    """Simple linear regression slope."""
    n = len(x)
    if n < 2:
        return 0.0

    x_mean = mean(x)
    y_mean = mean(y)

    numerator = sum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x, y))
    denominator = sum((xi - x_mean) ** 2 for xi in x)

    if denominator == 0:
        return 0.0

    return numerator / denominator
