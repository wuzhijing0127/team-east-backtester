# Team East Backtester

Backtester and data exploration toolkit for IMC Prosperity 4. Includes a backtesting engine, a Streamlit data dashboard for exploring raw market data, and an HTML visualizer for reviewing backtest results.

## Project Structure

```
team-east-backtester/
├── teameastbt/                  # Backtester engine (Python package)
│   ├── resources/               # Market data goes here
│   │   └── round0/             # One folder per round
│   │       ├── prices_round_0_day_-1.csv
│   │       ├── trades_round_0_day_-1.csv
│   │       └── ...
│   ├── __main__.py             # CLI entry point
│   ├── datamodel.py            # TradingState, Order, OrderDepth, etc.
│   ├── data.py                 # CSV parsing and position limits
│   ├── runner.py               # Backtest simulation loop
│   ├── metrics.py              # Sharpe, Sortino, max drawdown, Calmar
│   ├── models.py               # Log row types
│   ├── file_reader.py          # File abstraction layer
│   ├── open.py                 # Launches visualizer in browser
│   └── parse_submission_logs.py # Extract data from Prosperity submission logs
├── dashboard.py                 # Streamlit data explorer (raw market data)
├── visualizer.html              # HTML dashboard (backtest results)
├── sample_strategy.py           # Strategy template — start here
├── backtests/                   # Backtest output logs saved here
├── pyproject.toml
└── README.md
```

## Setup

### 1. Install Python dependencies

```bash
pip install -e .
```

Or install individually:

```bash
pip install ipython jsonpickle orjson tqdm typer streamlit plotly pandas
```

### 2. Add market data

When a new round is released, download the CSV files and place them in:

```
teameastbt/resources/round{N}/
```

Each round folder needs these files (naming must match exactly):

```
prices_round_{N}_day_{D}.csv    # Required — order book snapshots
trades_round_{N}_day_{D}.csv    # Optional — market trades
observations_round_{N}_day_{D}.csv  # Optional — conversion observations
```

Where `{N}` is the round number and `{D}` is the day number (can be negative, e.g. `-1`).

You also need an `__init__.py` (empty file) in each round folder:

```bash
# Windows (PowerShell)
New-Item teameastbt/resources/round1/__init__.py -ItemType File

# Mac/Linux
touch teameastbt/resources/round1/__init__.py
```

## Workflow

### Step 1: Explore the data

Launch the Streamlit dashboard to visualize raw market data:

```bash
streamlit run dashboard.py
```

This opens http://localhost:8501 in your browser. You'll see:
- Price time series with bid/ask bands
- Bid-ask spread over time
- Volume profile (bid vs ask)
- Market trades scatter plot
- Cross-product correlation and rolling correlation
- Order book imbalance

Use the sidebar to select round, day, and products. You can also point it at any data directory (e.g. the v3 backtester's resources folder).

### Step 2: Write your strategy

Copy `sample_strategy.py` and edit it:

```bash
cp sample_strategy.py my_strategy.py
```

Your strategy must have a `Trader` class with a `run` method:

```python
from datamodel import Order, OrderDepth, TradingState

class Trader:
    def run(self, state: TradingState):
        orders = {}
        conversions = 0
        trader_data = ""

        for product in state.order_depths:
            order_depth = state.order_depths[product]
            product_orders = []

            # Your logic here
            # Buy:  product_orders.append(Order(product, price, quantity))   # quantity > 0
            # Sell: product_orders.append(Order(product, price, quantity))   # quantity < 0

            orders[product] = product_orders

        return orders, conversions, trader_data
```

**Key data available in `state`:**

| Field | Type | Description |
|-------|------|-------------|
| `state.order_depths[product]` | `OrderDepth` | Current order book (`.buy_orders`, `.sell_orders` dicts of price→volume) |
| `state.position.get(product, 0)` | `int` | Your current position |
| `state.own_trades.get(product, [])` | `list[Trade]` | Your fills from the previous tick |
| `state.market_trades.get(product, [])` | `list[Trade]` | Other participants' trades from previous tick |
| `state.observations` | `Observation` | Conversion observations (if available) |
| `state.traderData` | `str` | The string you returned last tick (use for state persistence) |
| `state.timestamp` | `int` | Current timestamp |

**Persisting state across ticks:**

The `trader_data` string you return gets passed back as `state.traderData` on the next tick. Use JSON to store anything you need:

```python
import json

class Trader:
    def run(self, state: TradingState):
        # Load previous state
        if state.traderData:
            my_state = json.loads(state.traderData)
        else:
            my_state = {"prices": []}

        # ... your logic ...

        # Save state for next tick
        trader_data = json.dumps(my_state)
        return orders, conversions, trader_data
```

### Step 3: Run the backtest

```bash
# Run on all days in round 0
teameastbt my_strategy.py 0

# Run on a specific day
teameastbt my_strategy.py 0--1

# Run on multiple rounds
teameastbt my_strategy.py 0 1 2

# With live output from your print() statements
teameastbt my_strategy.py 0 --print

# Override position limits
teameastbt my_strategy.py 0 --limit PRODUCT_A:100 --limit PRODUCT_B:200

# Merge P&L across days
teameastbt my_strategy.py 0 --merge-pnl

# Skip saving output log
teameastbt my_strategy.py 0 --no-out

# Use data from a different directory
teameastbt my_strategy.py 0 --data /path/to/custom/data/
```

After the backtest finishes, you'll see:
- Per-product P&L
- Total profit
- Risk metrics (Sharpe, Sortino, max drawdown, Calmar ratio)
- Output log saved to `backtests/<timestamp>.log`

### Step 4: Visualize backtest results

**Option A — Local HTML visualizer:**

Open `visualizer.html` in your browser. Drag and drop the `.log` file from the `backtests/` folder. This shows:
- Order book levels with your trades overlaid
- Profit & Loss chart
- Position chart
- Sandbox logs (warnings, your print output)

**Option B — Open visualizer automatically after backtest:**

```bash
teameastbt my_strategy.py 0 --vis
```

This saves the log and opens it in the online Prosperity 3 visualizer.

### Step 5: Iterate

Repeat steps 2-4. Tweak parameters, test different approaches, check edge cases.

## CLI Reference

```
teameastbt [OPTIONS] ALGORITHM DAYS...
```

| Argument/Option | Description |
|----------------|-------------|
| `ALGORITHM` | Path to your `.py` file with a `Trader` class |
| `DAYS` | Round numbers (`0`, `1`) or specific days (`0--1`, `1-2`) |
| `--merge-pnl` | Accumulate P&L across days |
| `--vis` | Open results in visualizer when done |
| `--out FILE` | Custom output log path |
| `--no-out` | Don't save output log |
| `--data DIR` | Use custom data directory instead of bundled resources |
| `--print` | Print your trader's stdout output live |
| `--match-trades MODE` | `all` (default), `worse`, or `none` — controls trade matching |
| `--no-progress` | Hide progress bars |
| `--original-timestamps` | Don't merge timestamps across days |
| `--limit PRODUCT:N` | Override position limit (repeatable) |
| `--version` / `-v` | Show version |
| `--help` / `-h` | Show help |

## Extracting Data from Submission Logs

If you have submission logs from the Prosperity platform, you can extract CSV data:

```bash
python -m teameastbt.parse_submission_logs path/to/submission.log 1 0
```

This creates `prices_round_1_day_0.csv` and `trades_round_1_day_0.csv` in the `teameastbt/resources/round1/` folder, ready for backtesting.

## Tips

- **Position limits** default to 50 for unknown products. Use `--limit` to override, or edit `LIMITS` in `teameastbt/data.py` once the official limits are announced.
- **The dashboard works with any data directory** — point it at the v3 backtester's resources folder to explore old Prosperity 3 data too.
- **`from datamodel import ...`** works in your strategy files automatically — no need to reference the package name.
- **Backtest logs** in `backtests/` can be loaded into both the local `visualizer.html` and the online Prosperity 3 visualizer.
