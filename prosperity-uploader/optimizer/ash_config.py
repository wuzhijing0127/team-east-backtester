"""ASHConfig — fully parameterized, always-skewed market maker configuration.

Every decision rule is a parameter. Asymmetric on both sides (bid/ask).
Designed for staged search: random coarse → local refinement → robustness.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, asdict, fields
from typing import Any


@dataclass(frozen=True)
class ASHConfig:
    """Complete ASH market-making configuration. ~35 parameters."""

    # ── Fair value computation ────────────────────────────────
    anchor_fair: int = 10000
    micro_beta: float = 0.0        # weight of (microprice - mid) adjustment
    imbalance_beta: float = 0.0    # weight of orderbook volume imbalance

    # ── Always-skewed reservation price ───────────────────────
    base_skew: float = 0.0         # fixed directional shift on reservation
    inventory_skew_k: float = 2.5  # reservation -= k * (pos / limit)
    signal_skew_k: float = 0.0     # reservation += k * signal (future use)

    # ── Asymmetric passive spreads ────────────────────────────
    bid_half_spread: int = 3       # bid = reservation - bid_half_spread
    ask_half_spread: int = 3       # ask = reservation + ask_half_spread

    # ── Quote join/improve behavior ───────────────────────────
    # 0 = cap at reservation-derived price
    # 1 = join best bid/ask
    # 2 = improve best by 1 tick
    join_improve_mode: int = 2

    # ── Asymmetric taking ─────────────────────────────────────
    take_buy_edge: int = 2         # buy asks at fair - take_buy_edge
    take_sell_edge: int = 2        # sell bids at fair + take_sell_edge
    take_buy_when_short_edge: int = 0   # more aggressive buy when short
    take_sell_when_long_edge: int = 0   # more aggressive sell when long

    # ── Asymmetric passive quote sizing ───────────────────────
    quote_size_bid: int = 15       # base bid size
    quote_size_ask: int = 15       # base ask size

    # ── Position limits ───────────────────────────────────────
    position_limit: int = 50

    # ── Inventory tier thresholds ─────────────────────────────
    tier_medium: float = 0.4
    tier_high: float = 0.7
    tier_extreme: float = 0.9

    # ── Asymmetric inventory size multipliers ─────────────────
    # When inventory is low (normal): full size
    bid_mult_normal: float = 1.0
    ask_mult_normal: float = 1.0
    # When at medium inventory tier
    bid_mult_medium: float = 0.5
    ask_mult_medium: float = 0.5
    # When at high inventory tier
    bid_mult_high: float = 0.25
    ask_mult_high: float = 0.25
    # When at extreme inventory tier
    bid_mult_extreme: float = 0.0
    ask_mult_extreme: float = 0.0

    # ── Flattening / safety ───────────────────────────────────
    flatten_enabled: bool = True
    flatten_trigger: float = 0.9   # fraction of limit
    flatten_size: int = 10
    flatten_aggression: int = 0    # 0 = hit best, 1 = cross to fair

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ASHConfig:
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)


# ── Parameter ranges for search ───────────────────────────────

PARAM_RANGES: dict[str, dict[str, Any]] = {
    # Fair value — keep conservative (high values cause huge losses)
    "micro_beta":          {"type": "float", "low": 0.0,  "high": 0.5,  "step": 0.1},
    "imbalance_beta":      {"type": "float", "low": 0.0,  "high": 0.5,  "step": 0.1},

    # Skew — keep near zero (large skew = guaranteed losses)
    "base_skew":           {"type": "float", "low": -0.5, "high": 0.5,  "step": 0.25},
    "inventory_skew_k":    {"type": "float", "low": 1.0,  "high": 4.0,  "step": 0.5},

    # Spreads (asymmetric)
    "bid_half_spread":     {"type": "int",   "low": 1,    "high": 8,    "step": 1},
    "ask_half_spread":     {"type": "int",   "low": 1,    "high": 8,    "step": 1},

    # Join/improve
    "join_improve_mode":   {"type": "int",   "low": 0,    "high": 2,    "step": 1},

    # Taking (asymmetric)
    "take_buy_edge":       {"type": "int",   "low": 0,    "high": 4,    "step": 1},
    "take_sell_edge":      {"type": "int",   "low": 0,    "high": 4,    "step": 1},
    "take_buy_when_short_edge":  {"type": "int", "low": 0, "high": 3,   "step": 1},
    "take_sell_when_long_edge":  {"type": "int", "low": 0, "high": 3,   "step": 1},

    # Quote sizing (asymmetric)
    "quote_size_bid":      {"type": "int",   "low": 3,    "high": 25,   "step": 2},
    "quote_size_ask":      {"type": "int",   "low": 3,    "high": 25,   "step": 2},

    # Inventory tiers
    "tier_medium":         {"type": "float", "low": 0.3,  "high": 0.6,  "step": 0.1},
    "tier_high":           {"type": "float", "low": 0.5,  "high": 0.8,  "step": 0.1},
    "tier_extreme":        {"type": "float", "low": 0.8,  "high": 1.0,  "step": 0.05},

    # Asymmetric inventory multipliers
    "bid_mult_medium":     {"type": "float", "low": 0.0,  "high": 1.0,  "step": 0.25},
    "ask_mult_medium":     {"type": "float", "low": 0.0,  "high": 1.0,  "step": 0.25},
    "bid_mult_high":       {"type": "float", "low": 0.0,  "high": 0.5,  "step": 0.25},
    "ask_mult_high":       {"type": "float", "low": 0.0,  "high": 0.5,  "step": 0.25},

    # Flattening
    "flatten_trigger":     {"type": "float", "low": 0.7,  "high": 1.0,  "step": 0.05},
    "flatten_size":        {"type": "int",   "low": 3,    "high": 20,   "step": 2},
    "flatten_aggression":  {"type": "int",   "low": 0,    "high": 1,    "step": 1},
}


def random_config(seed: int | None = None) -> ASHConfig:
    """Generate a random ASHConfig by sampling each parameter from its range."""
    if seed is not None:
        random.seed(seed)

    overrides: dict[str, Any] = {}
    for name, spec in PARAM_RANGES.items():
        low, high, step = spec["low"], spec["high"], spec["step"]
        if spec["type"] == "int":
            overrides[name] = random.randint(low, high)
        else:
            # Snap to step grid
            n_steps = int((high - low) / step)
            overrides[name] = round(low + random.randint(0, n_steps) * step, 4)

    return ASHConfig(**overrides)


def latin_hypercube_configs(n: int, seed: int = 42) -> list[ASHConfig]:
    """Generate n configs using Latin Hypercube Sampling for better coverage."""
    rng = random.Random(seed)
    param_names = list(PARAM_RANGES.keys())
    k = len(param_names)

    # For each param, create n evenly-spaced bins and shuffle
    bins: dict[str, list[float]] = {}
    for name, spec in PARAM_RANGES.items():
        low, high, step = spec["low"], spec["high"], spec["step"]
        if spec["type"] == "int":
            n_possible = int((high - low) / step) + 1
            # Create bins across the range
            values = []
            for i in range(n):
                frac = (i + rng.random()) / n
                val = low + frac * (high - low)
                val = round(val / step) * step
                val = max(low, min(high, int(val)))
                values.append(val)
            rng.shuffle(values)
            bins[name] = values
        else:
            values = []
            for i in range(n):
                frac = (i + rng.random()) / n
                val = low + frac * (high - low)
                val = round(val / step) * step
                val = max(low, min(high, round(val, 4)))
                values.append(val)
            rng.shuffle(values)
            bins[name] = values

    configs = []
    for i in range(n):
        overrides = {name: bins[name][i] for name in param_names}
        configs.append(ASHConfig(**overrides))

    return configs


def perturb_config(config: ASHConfig, n_changes: int = 3, seed: int | None = None) -> ASHConfig:
    """Create a neighbor by perturbing n_changes random parameters by ±1 step."""
    rng = random.Random(seed)
    d = config.to_dict()
    param_names = list(PARAM_RANGES.keys())
    to_change = rng.sample(param_names, min(n_changes, len(param_names)))

    for name in to_change:
        spec = PARAM_RANGES[name]
        current = d[name]
        step = spec["step"]
        direction = rng.choice([-1, 1])
        new_val = current + direction * step

        # Clamp to range
        new_val = max(spec["low"], min(spec["high"], new_val))
        if spec["type"] == "int":
            new_val = int(round(new_val))
        else:
            new_val = round(new_val, 4)

        d[name] = new_val

    return ASHConfig.from_dict(d)


def config_diff(config: ASHConfig) -> dict[str, Any]:
    """Return only parameters that differ from defaults."""
    default = ASHConfig()
    d = config.to_dict()
    dd = default.to_dict()
    return {k: v for k, v in d.items() if v != dd.get(k)}
