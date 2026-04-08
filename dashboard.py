import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from pathlib import Path
import re

st.set_page_config(page_title="Team East Data Explorer", layout="wide")

DEFAULT_DATA_DIR = str(Path(__file__).parent / "teameastbt" / "resources")


@st.cache_data
def discover_rounds_and_days(data_dir: str) -> dict[int, list[int]]:
    root = Path(data_dir)
    if not root.is_dir():
        return {}
    result: dict[int, list[int]] = {}
    for f in root.glob("round*/prices_round_*_day_*.csv"):
        m = re.search(r"prices_round_(\d+)_day_(-?\d+)\.csv", f.name)
        if m:
            r, d = int(m.group(1)), int(m.group(2))
            result.setdefault(r, []).append(d)
    for r in result:
        result[r] = sorted(result[r])
    return result


@st.cache_data
def load_prices(data_dir: str, round_num: int, day: int) -> pd.DataFrame:
    path = Path(data_dir) / f"round{round_num}" / f"prices_round_{round_num}_day_{day}.csv"
    df = pd.read_csv(path, sep=";")
    for col in ["bid_price_1", "bid_volume_1", "bid_price_2", "bid_volume_2",
                 "bid_price_3", "bid_volume_3", "ask_price_1", "ask_volume_1",
                 "ask_price_2", "ask_volume_2", "ask_price_3", "ask_volume_3",
                 "mid_price", "profit_and_loss"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


@st.cache_data
def load_trades(data_dir: str, round_num: int, day: int) -> pd.DataFrame | None:
    path = Path(data_dir) / f"round{round_num}" / f"trades_round_{round_num}_day_{day}.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, sep=";")
    df.rename(columns={"symbol": "product"}, inplace=True)
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
    return df


# ── Sidebar ──────────────────────────────────────────────────────────────────

st.sidebar.title("Team East Data Explorer")

data_dir = st.sidebar.text_input("Data Directory", value=DEFAULT_DATA_DIR)
rounds = discover_rounds_and_days(data_dir)

if not rounds:
    st.error(f"No data found in `{data_dir}`. Check the path or drop CSV files into the resources folder.")
    st.stop()

round_num = st.sidebar.selectbox("Round", sorted(rounds.keys()))
day = st.sidebar.selectbox("Day", rounds[round_num])

prices_df = load_prices(data_dir, round_num, day)
trades_df = load_trades(data_dir, round_num, day)

products = sorted(prices_df["product"].unique())
selected = st.sidebar.multiselect("Products", products, default=products)

if not selected:
    st.warning("Select at least one product.")
    st.stop()

pdf = prices_df[prices_df["product"].isin(selected)].copy()
tdf = trades_df[trades_df["product"].isin(selected)].copy() if trades_df is not None else None

st.title(f"Round {round_num} · Day {day}")

# ── 1. Price Time Series ────────────────────────────────────────────────────

st.subheader("1. Price Time Series")

fig = make_subplots(rows=len(selected), cols=1, shared_xaxes=True,
                    subplot_titles=selected, vertical_spacing=0.06)

for i, product in enumerate(selected, 1):
    p = pdf[pdf["product"] == product].sort_values("timestamp")
    ts = p["timestamp"]

    fig.add_trace(go.Scatter(
        x=ts, y=p["ask_price_1"], mode="lines",
        line=dict(color="rgba(255,82,82,0.3)", width=0),
        showlegend=False, name="Ask 1",
    ), row=i, col=1)

    fig.add_trace(go.Scatter(
        x=ts, y=p["bid_price_1"], mode="lines",
        line=dict(color="rgba(76,175,80,0.3)", width=0),
        fill="tonexty", fillcolor="rgba(33,150,243,0.12)",
        showlegend=False, name="Bid 1",
    ), row=i, col=1)

    fig.add_trace(go.Scatter(
        x=ts, y=p["mid_price"], mode="lines",
        line=dict(color="#1976D2", width=1.5),
        name=product, showlegend=True,
    ), row=i, col=1)

fig.update_layout(height=350 * len(selected), xaxis_title="Timestamp",
                  legend=dict(orientation="h"), margin=dict(t=40, b=30))
st.plotly_chart(fig, use_container_width=True)

# ── 2. Spread Analysis ──────────────────────────────────────────────────────

st.subheader("2. Bid-Ask Spread")

fig2 = go.Figure()
for product in selected:
    p = pdf[pdf["product"] == product].sort_values("timestamp")
    spread = p["ask_price_1"] - p["bid_price_1"]
    fig2.add_trace(go.Scatter(
        x=p["timestamp"], y=spread, mode="lines", name=product,
    ))

fig2.update_layout(height=350, xaxis_title="Timestamp", yaxis_title="Spread",
                   legend=dict(orientation="h"), margin=dict(t=20, b=30))
st.plotly_chart(fig2, use_container_width=True)

# ── 3. Volume Profile ───────────────────────────────────────────────────────

st.subheader("3. Volume Profile (Total Bid vs Ask Volume)")

fig3 = make_subplots(rows=len(selected), cols=1, shared_xaxes=True,
                     subplot_titles=selected, vertical_spacing=0.06)

for i, product in enumerate(selected, 1):
    p = pdf[pdf["product"] == product].sort_values("timestamp")
    ts = p["timestamp"]
    bid_vol = p[["bid_volume_1", "bid_volume_2", "bid_volume_3"]].sum(axis=1)
    ask_vol = p[["ask_volume_1", "ask_volume_2", "ask_volume_3"]].sum(axis=1)

    fig3.add_trace(go.Scatter(
        x=ts, y=bid_vol, mode="lines", name=f"{product} Bid Vol",
        line=dict(color="#4CAF50"), fill="tozeroy",
        fillcolor="rgba(76,175,80,0.2)",
    ), row=i, col=1)

    fig3.add_trace(go.Scatter(
        x=ts, y=-ask_vol, mode="lines", name=f"{product} Ask Vol",
        line=dict(color="#F44336"), fill="tozeroy",
        fillcolor="rgba(244,67,54,0.2)",
    ), row=i, col=1)

fig3.update_layout(height=350 * len(selected), xaxis_title="Timestamp",
                   legend=dict(orientation="h"), margin=dict(t=40, b=30))
st.plotly_chart(fig3, use_container_width=True)

# ── 4. Market Trades ────────────────────────────────────────────────────────

st.subheader("4. Market Trades")

if tdf is not None and len(tdf) > 0:
    fig4 = px.scatter(tdf, x="timestamp", y="price", size="quantity",
                      color="product", hover_data=["quantity", "buyer", "seller"],
                      size_max=18, opacity=0.7)
    fig4.update_layout(height=400, xaxis_title="Timestamp", yaxis_title="Trade Price",
                       legend=dict(orientation="h"), margin=dict(t=20, b=30))
    st.plotly_chart(fig4, use_container_width=True)
else:
    st.info("No trade data available for this round/day.")

# ── 5. Cross-Product Correlation ────────────────────────────────────────────

st.subheader("5. Cross-Product Correlation")

if len(selected) >= 2:
    pivot = pdf.pivot_table(index="timestamp", columns="product", values="mid_price")
    pivot = pivot[selected]

    normed = (pivot - pivot.mean()) / pivot.std()
    fig5a = go.Figure()
    for col in normed.columns:
        fig5a.add_trace(go.Scatter(
            x=normed.index, y=normed[col], mode="lines", name=col,
        ))
    fig5a.update_layout(height=350, xaxis_title="Timestamp",
                        yaxis_title="Normalized Price (z-score)",
                        legend=dict(orientation="h"), margin=dict(t=20, b=30))
    st.plotly_chart(fig5a, use_container_width=True)

    corr = pivot.corr()
    fig5b = px.imshow(corr, text_auto=".3f", color_continuous_scale="RdBu_r",
                      zmin=-1, zmax=1, aspect="auto")
    fig5b.update_layout(height=350, margin=dict(t=20, b=30))
    st.plotly_chart(fig5b, use_container_width=True)

    if len(selected) == 2:
        window = st.slider("Rolling correlation window", 10, 500, 100, step=10)
        rolling_corr = pivot[selected[0]].rolling(window).corr(pivot[selected[1]])
        fig5c = go.Figure()
        fig5c.add_trace(go.Scatter(
            x=rolling_corr.index, y=rolling_corr.values, mode="lines",
            name=f"{selected[0]} vs {selected[1]}",
            line=dict(color="#9C27B0"),
        ))
        fig5c.add_hline(y=0, line_dash="dash", line_color="gray")
        fig5c.update_layout(height=300, xaxis_title="Timestamp",
                            yaxis_title="Rolling Correlation",
                            margin=dict(t=20, b=30))
        st.plotly_chart(fig5c, use_container_width=True)
else:
    st.info("Select 2+ products to see cross-product correlation.")

# ── 6. Order Book Imbalance ─────────────────────────────────────────────────

st.subheader("6. Order Book Imbalance (Bid Vol - Ask Vol)")

fig6 = make_subplots(rows=len(selected), cols=1, shared_xaxes=True,
                     subplot_titles=selected, vertical_spacing=0.06)

for i, product in enumerate(selected, 1):
    p = pdf[pdf["product"] == product].sort_values("timestamp")
    ts = p["timestamp"]
    bid_vol = p[["bid_volume_1", "bid_volume_2", "bid_volume_3"]].sum(axis=1)
    ask_vol = p[["ask_volume_1", "ask_volume_2", "ask_volume_3"]].sum(axis=1)
    imbalance = bid_vol - ask_vol

    colors = ["rgba(76,175,80,0.5)" if v >= 0 else "rgba(244,67,54,0.5)" for v in imbalance]

    fig6.add_trace(go.Bar(
        x=ts, y=imbalance, marker_color=colors, name=product, showlegend=True,
    ), row=i, col=1)

    fig6.add_hline(y=0, line_dash="dash", line_color="gray", row=i, col=1)

fig6.update_layout(height=350 * len(selected), xaxis_title="Timestamp",
                   bargap=0, legend=dict(orientation="h"),
                   margin=dict(t=40, b=30))
st.plotly_chart(fig6, use_container_width=True)
