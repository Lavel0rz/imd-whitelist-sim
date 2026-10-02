"""IMD Whitelist Distribution Simulator for Fren Pet Analytica."""

from __future__ import annotations

import json
import time
from io import StringIO
from pathlib import Path

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

DATA_URL = "https://fpanalytica.tech:3080/whitelist-data"
GRAPHQL_URL = "https://api.pet.game"
LOCAL_JSON = Path(__file__).resolve().parent / "fp-whitelist.json"
BURNS_JSON = Path(__file__).resolve().parent / "fp-burns.json"
FALLBACK_JSON = Path("/workspace/fp-whitelist.json")
DEFAULT_POOL = 300_000.0

# Site-ish defaults (from blurb):
# score uses staked FP per alive pet, shields at 6 FP each (2x) => 12 FP-eq per shield,
# lootboxes at $1.50 each (2x) => 3 USD-eq; convert via FP USD price,
# dice 0.5x, age 0.1x, stars 0.1x.
# lifetimeFpBurned: 1.0 so each burned FP counts 1:1 in custom weight (FP units).
DEFAULT_MULT = {
    "stakedFp": 1.0,
    "shieldsPurchased": 12.0,  # 6 FP * 2x
    "lootboxesOpened": 3.0,  # $1.50 * 2x (USD face; optional FP-price convert below)
    "diceGamesEntered": 0.5,
    "longestPetAliveDays": 0.1,
    "stars": 0.1,
    "lifetimeFpBurned": 1.0,
}


def load_local() -> dict:
    path = LOCAL_JSON if LOCAL_JSON.exists() else FALLBACK_JSON
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_local(data: dict) -> None:
    LOCAL_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCAL_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f)


def load_burns() -> dict:
    """Load sidecar burns cache. Returns empty burns map if missing."""
    if not BURNS_JSON.exists():
        return {
            "source": GRAPHQL_URL,
            "field": "pet.fpSpent",
            "burns": {},
            "missingBurnDataWallets": None,
        }
    with open(BURNS_JSON, encoding="utf-8") as f:
        return json.load(f)


def save_burns(data: dict) -> None:
    BURNS_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(BURNS_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f)


def fetch_remote(timeout: float = 20.0) -> dict:
    r = requests.get(DATA_URL, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _graphql(query: str, variables: dict | None = None, timeout: float = 90.0) -> dict:
    r = requests.post(
        GRAPHQL_URL,
        json={"query": query, "variables": variables or {}},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=timeout,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(payload["errors"][0].get("message") or str(payload["errors"][0]))
    return payload


def fetch_burns_for_whitelist(whitelist: dict, batch_size: int = 100) -> dict:
    """Fetch pet.fpSpent from api.pet.game GraphQL and aggregate to wallet.

    `fpSpent` is lifetime FP spent per pet (wei, 18 decimals). Mushroom buys burn
    100% of FP; this is the best public per-pet lifetime burn/spend signal.
    """
    pet_ids: list[int] = []
    for w in whitelist.get("wallets") or []:
        for p in w.get("pets") or []:
            try:
                pet_ids.append(int(p["petId"]))
            except (KeyError, TypeError, ValueError):
                continue
    pet_ids = sorted(set(pet_ids))

    query = """
    query($ids: [Int!], $limit: Int!) {
      pets(where: { id_in: $ids }, limit: $limit) {
        items { id owner fpSpent }
      }
    }
    """
    by_pet: dict[int, dict] = {}
    for i in range(0, len(pet_ids), batch_size):
        chunk = pet_ids[i : i + batch_size]
        if not chunk:
            continue
        payload = _graphql(query, {"ids": chunk, "limit": len(chunk)})
        items = (((payload.get("data") or {}).get("pets") or {}).get("items")) or []
        for it in items:
            try:
                pid = int(it["id"])
                spent = float(it["fpSpent"]) / 1e18
            except (KeyError, TypeError, ValueError):
                continue
            by_pet[pid] = {"owner": it.get("owner") or "", "fpSpent": spent}

    wallet_totals: dict[str, float] = {}
    for info in by_pet.values():
        owner = info["owner"]
        if not owner:
            continue
        wallet_totals[owner] = wallet_totals.get(owner, 0.0) + float(info["fpSpent"])

    # Map onto whitelist wallet casing; missing wallets => 0
    lower_map = {str(w["wallet"]).lower(): w["wallet"] for w in whitelist.get("wallets") or []}
    burns: dict[str, float] = {}
    for owner, amt in wallet_totals.items():
        key = lower_map.get(owner.lower(), owner)
        burns[key] = round(float(amt), 6)

    missing = 0
    for w in whitelist.get("wallets") or []:
        addr = w["wallet"]
        if addr not in burns:
            # Case-insensitive fallback
            found = burns.get(lower_map.get(addr.lower(), ""), None)
            if found is None and addr.lower() in {k.lower(): k for k in burns}:
                # already keyed differently
                pass
            if addr not in burns:
                burns[addr] = 0.0
                missing += 1

    return {
        "source": GRAPHQL_URL,
        "field": "pet.fpSpent",
        "unit": "FP (1e18 wei scaled)",
        "note": (
            "Aggregated pet.fpSpent from https://api.pet.game GraphQL. "
            "FP used to buy mushrooms is 100% burned; fpSpent is the public lifetime spend field."
        ),
        "updatedAt": int(time.time()),
        "petCount": len(by_pet),
        "walletCount": len(burns),
        "matchedWhitelistWallets": len(whitelist.get("wallets") or []) - missing,
        "missingBurnDataWallets": missing,
        "burns": burns,
    }


def wallets_df(data: dict, burns_map: dict[str, float] | None = None) -> pd.DataFrame:
    burns_map = burns_map or {}
    # Case-insensitive lookup
    burns_lower = {str(k).lower(): float(v) for k, v in burns_map.items()}
    rows = []
    for w in data.get("wallets", []):
        addr = w["wallet"]
        burned = burns_map.get(addr)
        if burned is None:
            burned = burns_lower.get(str(addr).lower(), 0.0)
        rows.append(
            {
                "wallet": addr,
                "rank": w.get("rank"),
                "score": float(w.get("score") or 0),
                "stakedFp": float(w.get("stakedFp") or 0),
                "shieldsPurchased": float(w.get("shieldsPurchased") or 0),
                "lootboxesOpened": float(w.get("lootboxesOpened") or 0),
                "diceGamesEntered": float(w.get("diceGamesEntered") or 0),
                "longestPetAliveDays": float(w.get("longestPetAliveDays") or 0),
                "stars": float(w.get("stars") or 0),
                "alivePetCount": float(w.get("alivePetCount") or 0),
                "lifetimeFpBurned": float(burned or 0),
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("score", ascending=False).reset_index(drop=True)
    return df


def custom_weights(df: pd.DataFrame, mult: dict, lootbox_as_fp: bool, fp_usd: float) -> pd.Series:
    """Weighted sum of components. Optionally convert lootbox USD face to FP via price."""
    loot_mult = float(mult["lootboxesOpened"])
    if lootbox_as_fp and fp_usd > 0:
        loot_contrib = df["lootboxesOpened"] * (loot_mult / fp_usd)
    else:
        loot_contrib = df["lootboxesOpened"] * loot_mult

    burned_col = df["lifetimeFpBurned"] if "lifetimeFpBurned" in df.columns else 0.0
    w = (
        df["stakedFp"] * float(mult["stakedFp"])
        + df["shieldsPurchased"] * float(mult["shieldsPurchased"])
        + loot_contrib
        + df["diceGamesEntered"] * float(mult["diceGamesEntered"])
        + df["longestPetAliveDays"] * float(mult["longestPetAliveDays"])
        + df["stars"] * float(mult["stars"])
        + burned_col * float(mult.get("lifetimeFpBurned", 0.0))
    )
    return w.clip(lower=0)


def allocate(weights: pd.Series, total: float, method: str) -> pd.Series:
    w = weights.astype(float).clip(lower=0)
    if method == "equal":
        n = max(len(w), 1)
        return pd.Series([total / n] * len(w), index=w.index)
    if method == "sqrt":
        w = w.pow(0.5)
    s = float(w.sum())
    if s <= 0:
        return pd.Series([0.0] * len(w), index=w.index)
    return w / s * total


def gini(values: pd.Series) -> float:
    """Gini coefficient of non-negative allocation amounts."""
    x = values.astype(float).to_numpy()
    if len(x) == 0:
        return 0.0
    if x.sum() <= 0:
        return 0.0
    x = sorted(x)
    n = len(x)
    cum = 0.0
    for i, v in enumerate(x, start=1):
        cum += i * v
    return (2 * cum) / (n * sum(x)) - (n + 1) / n


def herfindahl(shares: pd.Series) -> float:
    """Herfindahl-Hirschman Index on weight/IMD shares (0–1 scale)."""
    s = shares.astype(float)
    total = s.sum()
    if total <= 0:
        return 0.0
    p = s / total
    return float((p ** 2).sum())


def short_addr(a: str) -> str:
    if not isinstance(a, str) or len(a) < 12:
        return str(a)
    return f"{a[:6]}…{a[-4:]}"


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="IMD Whitelist Sim",
    page_icon="🐾",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("🐾 IMD Whitelist Distribution Simulator")
st.caption(
    f"Source: [{DATA_URL}]({DATA_URL}) · Burns: [{GRAPHQL_URL}]({GRAPHQL_URL}) `pet.fpSpent` · "
    "Simulation only — **not** an official airdrop. "
    "Score blurb: staked FP per alive pet; shields 6 FP × 2x; lootboxes $1.50 × 2x; "
    "dice 0.5x; age 0.1x; stars 0.1x."
)

# Session state bootstrap
if "manual_df" not in st.session_state:
    st.session_state.manual_df = None
if "data_version" not in st.session_state:
    st.session_state.data_version = 0

# Sidebar: pool, method, refresh
with st.sidebar:
    st.header("Pool & method")
    total_pool = st.number_input(
        "Total IMD pool",
        min_value=0.0,
        value=float(DEFAULT_POOL),
        step=1000.0,
        format="%.0f",
    )
    dist_method = st.selectbox(
        "Distribution method",
        options=["pro-rata", "sqrt", "equal"],
        format_func=lambda m: {
            "pro-rata": "Pro-rata (weight)",
            "sqrt": "Sqrt(weight)",
            "equal": "Equal split",
        }[m],
        index=0,
        help="Primary allocation uses the selected method. Pro-rata is weight_i / sum(weights) × pool.",
    )
    top_n = st.slider("Top N chart", min_value=5, max_value=50, value=15, step=1)

    st.divider()
    st.subheader("Data")
    if st.button("🔄 Refresh whitelist + burns", use_container_width=True):
        try:
            with st.spinner("Fetching whitelist…"):
                remote = fetch_remote()
            save_local(remote)
            with st.spinner("Fetching lifetime FP burned (GraphQL)…"):
                burns_payload = fetch_burns_for_whitelist(remote)
            save_burns(burns_payload)
            st.session_state.data_version += 1
            st.session_state.manual_df = None
            st.success(
                f"Updated · {remote.get('walletCount')} wallets · "
                f"alive pets {remote.get('alivePetCount')} · "
                f"burns pets {burns_payload.get('petCount')} · "
                f"missing burns {burns_payload.get('missingBurnDataWallets', 0)}"
            )
        except requests.Timeout:
            st.error("Request timed out. Local JSON unchanged.")
        except requests.RequestException as e:
            st.error(f"Fetch failed: {e}. Local JSON unchanged.")
        except (OSError, json.JSONDecodeError, RuntimeError) as e:
            st.error(f"Could not save/parse data: {e}")

try:
    data = load_local()
except Exception as e:
    st.error(f"Failed to load local whitelist JSON: {e}")
    st.stop()

burns_meta = load_burns()
burns_map = burns_meta.get("burns") or {}

df = wallets_df(data, burns_map)
if df.empty:
    st.warning("No wallets in whitelist data.")
    st.stop()

meta_cols = st.columns(5)
meta_cols[0].metric("Wallets", int(data.get("walletCount") or len(df)))
meta_cols[1].metric("Alive pets", int(data.get("alivePetCount") or 0))
meta_cols[2].metric("Site score sum", f"{df['score'].sum():,.0f}")
meta_cols[3].metric("FP burned sum", f"{df['lifetimeFpBurned'].sum():,.0f}")
updated = data.get("updatedAt")
burns_updated = burns_meta.get("updatedAt")
meta_cols[4].metric("updatedAt (unix)", updated if updated is not None else "—")

zero_burns = int((df["lifetimeFpBurned"] <= 0).sum())
if not burns_map:
    st.warning(
        "No burn cache yet (`fp-burns.json`). Click **Refresh whitelist + burns** "
        "or treat lifetime FP burned as 0 for all wallets."
    )
elif zero_burns:
    st.info(
        f"Burn source: `{burns_meta.get('source')}` field `{burns_meta.get('field')}` "
        f"(updatedAt={burns_updated}). "
        f"{zero_burns} wallet(s) have 0 burned FP (missing or truly zero) — treated as 0 in weights."
    )

st.divider()

mode = st.radio(
    "Weight mode",
    options=["site", "site_blend", "custom", "manual"],
    format_func=lambda m: {
        "site": "A. Site score",
        "site_blend": "A2. Site score + burns blend",
        "custom": "B. Custom component weights",
        "manual": "C. Manual override table",
    }[m],
    horizontal=True,
    index=0,
)

weights = pd.Series(dtype=float)

if mode == "site":
    weights = df["score"].copy()
    st.info("Using each wallet's Analytica `score` as weight.")

elif mode == "site_blend":
    st.markdown(
        "Blend Analytica site score with lifetime FP burned. "
        "`weight = score × scoreMult + lifetimeFpBurned × burnMult`."
    )
    b1, b2 = st.columns(2)
    with b1:
        score_mult = st.number_input("score ×", value=1.0, step=0.1, format="%.4f")
    with b2:
        burn_mult = st.number_input(
            "lifetimeFpBurned ×",
            value=1.0,
            step=0.1,
            format="%.4f",
            help="FP units from api.pet.game pet.fpSpent (aggregated per wallet).",
        )
    weights = (df["score"] * float(score_mult) + df["lifetimeFpBurned"] * float(burn_mult)).clip(lower=0)

elif mode == "custom":
    st.markdown("Edit multipliers. Defaults approximate the site formula; burns default 1.0× FP.")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        m_staked = st.number_input("stakedFp ×", value=DEFAULT_MULT["stakedFp"], step=0.1, format="%.4f")
        m_shields = st.number_input(
            "shieldsPurchased ×",
            value=DEFAULT_MULT["shieldsPurchased"],
            step=0.1,
            format="%.4f",
            help="Site: 6 FP × 2x = 12",
        )
    with c2:
        m_loot = st.number_input(
            "lootboxesOpened ×",
            value=DEFAULT_MULT["lootboxesOpened"],
            step=0.1,
            format="%.4f",
            help="Site: $1.50 × 2x = 3 (USD face)",
        )
        m_dice = st.number_input("diceGamesEntered ×", value=DEFAULT_MULT["diceGamesEntered"], step=0.1, format="%.4f")
    with c3:
        m_age = st.number_input(
            "longestPetAliveDays ×",
            value=DEFAULT_MULT["longestPetAliveDays"],
            step=0.01,
            format="%.4f",
        )
        m_stars = st.number_input("stars ×", value=DEFAULT_MULT["stars"], step=0.01, format="%.4f")
    with c4:
        m_burn = st.number_input(
            "lifetimeFpBurned ×",
            value=DEFAULT_MULT["lifetimeFpBurned"],
            step=0.1,
            format="%.4f",
            help="From api.pet.game GraphQL pet.fpSpent (wei→FP), summed per wallet.",
        )

    lootbox_as_fp = st.checkbox(
        "Convert lootbox USD face → FP using FP USD price",
        value=False,
        help="If on, loot contribution = lootboxes × (loot_mult / FP_USD). "
        "If off, loot_mult is applied raw (default 3 ≈ $1.50×2).",
    )
    fp_usd = 1.0
    if lootbox_as_fp:
        fp_usd = st.number_input("FP price (USD)", min_value=1e-9, value=1.0, step=0.01, format="%.6f")

    mult = {
        "stakedFp": m_staked,
        "shieldsPurchased": m_shields,
        "lootboxesOpened": m_loot,
        "diceGamesEntered": m_dice,
        "longestPetAliveDays": m_age,
        "stars": m_stars,
        "lifetimeFpBurned": m_burn,
    }
    weights = custom_weights(df, mult, lootbox_as_fp, fp_usd)

else:  # manual
    st.markdown("Edit weights (or paste %). Click **Reset from Site score** / **Custom defaults** to refill.")
    rc1, rc2 = st.columns(2)
    reset_site = rc1.button("Reset from Site score", use_container_width=True)
    reset_custom = rc2.button("Reset from Custom defaults", use_container_width=True)

    if reset_site or st.session_state.manual_df is None:
        base = df[["wallet", "score"]].rename(columns={"score": "weight"}).copy()
        st.session_state.manual_df = base
    if reset_custom:
        cw = custom_weights(df, DEFAULT_MULT, False, 1.0)
        base = df[["wallet"]].copy()
        base["weight"] = cw.values
        st.session_state.manual_df = base

    edited = st.data_editor(
        st.session_state.manual_df,
        num_rows="fixed",
        use_container_width=True,
        column_config={
            "wallet": st.column_config.TextColumn("wallet", disabled=True),
            "weight": st.column_config.NumberColumn("weight", min_value=0.0, format="%.4f"),
        },
        key=f"manual_editor_{st.session_state.data_version}",
    )
    st.session_state.manual_df = edited
    merged = df[["wallet"]].merge(edited[["wallet", "weight"]], on="wallet", how="left")
    weights = merged["weight"].fillna(0.0)

# Build allocation
alloc = allocate(weights, float(total_pool), dist_method)
out = df[["wallet", "rank", "score", "lifetimeFpBurned"]].copy()
out["weight"] = weights.values
wsum = float(out["weight"].sum())
out["weight_pct"] = (out["weight"] / wsum * 100.0) if wsum > 0 else 0.0
out["imd"] = alloc.values
out = out.sort_values("imd", ascending=False).reset_index(drop=True)

# Summary metrics
st.subheader("Summary")
top10_share = float(out["imd"].head(10).sum() / total_pool * 100) if total_pool > 0 else 0.0
g = gini(out["imd"])
hhi = herfindahl(out["imd"])

m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric("Wallet count", len(out))
m2.metric("Total weight", f"{wsum:,.2f}")
m3.metric("Top-10 share", f"{top10_share:.1f}%")
m4.metric("Gini", f"{g:.4f}")
m5.metric("HHI", f"{hhi:.4f}")
m6.metric(
    "IMD min / med / max",
    f"{out['imd'].min():,.1f} / {out['imd'].median():,.1f} / {out['imd'].max():,.1f}",
)

# Top N chart
st.subheader(f"Top {top_n} recipients")
top = out.head(top_n).copy()
top["label"] = top["wallet"].map(short_addr)
fig_bar = px.bar(
    top,
    x="label",
    y="imd",
    hover_data={
        "wallet": True,
        "weight": True,
        "weight_pct": ":.3f",
        "lifetimeFpBurned": ":.2f",
        "imd": ":.2f",
        "label": False,
    },
    labels={"label": "Wallet", "imd": "IMD"},
    title=f"Top {top_n} by IMD ({dist_method})",
)
fig_bar.update_layout(template="plotly_dark", xaxis_tickangle=-45, margin=dict(b=80))
st.plotly_chart(fig_bar, use_container_width=True)

# Histogram
st.subheader("IMD distribution histogram")
fig_hist = px.histogram(
    out,
    x="imd",
    nbins=40,
    labels={"imd": "IMD"},
    title="Histogram of IMD amounts",
)
fig_hist.update_layout(template="plotly_dark")
st.plotly_chart(fig_hist, use_container_width=True)

# Full table + download
st.subheader("Full allocation")
display = out.copy()
display["weight"] = display["weight"].map(lambda v: round(float(v), 6))
display["weight_pct"] = display["weight_pct"].map(lambda v: round(float(v), 6))
display["lifetimeFpBurned"] = display["lifetimeFpBurned"].map(lambda v: round(float(v), 6))
display["imd"] = display["imd"].map(lambda v: round(float(v), 6))
st.dataframe(display, use_container_width=True, height=420)

csv_buf = StringIO()
display.to_csv(csv_buf, index=False)
st.download_button(
    "⬇️ Download CSV",
    data=csv_buf.getvalue(),
    file_name="imd_whitelist_allocation.csv",
    mime="text/csv",
    use_container_width=True,
)

# Comparison samples (optional glance)
with st.expander("Compare alternate methods (same weights)"):
    cmp = df[["wallet"]].copy()
    for method, label in [("pro-rata", "imd_prorata"), ("sqrt", "imd_sqrt"), ("equal", "imd_equal")]:
        cmp[label] = allocate(weights, float(total_pool), method).values
    cmp = cmp.sort_values("imd_prorata", ascending=False).reset_index(drop=True)
    st.dataframe(cmp.head(25), use_container_width=True)

with st.expander("Top burners vs top score (glance)"):
    glance = df[["wallet", "score", "lifetimeFpBurned"]].copy()
    c_a, c_b = st.columns(2)
    with c_a:
        st.markdown("**Top 10 by lifetime FP burned**")
        st.dataframe(
            glance.sort_values("lifetimeFpBurned", ascending=False).head(10).reset_index(drop=True),
            use_container_width=True,
        )
    with c_b:
        st.markdown("**Top 10 by site score**")
        st.dataframe(
            glance.sort_values("score", ascending=False).head(10).reset_index(drop=True),
            use_container_width=True,
        )
