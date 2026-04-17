"""S1-S5 structured test matrix — 76 configs.

Platform is deterministic → 1 baseline run sufficient.
All configs use: PEPPER=80 B&H, ASH hs=1, anchor=10000, k_inv=2.5
"""

def _base(**kw):
    """Create a config with common defaults."""
    cfg = {
        "pepper_limit": 80,
        "ash_position_limit": 50,
        "ash_anchor_fair": 10000,
        "ash_k_inv": 2.5,
        "ash_base_size": 20,
        "ash_half_spread": 1,
        "ash_flatten_size": 10,
        "ash_tier_medium": 0.4,
        "ash_tier_high": 0.7,
        "ash_tier_extreme": 0.9,
    }
    cfg.update(kw)
    return cfg


# ── BASELINE ──────────────────────────────────────────────
BASELINE = _base(name="B0_01", family="baseline",
    take_schedule=[[0.30, 1], [1.01, 0]])

# ── S1: 2-band take schedule (18 configs) ─────────────────
S1 = []
_s1_grid = [
    ("S1_01", 0.10, 1, 0), ("S1_02", 0.20, 1, 0), ("S1_03", 0.25, 1, 0), ("S1_04", 0.30, 1, 0),
    ("S1_05", 0.10, 2, 0), ("S1_06", 0.20, 2, 0), ("S1_07", 0.25, 2, 0), ("S1_08", 0.30, 2, 0),
    ("S1_09", 0.10, 1, 1), ("S1_10", 0.20, 1, 1), ("S1_11", 0.25, 1, 1), ("S1_12", 0.30, 1, 1),
    ("S1_13", 0.10, 2, 1), ("S1_14", 0.20, 2, 1), ("S1_15", 0.25, 2, 1), ("S1_16", 0.30, 2, 1),
    ("S1_17", 0.20, 0, 0), ("S1_18", 0.30, 0, 0),
]
for name, t1, te0, te1 in _s1_grid:
    S1.append(_base(name=name, family="S1_2band",
        take_schedule=[[t1, te0], [1.01, te1]]))

# ── S2: 3-band take schedule (21 configs) ─────────────────
S2 = []
_s2_grid = [
    ("S2_01", 0.10, 0.40, 2, 1, 0), ("S2_02", 0.10, 0.50, 2, 1, 0), ("S2_03", 0.10, 0.60, 2, 1, 0),
    ("S2_04", 0.15, 0.40, 2, 1, 0), ("S2_05", 0.15, 0.50, 2, 1, 0), ("S2_06", 0.15, 0.60, 2, 1, 0),
    ("S2_07", 0.20, 0.40, 2, 1, 0), ("S2_08", 0.20, 0.50, 2, 1, 0), ("S2_09", 0.20, 0.60, 2, 1, 0),
    ("S2_10", 0.10, 0.50, 2, 0, 0), ("S2_11", 0.15, 0.50, 2, 0, 0), ("S2_12", 0.20, 0.50, 2, 0, 0),
    ("S2_13", 0.10, 0.50, 1, 1, 0), ("S2_14", 0.15, 0.50, 1, 1, 0), ("S2_15", 0.20, 0.50, 1, 1, 0),
    ("S2_16", 0.10, 0.50, 1, 0, 0), ("S2_17", 0.15, 0.50, 1, 0, 0), ("S2_18", 0.20, 0.50, 1, 0, 0),
    ("S2_19", 0.10, 0.50, 2, 1, 1), ("S2_20", 0.15, 0.50, 2, 1, 1), ("S2_21", 0.20, 0.50, 2, 1, 1),
]
for name, t1, t2, te0, te1, te2 in _s2_grid:
    S2.append(_base(name=name, family="S2_3band",
        take_schedule=[[t1, te0], [t2, te1], [1.01, te2]]))

# ── S3: size schedule (14 configs) ────────────────────────
# These need custom codegen since base_size varies by band
S3 = []
_s3_grid = [
    ("S3_01", 0.15, 0.50, 24, 20, 16), ("S3_02", 0.20, 0.50, 24, 20, 16), ("S3_03", 0.25, 0.50, 24, 20, 16),
    ("S3_04", 0.15, 0.60, 28, 20, 12), ("S3_05", 0.20, 0.60, 28, 20, 12), ("S3_06", 0.25, 0.60, 28, 20, 12),
    ("S3_07", 0.15, 0.50, 20, 16, 12), ("S3_08", 0.20, 0.50, 20, 16, 12), ("S3_09", 0.25, 0.50, 20, 16, 12),
    ("S3_10", 0.15, 0.60, 24, 16, 12), ("S3_11", 0.20, 0.60, 24, 16, 12), ("S3_12", 0.25, 0.60, 24, 16, 12),
    ("S3_13", 0.20, 0.50, 20, 20, 16), ("S3_14", 0.20, 0.60, 20, 20, 16),
]
for name, t1, t2, bs0, bs1, bs2 in _s3_grid:
    S3.append({
        "name": name, "family": "S3_size",
        "t1": t1, "t2": t2, "bs0": bs0, "bs1": bs1, "bs2": bs2,
    })

# ── S4: PEPPER-load-aware non-defensive (12 configs) ──────
S4 = []
_s4_grid = [
    ("S4_01", 0.50, 0.20, "take", 1), ("S4_02", 0.75, 0.20, "take", 1), ("S4_03", 0.90, 0.20, "take", 1),
    ("S4_04", 0.50, 0.25, "take", 1), ("S4_05", 0.75, 0.25, "take", 1), ("S4_06", 0.90, 0.25, "take", 1),
    ("S4_07", 0.50, 0.20, "size", 4), ("S4_08", 0.75, 0.20, "size", 4), ("S4_09", 0.90, 0.20, "size", 4),
    ("S4_10", 0.50, 0.25, "size", 4), ("S4_11", 0.75, 0.25, "size", 4), ("S4_12", 0.90, 0.25, "size", 4),
]
for name, load_cut, t1, mode, boost in _s4_grid:
    S4.append({
        "name": name, "family": "S4_pepper",
        "load_cut": load_cut, "t1": t1, "mode": mode, "boost": boost,
    })

# ── S5: failure confirmation (6 configs) ──────────────────
S5 = [
    _base(name="S5_01", family="S5_confirm", take_schedule=[[0.30, 1], [1.01, 0]], ash_half_spread=2),
    _base(name="S5_02", family="S5_confirm", take_schedule=[[0.30, 1], [1.01, 0]], ash_half_spread=2),
    _base(name="S5_03", family="S5_confirm", take_schedule=[[0.30, 3], [1.01, 0]]),
    _base(name="S5_04", family="S5_confirm", take_schedule=[[0.30, 3], [1.01, 1]]),
    # S5_05 and S5_06 are pepper defensive — need custom code
    {"name": "S5_05", "family": "S5_confirm", "mode": "pepper_defensive"},
    {"name": "S5_06", "family": "S5_confirm", "mode": "pepper_defensive_wide"},
]

ALL_S1S5 = [BASELINE] + S1 + S2 + S3 + S4 + S5
