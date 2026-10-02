# IMD Whitelist Distribution Simulator

Simulate distributing IMD tokens across the [Fren Pet Analytica](https://fpanalytica.tech) whitelist using site scores or custom weights.

**This is a simulation only — not an official airdrop.**

Data source: `https://fpanalytica.tech:3080/whitelist-data`

## Setup

```bash
cd /workspace/imd-whitelist-sim
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
```

Then open http://localhost:8501 (or the host URL if remote).

## Features

- Default pool: 300,000 IMD (editable)
- Weight modes:
  - **Site score** — use each wallet's Analytica `score`
  - **Custom component weights** — tune multipliers for staked FP, shields, lootboxes, dice, age, stars
  - **Manual override** — edit per-wallet weights in a table
- Distribution methods: pro-rata, equal split, sqrt(weight)
- Summary metrics (Gini, HHI, top-10 share, min/median/max)
- Top-N chart, full allocation table, histogram, CSV download
- Refresh button to re-fetch live whitelist data

## Verify allocation math

With default 300k + site scores, top wallet (score 185320 / sum ≈ 1,872,539) should receive ≈ 29,690 IMD.

## Virtualenv (recommended)

```bash
cd /workspace/imd-whitelist-sim
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
```
