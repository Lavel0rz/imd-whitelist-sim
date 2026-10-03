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
  - **Site score + burns blend** — `score × m1 + burnFpSum × m2` (`burnFpSum` = shroom + shield + other shop + dice + loot + pass; not a separate source)
  - **Custom component weights** — site-style multipliers (staked FP, shields, lootboxes, dice, age, stars) plus an independent multiplier for each burn column: `shroomFp`, `shieldFp`, `otherShopFp`, `diceFp`, `lootFp`, `passFp`
  - **Manual override** — edit per-wallet weights in a table
- Distribution methods: pro-rata, equal split, sqrt(weight)
- Summary metrics (Gini, HHI, top-10 share, min/median/max)
- Top-N chart, full allocation table, histogram, CSV download
- Refresh button re-fetches whitelist **and** per-action FP burns (`fp-burns.json` sidecar)
- **Shareable seeds** (`imd1.` + zlib/base64url JSON): copy/load formula knobs; optional `?seed=` URL. Seeds lock the formula; whitelist data may drift.
- Burns are **separate columns**, not one `lifetimeFpBurned` total: `shroomFp` (shop Shroom), `shieldFp` (shop shields), `otherShopFp` (insurance + cosmetics), `diceFp` (×0.2), `lootFp` ($2/FP_USD), `passFp` ($15/FP_USD). `burnFpSum` is only those columns added together. Stake upgrades (items 1–5) and `pet.fpSpent`/`stakedFp` are excluded. Prices are the existing catalog / unit costs — nothing new. CSV includes every column.
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
