"""Complete test matrix — all configs from Sections 3-7."""

# ── BASELINE ──────────────────────────────────────────────────
BASELINE = {
    "name": "BASELINE_CHAMPION",
    "family": "baseline",
    "pepper_limit": 80,
    "ash_base_size": 20,
    "ash_half_spread": 1,
    "ash_position_limit": 50,
    "ash_k_inv": 2.5,
    "ash_flatten_size": 10,
    "ash_tier_medium": 0.4,
    "ash_tier_high": 0.7,
    "ash_tier_extreme": 0.9,
    "take_schedule": [[0.30, 1], [1.01, 0]],
    "quote_mode": "symmetric",
    "bid_hs": 1,
    "ask_hs": 1,
}

# ── SECTION 4: NEUTRAL-ZONE TAKING ───────────────────────────

# A1: Two-zone
NT_A1 = [
    {"name": "NT_A1_1", "family": "neutral_taking", "take_schedule": [[0.10, 2], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1},
    {"name": "NT_A1_2", "family": "neutral_taking", "take_schedule": [[0.15, 2], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1},
    {"name": "NT_A1_3", "family": "neutral_taking", "take_schedule": [[0.20, 2], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1},
    {"name": "NT_A1_4", "family": "neutral_taking", "take_schedule": [[0.10, 1], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1},
    {"name": "NT_A1_5", "family": "neutral_taking", "take_schedule": [[0.15, 1], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1},
    {"name": "NT_A1_6", "family": "neutral_taking", "take_schedule": [[0.20, 1], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1},
]

# A2: Three-zone
NT_A2 = [
    {"name": "NT_A2_1", "family": "neutral_taking", "take_schedule": [[0.10, 2], [0.30, 1], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1},
    {"name": "NT_A2_2", "family": "neutral_taking", "take_schedule": [[0.15, 2], [0.30, 1], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1},
    {"name": "NT_A2_3", "family": "neutral_taking", "take_schedule": [[0.10, 3], [0.30, 1], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1},
    {"name": "NT_A2_4", "family": "neutral_taking", "take_schedule": [[0.10, 2], [0.25, 1], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1},
    {"name": "NT_A2_5", "family": "neutral_taking", "take_schedule": [[0.05, 2], [0.30, 1], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1},
]

# A3: Four-zone
NT_A3 = [
    {"name": "NT_A3_1", "family": "neutral_taking", "take_schedule": [[0.10, 2], [0.20, 1], [0.30, 0], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1},
    {"name": "NT_A3_2", "family": "neutral_taking", "take_schedule": [[0.05, 3], [0.15, 2], [0.30, 1], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1},
    {"name": "NT_A3_3", "family": "neutral_taking", "take_schedule": [[0.10, 2], [0.20, 1], [0.40, 0], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1},
]

# ── SECTION 5: ASYMMETRIC QUOTING ────────────────────────────

# B1: Static asymmetry
AQ_B1 = [
    {"name": "AQ_B1_1", "family": "asym_quote", "take_schedule": [[0.30, 1], [1.01, 0]], "quote_mode": "static_asym", "bid_hs": 1, "ask_hs": 2},
    {"name": "AQ_B1_2", "family": "asym_quote", "take_schedule": [[0.30, 1], [1.01, 0]], "quote_mode": "static_asym", "bid_hs": 2, "ask_hs": 1},
    {"name": "AQ_B1_3", "family": "asym_quote", "take_schedule": [[0.30, 1], [1.01, 0]], "quote_mode": "static_asym", "bid_hs": 1, "ask_hs": 3},
    {"name": "AQ_B1_4", "family": "asym_quote", "take_schedule": [[0.30, 1], [1.01, 0]], "quote_mode": "static_asym", "bid_hs": 3, "ask_hs": 1},
    {"name": "AQ_B1_5", "family": "asym_quote", "take_schedule": [[0.30, 1], [1.01, 0]], "quote_mode": "static_asym", "bid_hs": 2, "ask_hs": 3},
    {"name": "AQ_B1_6", "family": "asym_quote", "take_schedule": [[0.30, 1], [1.01, 0]], "quote_mode": "static_asym", "bid_hs": 3, "ask_hs": 2},
]

# B2: Inventory-conditional
AQ_B2 = [
    {"name": "AQ_B2_1", "family": "asym_quote", "take_schedule": [[0.30, 1], [1.01, 0]], "quote_mode": "inv_conditional",
     "inv_conditional_quotes": {"long": {"bid_hs": 2, "ask_hs": 1}, "short": {"bid_hs": 1, "ask_hs": 2}, "neutral": {"bid_hs": 1, "ask_hs": 1}}},
    {"name": "AQ_B2_2", "family": "asym_quote", "take_schedule": [[0.30, 1], [1.01, 0]], "quote_mode": "inv_conditional",
     "inv_conditional_quotes": {"long": {"bid_hs": 3, "ask_hs": 1}, "short": {"bid_hs": 1, "ask_hs": 3}, "neutral": {"bid_hs": 1, "ask_hs": 1}}},
    {"name": "AQ_B2_3", "family": "asym_quote", "take_schedule": [[0.30, 1], [1.01, 0]], "quote_mode": "inv_conditional",
     "inv_conditional_quotes": {"long": {"bid_hs": 2, "ask_hs": 1}, "short": {"bid_hs": 1, "ask_hs": 2}, "neutral": {"bid_hs": 1, "ask_hs": 2}}},
    {"name": "AQ_B2_4", "family": "asym_quote", "take_schedule": [[0.30, 1], [1.01, 0]], "quote_mode": "inv_conditional",
     "inv_conditional_quotes": {"long": {"bid_hs": 2, "ask_hs": 1}, "short": {"bid_hs": 1, "ask_hs": 2}, "neutral": {"bid_hs": 2, "ask_hs": 1}}},
]

# B3: Mild asymmetry near neutral
AQ_B3 = [
    {"name": "AQ_B3_1", "family": "asym_quote", "take_schedule": [[0.30, 1], [1.01, 0]], "quote_mode": "neutral_conditional",
     "neutral_quote_schedule": [[0.10, 1, 2], [1.01, 1, 1]]},
    {"name": "AQ_B3_2", "family": "asym_quote", "take_schedule": [[0.30, 1], [1.01, 0]], "quote_mode": "neutral_conditional",
     "neutral_quote_schedule": [[0.10, 2, 1], [1.01, 1, 1]]},
    {"name": "AQ_B3_3", "family": "asym_quote", "take_schedule": [[0.30, 1], [1.01, 0]], "quote_mode": "neutral_conditional",
     "neutral_quote_schedule": [[0.15, 1, 2], [1.01, 1, 1]]},
]

# ── SECTION 6: NEUTRAL SCHEDULES (joint quote+take) ──────────

NS_C1 = [
    {"name": "NS_C1_1", "family": "neutral_schedule", "neutral_schedule": [[0.10, 1, 1, 2], [0.30, 1, 1, 1], [1.01, 2, 2, 0]]},
    {"name": "NS_C1_2", "family": "neutral_schedule", "neutral_schedule": [[0.10, 1, 1, 3], [0.30, 1, 1, 1], [1.01, 2, 2, 0]]},
    {"name": "NS_C1_3", "family": "neutral_schedule", "neutral_schedule": [[0.15, 1, 1, 2], [0.30, 1, 1, 1], [1.01, 2, 2, 0]]},
]

NS_C2 = [
    {"name": "NS_C2_1", "family": "neutral_schedule", "neutral_schedule": [[0.10, 1, 1, 2], [0.30, 1, 1, 1], [1.01, 1, 2, 0]]},
    {"name": "NS_C2_2", "family": "neutral_schedule", "neutral_schedule": [[0.10, 1, 1, 2], [0.30, 1, 1, 1], [1.01, 2, 1, 0]]},
]

NS_C3 = [
    {"name": "NS_C3_1", "family": "neutral_schedule", "neutral_schedule": [[0.10, 1, 2, 2], [0.30, 1, 2, 1], [1.01, 2, 2, 0]]},
    {"name": "NS_C3_2", "family": "neutral_schedule", "neutral_schedule": [[0.10, 2, 1, 2], [0.30, 2, 1, 1], [1.01, 2, 2, 0]]},
]

# ── SECTION 7: PORTFOLIO COUPLING ─────────────────────────────

PC_D1 = [
    {"name": "PC_D1_1", "family": "portfolio_coupling", "take_schedule": [[0.30, 1], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1,
     "pepper_coupling": {"threshold": 0.50, "mode": "conservative", "bid_hs": 2, "ask_hs": 2, "take_edge": 0}},
    {"name": "PC_D1_2", "family": "portfolio_coupling", "take_schedule": [[0.30, 1], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1,
     "pepper_coupling": {"threshold": 0.75, "mode": "conservative", "bid_hs": 2, "ask_hs": 2, "take_edge": 0}},
]

PC_D2 = [
    {"name": "PC_D2_1", "family": "portfolio_coupling", "take_schedule": [[0.30, 1], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1,
     "pepper_coupling": {"threshold": 0.50, "mode": "neutral_tighten", "neutral_threshold": 0.15}},
    {"name": "PC_D2_2", "family": "portfolio_coupling", "take_schedule": [[0.30, 1], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1,
     "pepper_coupling": {"threshold": 0.75, "mode": "neutral_tighten", "neutral_threshold": 0.10}},
]

PC_D3 = [
    {"name": "PC_D3_1", "family": "portfolio_coupling", "take_schedule": [[0.30, 1], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1,
     "pepper_coupling": {"threshold": 0.50, "mode": "mild_widen", "bid_hs": 1, "ask_hs": 2}},
    {"name": "PC_D3_2", "family": "portfolio_coupling", "take_schedule": [[0.30, 1], [1.01, 0]], "quote_mode": "symmetric", "bid_hs": 1, "ask_hs": 1,
     "pepper_coupling": {"threshold": 0.50, "mode": "mild_widen", "bid_hs": 2, "ask_hs": 1}},
]

# ── ALL CONFIGS IN EXECUTION ORDER ────────────────────────────

ALL_CONFIGS = (
    [BASELINE]
    + NT_A1 + NT_A2 + NT_A3          # Section 4: 14 configs
    + AQ_B1 + AQ_B2 + AQ_B3          # Section 5: 13 configs
    + NS_C1 + NS_C2 + NS_C3          # Section 6: 7 configs
    + PC_D1 + PC_D2 + PC_D3          # Section 7: 6 configs
)

# Add common defaults to all configs
for cfg in ALL_CONFIGS:
    cfg.setdefault("pepper_limit", 80)
    cfg.setdefault("ash_base_size", 20)
    cfg.setdefault("ash_position_limit", 50)
    cfg.setdefault("ash_k_inv", 2.5)
    cfg.setdefault("ash_flatten_size", 10)
    cfg.setdefault("ash_tier_medium", 0.4)
    cfg.setdefault("ash_tier_high", 0.7)
    cfg.setdefault("ash_tier_extreme", 0.9)
    cfg.setdefault("ash_anchor_fair", 10000)
