# Optimization Session Log — 2026-04-16

## Session Overview
- **Duration**: ~16 hours continuous
- **Total configs uploaded**: 500+
- **Auto-token refreshes**: 15+
- **Platform**: IMC Prosperity Round 2

---

## Phase 1: Discovery (hours 0-2)

### Confirmed API endpoints
- Upload: `POST /submission/algo` (singular)
- List: `GET /submissions/algo/{roundId}?page=1&pageSize=50` (plural)
- Graph: `GET /submissions/algo/{submissionId}/graph` (plural)

### Key discoveries
- PEPPER limit = **80** in Round 2 (not 50)
- Platform simulation is **deterministic** for identical code
- Platform PnL variance between sessions: ~300-500 (different market conditions per session)

### Baseline strategies found
| Submission | Strategy | PnL |
|-----------|----------|-----|
| 182338 | PEPPER=80 buy-and-hold, ASH OFF | 7,286 |
| 182541 | PEPPER=80 B&H + ASH default (te=2, hs=6, bs=15) | 9,800+ |

---

## Phase 2: Simple Grid Search (hours 2-4)

### Old v4o grid (killed after 5 results — wrong template)
- Only tested L1_size=8, k_inv=1.0 region
- Best: 6,322 (take_edge_sell=3)

### 182338 simple grid (base_size=5,8 with hs and te sweep)
8 results at base_size=8, all above 9,800:

| Rank | base_size | half_spread | take_edge | PnL |
|------|-----------|-------------|-----------|-----|
| 1 | 8 | 3 | 0 | **9,967** |
| 2 | 8 | 3 | 2 | 9,952 |
| 3 | 8 | 4 | 0 | 9,944 |
| 4 | 8 | 5 | 0 | 9,911 |

**Pattern**: tighter spread + lower take_edge = higher PnL

---

## Phase 3: ASH v2 Complex Framework (hours 4-12)

### Built
- `ash_config.py`: 31-param always-skewed ASH config with LHC sampling
- `ash_engines.py`: modular signal/take/passive/risk engines
- `codegen_ash_v2.py`: self-contained strategy generator
- `staged_search.py`: coarse LHC → local refinement → robustness

### Stage 1: 200 LHC configs (hours 4-11)
- **Best: PnL = 9,666** (micro=0.2, imb=0.2, skew=0.5, inv_k=3.0)
- Median: ~8,600
- 38% configs above 9,000

### Critical finding: parameter ranges
| Parameter | Dangerous range | Safe range |
|-----------|----------------|------------|
| imbalance_beta | > 1.0 → catastrophic loss | 0.0 - 0.5 |
| base_skew | > ±1.0 → guaranteed loss | -0.5 to +0.5 |
| inventory_skew_k | > 8.0 → absurd | 1.0 - 4.0 |
| micro_beta | > 2.0 → noise chasing | 0.0 - 0.5 |

### Stage 2: Refinement (hours 11-13)
- 3 rounds of neighborhood perturbation
- Best improved to **PnL = 9,757** (bid_hs=8 added)
- True mean for leader config: ~9,600 ± 180

### Verdict: v2 complex (9,757) NEVER beat simple grid (9,967)

---

## Phase 4: Structured Search — Steps 1-3 (hours 13-15)

### Step 1: base_size sweep (hs=3, te=0, fixed)
| base_size | PnL |
|-----------|-----|
| 8 | 9,967 |
| 10 | 10,090 |
| 12 | 10,045 |
| 15 | 10,088 |
| 18 | 10,088 |
| **20** | **10,139** |
| 25 | 10,000 |
| 30 | 10,041 |

**Winner: base_size=20**

### Step 2: half_spread refinement around winner
| base_size | hs=1 | hs=2 | hs=3 | hs=4 | hs=5 |
|-----------|------|------|------|------|------|
| 18 | 10,309 | 10,243 | 10,088 | 10,080 | 10,034 |
| **20** | **10,338** | 10,276 | 10,139 | 10,124 | 10,078 |
| 25 | — | 10,149 | 10,000 | 9,979 | 9,995 |

**Winner: bs=20, hs=1 → PnL 10,338**

### Step 3: k_inv sweep at winner
| k_inv | PnL |
|-------|-----|
| 1.5 | 10,277 |
| 2.0 | 10,276 |
| 2.5 | 10,276 |
| 3.0 | 10,281 |
| 3.5 | 10,286 |

**k_inv is irrelevant** — all within 10 PnL

---

## Phase 5: Advanced Features (hours 15-16)

### Position-aware taking
| Config | PnL | Delta |
|--------|-----|-------|
| **te=1 when |pos|<30%, else 0** | **10,428** | **+90** |
| te=2 when <30% | 10,199 | -139 |
| te=1 when <50% | 10,300 | -38 |

### Dynamic sizing → REJECTED
| Config | PnL | Delta |
|--------|-----|-------|
| floor=0.2 | 10,080 | -258 |
| floor=0.4 | 10,159 | -179 |
| floor=0.6 | 10,287 | -51 |

### Signal features → ALL REJECTED
| Feature | Best PnL | Delta |
|---------|----------|-------|
| Microprice (k=0.3-1.0) | 9,732 | **-696** |
| Inv-skew quoting (α=0.5-2.0) | 10,041 | -387 |
| Regime detection (EMA 3/10, 5/20) | 10,041 | -387 |
| Combined micro+skew | 9,698 | -730 |

---

## Phase 6: First Test Matrix — 41 configs (hours 16-17.5)

### Sections tested
- Neutral-zone taking refinement (14 configs)
- Asymmetric quoting (13 configs)
- Neutral schedules (7 configs)
- Portfolio coupling (6 configs)

### Results
- **Only 1 of 40 variants beat baseline**: NT_A2_4 at +9 (weak)
- **ask_hs must be 1**: every config with ask>1 lost exactly -72
- **take_edge ≥ 3 is toxic**: always -106
- **Defensive ASH under PEPPER load**: -204

---

## Phase 7: Mechanism Tests — 12 configs (hours 17.5-18)

| Config | PnL | Delta | Insight |
|--------|-----|-------|---------|
| SIZE ≥10 | 10,120 | +80 | Size plateau above 10 |
| UNDERCUT+TAKE | 10,094 | +55 | Undercutting close to baseline |
| UNDERCUT only | 9,624 | -416 | Fair anchor essential |
| SIZE_5 | 9,545 | -494 | Too small |
| SIZE_1 | 8,193 | -1,846 | Barely quoting |
| BURST | 7,991 | -2,048 | Mode switching destroys edge |
| TAKE_ONLY | 7,790 | -2,249 | Passive quotes = core edge |
| ASH_OFF | 7,286 | -2,753 | PEPPER-only baseline |

### Key findings
- **ASH adds exactly 2,834 PnL** over PEPPER-only
- **Passive quoting = 2,330 PnL** (core edge)
- **Fair anchor = 496 PnL** (vs undercutting)
- **Size ≥10 all identical** — market depth caps fills

---

## Phase 8: S1-S5 Matrix — in progress (hour 18+)

### S1 two-band results (18/72 done)
| Take schedule | PnL | Delta |
|--------------|-----|-------|
| **te=1 always** | **10,071** | **+75** |
| te=0 always | 9,995 | 0 |
| te=1/<30% else 0 (baseline) | 9,995 | 0 |
| te=2 any schedule | 9,995-10,071 | 0 to +75 |

**Key insight**: Baseline's inventory gating never triggered — ASH stays <30% inventory. Constant te=1 is +75.

### Remaining: S2 (21), S3 (14), S4 (12), S5 (6) = 53 configs

---

## Current Best Configs

| Rank | Config | PnL | Session |
|------|--------|-----|---------|
| 1 | bs=20, hs=1, te=1 always (S1 finding) | ~10,071* | Current |
| 2 | bs=20, hs=1, te=1/<30% (position-aware) | 10,428 | Earlier session |
| 3 | bs=20, hs=1, te=0 | 10,338 | Earlier session |
| 4 | bs=20, hs=2, te=0 | 10,276 | Earlier session |

*Note: PnL varies by ~300 between sessions due to different market conditions.

---

## Infrastructure Built

```
prosperity-uploader/
  main.py              # CLI: upload, batch, poll, graph, analyze, list, resume
  config.py            # YAML config with confirmed endpoints
  auth.py              # Token management
  client.py            # HTTP client with retry/backoff
  uploader.py          # Multipart upload to platform
  submissions.py       # List/poll submissions
  graph.py             # Fetch signed S3 artifact URL
  artifact_parser.py   # Parse [timestamp, value] artifacts
  metrics.py           # PnL, drawdown, slope, volatility
  storage.py           # SQLite + CSV persistence
  models.py            # Data models
  optimizer/
    auto_auth.py       # Cognito auto-token refresh
    ash_config.py      # 31-param ASHConfig with LHC/random/perturb
    ash_engines.py     # Modular signal/take/passive/risk engines
    codegen.py         # v4o template generator
    codegen_180750.py  # 180750 template generator
    codegen_182338.py  # 182338 template generator (PEPPER=80 + ASH)
    codegen_ash_v2.py  # ASH v2 always-skewed generator
    codegen_advanced.py # Position-aware taking + dynamic sizing
    codegen_signals.py # Microprice, inv-skew quoting, regime detection
    codegen_matrix.py  # Unified matrix test generator
    test_matrix.py     # 41-config matrix (Sections 4-7)
    test_s1s5.py       # 72-config S1-S5 matrix
    run_s1s5.py        # S1-S5 runner
    staged_search.py   # 3-stage search pipeline
    grid.py            # Grid search
    adaptive.py        # Neighborhood + Optuna search
    analysis.py        # Parameter sensitivity
    runner.py          # Master CLI orchestrator
```

## Lessons Learned

1. **Simple beats complex**: 3-param simple ASH (9,967+) consistently outperforms 23-param v2 (9,757 max)
2. **Platform is deterministic**: Same code = same PnL. No need for repeated runs.
3. **Session variance is real**: Same config gives different absolute PnL across sessions (~300-500 range). Always compare deltas within a session.
4. **ask_hs=1 is sacred**: Any ask widening costs exactly -72 PnL
5. **Fair anchor is essential**: Quoting relative to 10,000 beats undercutting by ~500
6. **Passive quoting is the core edge**: ~2,300 PnL from passive vs 0 from taking alone
7. **Constant te=1 > gated te=1**: Inventory gating at 30% never triggers, costs 75 PnL
8. **Microprice/signals/regime detection all fail**: ASH is mean-reverting, any signal that moves fair from anchor = loss
