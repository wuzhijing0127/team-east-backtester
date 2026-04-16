"""Parse raw JSON artifacts into normalized time series."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from exceptions import ArtifactParseError

logger = logging.getLogger(__name__)

# Candidate field names for auto-detection
_TIME_FIELDS = ("timestamp", "time", "t", "ts", "x", "step", "tick")
_VALUE_FIELDS = ("pnl", "profit", "value", "y", "profit_and_loss", "cumulative_pnl", "equity")
_SERIES_FIELDS = ("series", "data", "points", "records", "activities", "timeSeries")


@dataclass
class TimeSeriesPoint:
    timestamp: float
    value: float


@dataclass
class ParsedArtifact:
    """Normalized parsed result from a raw artifact."""

    # Primary PnL time series (aggregated across products if possible)
    pnl_series: list[TimeSeriesPoint] = field(default_factory=list)

    # Per-product series if detected
    product_series: dict[str, list[TimeSeriesPoint]] = field(default_factory=dict)

    # Trade records if present
    trades: list[dict[str, Any]] = field(default_factory=list)

    # Raw top-level fields found (for schema inspection)
    top_level_keys: list[str] = field(default_factory=list)
    detected_products: list[str] = field(default_factory=list)

    # Schema info
    schema_notes: list[str] = field(default_factory=list)


def parse_artifact(data: Any) -> ParsedArtifact:
    """Parse a raw artifact JSON into a normalized ParsedArtifact.

    This function handles multiple possible schemas defensively.
    It logs what it finds and what it could not parse.
    """
    result = ParsedArtifact()

    if isinstance(data, dict):
        result.top_level_keys = list(data.keys())
        logger.info("Artifact is dict with keys: %s", result.top_level_keys)
        _parse_dict_artifact(data, result)
    elif isinstance(data, list):
        result.schema_notes.append(f"Top-level list with {len(data)} items")
        logger.info("Artifact is list with %d items", len(data))
        _parse_list_artifact(data, result)
    else:
        raise ArtifactParseError(
            f"Unexpected artifact type: {type(data).__name__}"
        )

    # Log summary
    logger.info(
        "Parsed artifact: %d PnL points, %d products, %d trades, notes: %s",
        len(result.pnl_series),
        len(result.product_series),
        len(result.trades),
        result.schema_notes,
    )

    return result


def _parse_dict_artifact(data: dict[str, Any], result: ParsedArtifact) -> None:
    """Parse when artifact is a dict at the top level."""

    # Look for nested series under known keys
    for series_key in _SERIES_FIELDS:
        if series_key in data:
            val = data[series_key]
            if isinstance(val, list):
                result.schema_notes.append(f"Found series under '{series_key}'")
                points = _extract_time_series(val)
                if points:
                    result.pnl_series = points
                    return

    # Check for per-product structure: { "PRODUCT_A": [...], "PRODUCT_B": [...] }
    product_candidates = {}
    for key, val in data.items():
        if isinstance(val, list) and len(val) > 0:
            points = _extract_time_series(val)
            if points:
                product_candidates[key] = points

    if product_candidates:
        result.detected_products = list(product_candidates.keys())
        result.product_series = product_candidates
        result.schema_notes.append(
            f"Found per-product series: {result.detected_products}"
        )
        # Aggregate into single PnL series if multiple products
        result.pnl_series = _aggregate_product_series(product_candidates)
        return

    # Check for nested data container
    for container_key in ("data", "result", "results", "output"):
        if container_key in data and isinstance(data[container_key], (dict, list)):
            result.schema_notes.append(f"Recursing into '{container_key}'")
            inner = data[container_key]
            if isinstance(inner, dict):
                _parse_dict_artifact(inner, result)
            elif isinstance(inner, list):
                _parse_list_artifact(inner, result)
            if result.pnl_series:
                return

    # Look for trade records
    for trade_key in ("trades", "fills", "executions", "tradeHistory"):
        if trade_key in data and isinstance(data[trade_key], list):
            result.trades = data[trade_key]
            result.schema_notes.append(
                f"Found {len(result.trades)} trades under '{trade_key}'"
            )

    # Last resort: try to treat the entire dict values as a flat series
    result.schema_notes.append("Could not find standard series structure")


def _parse_list_artifact(data: list, result: ParsedArtifact) -> None:
    """Parse when artifact is a list at the top level."""
    if not data:
        result.schema_notes.append("Empty list")
        return

    # If items are dicts, try to extract time series
    if isinstance(data[0], dict):
        points = _extract_time_series(data)
        if points:
            result.pnl_series = points
            result.schema_notes.append(f"Extracted {len(points)} points from top-level list")
            return

        # Maybe it's a list of trade records
        sample = data[0]
        if any(k in sample for k in ("price", "quantity", "buyer", "seller")):
            result.trades = data
            result.schema_notes.append(f"Top-level list looks like {len(data)} trade records")
            return

    # If items are lists (list of lists), try as [timestamp, value] pairs
    if isinstance(data[0], (list, tuple)) and len(data[0]) >= 2:
        points = []
        for item in data:
            try:
                points.append(TimeSeriesPoint(timestamp=float(item[0]), value=float(item[1])))
            except (ValueError, TypeError, IndexError):
                continue
        if points:
            result.pnl_series = points
            result.schema_notes.append(f"Extracted {len(points)} points from list-of-lists")
            return

    result.schema_notes.append(f"Unrecognized list structure (sample: {type(data[0]).__name__})")


def _extract_time_series(records: list[dict[str, Any]]) -> list[TimeSeriesPoint]:
    """Try to extract a time series from a list of dicts.

    Auto-detects timestamp and value field names.
    """
    if not records or not isinstance(records[0], dict):
        return []

    sample = records[0]
    time_field = _detect_field(sample, _TIME_FIELDS)
    value_field = _detect_field(sample, _VALUE_FIELDS)

    if time_field is None or value_field is None:
        # Try numeric fields as fallback
        numeric_fields = [k for k, v in sample.items() if isinstance(v, (int, float))]
        if len(numeric_fields) >= 2 and time_field is None:
            time_field = numeric_fields[0]
            if value_field is None:
                value_field = numeric_fields[1]

    if time_field is None or value_field is None:
        return []

    points = []
    for record in records:
        try:
            t = float(record[time_field])
            v = float(record[value_field])
            points.append(TimeSeriesPoint(timestamp=t, value=v))
        except (KeyError, ValueError, TypeError):
            continue

    return points


def _detect_field(sample: dict[str, Any], candidates: tuple[str, ...]) -> Optional[str]:
    """Find the first matching field name from candidates (case-insensitive)."""
    lower_keys = {k.lower(): k for k in sample}
    for candidate in candidates:
        if candidate.lower() in lower_keys:
            return lower_keys[candidate.lower()]
    return None


def _aggregate_product_series(
    product_series: dict[str, list[TimeSeriesPoint]],
) -> list[TimeSeriesPoint]:
    """Aggregate per-product series into a single PnL series by summing at each timestamp."""
    from collections import defaultdict

    by_ts: dict[float, float] = defaultdict(float)
    for series in product_series.values():
        for pt in series:
            by_ts[pt.timestamp] += pt.value

    return [
        TimeSeriesPoint(timestamp=ts, value=val)
        for ts, val in sorted(by_ts.items())
    ]


def inspect_schema(data: Any, max_depth: int = 3) -> str:
    """Return a human-readable schema summary for unknown artifacts."""
    lines: list[str] = []
    _inspect_recursive(data, lines, prefix="", depth=0, max_depth=max_depth)
    return "\n".join(lines)


def _inspect_recursive(
    obj: Any,
    lines: list[str],
    prefix: str,
    depth: int,
    max_depth: int,
) -> None:
    indent = "  " * depth
    if depth >= max_depth:
        lines.append(f"{indent}{prefix}... (max depth)")
        return

    if isinstance(obj, dict):
        lines.append(f"{indent}{prefix}dict ({len(obj)} keys)")
        for key in list(obj.keys())[:20]:
            _inspect_recursive(obj[key], lines, f".{key}: ", depth + 1, max_depth)
        if len(obj) > 20:
            lines.append(f"{indent}  ... and {len(obj) - 20} more keys")
    elif isinstance(obj, list):
        lines.append(f"{indent}{prefix}list ({len(obj)} items)")
        if obj:
            _inspect_recursive(obj[0], lines, "[0]: ", depth + 1, max_depth)
            if len(obj) > 1:
                lines.append(f"{indent}  ... ({len(obj) - 1} more items)")
    else:
        type_name = type(obj).__name__
        sample = str(obj)[:80] if obj is not None else "null"
        lines.append(f"{indent}{prefix}{type_name} = {sample}")
