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
  - **Site score + burns blend** — `score × m1 + lifetimeFpBurned × m2`
  - **Custom component weights** — tune multipliers for staked FP, shields, lootboxes, dice, age, stars, **lifetime FP burned**
  - **Manual override** — edit per-wallet weights in a table
- Distribution methods: pro-rata, equal split, sqrt(weight)
- Summary metrics (Gini, HHI, top-10 share, min/median/max)
- Top-N chart, full allocation table, histogram, CSV download
- Refresh button re-fetches whitelist **and** lifetime FP burned (`fp-burns.json` sidecar)
- **Shareable seeds** (`imd1.` + zlib/base64url JSON): copy/load formula knobs; optional `?seed=` URL. Seeds lock the formula; whitelist data may drift.
- Burns source: GraphQL `consumeds` (all shop items except upgrade stake 1–5) × list FP + dice×0.2 FP + lootboxes×($2/FP_USD) + passes×($15/FP_USD). Treated as `lifetimeFpBurned`. Breakdown in `fp-burns.json`. **Not** `pet.fpSpent`/stakedFp. Caveat: catalog & FP_USD snapshots.
- **Historical foods (beer/apple/tea/…):** secondary guides (Odaily Nov 2023) list V1-era foods (beer 50 FP, etc.). On `api.pet.game`, every `isSell:false` consumed maps to catalog itemIds **0–21** only (no orphan ids; no Beer/Apple/Tea item names). Those V1 foods are **not** includable from this API without inventing itemId↔price maps, so they are not added to the burn sum. Fetch scans itemIds 0–64 and records any future unpriced ids in `fp-burns.json` (`unpricedItemIds` / `unpricedGlobalBuys`).

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
