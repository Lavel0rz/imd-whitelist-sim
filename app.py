"""IMD Whitelist Distribution Simulator for Fren Pet Analytica."""

from __future__ import annotations

import base64
import json
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
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
SEED_PREFIX = "imd1."

# Site-ish defaults (from blurb):
# score uses staked FP per alive pet, shields at 6 FP each (2x) => 12 FP-eq per shield,
# lootboxes at $1.50 each (2x) => 3 USD-eq; convert via FP USD price,
# dice 0.5x, age 0.1x, stars 0.1x.
# lifetimeFpBurned: 1.0 = Shroom-burn FP (catalog) counts 1:1 in custom weight.
DEFAULT_MULT = {
    "stakedFp": 1.0,
    "shieldsPurchased": 12.0,  # 6 FP * 2x
    "lootboxesOpened": 3.0,  # $1.50 * 2x (USD face; optional FP-price convert below)
    "diceGamesEntered": 0.5,
    "longestPetAliveDays": 0.1,
    "stars": 0.1,
    "lifetimeFpBurned": 1.0,
}

MODE_OPTIONS = ["site", "site_blend", "custom", "manual"]
METHOD_OPTIONS = ["pro-rata", "sqrt", "equal"]


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
            "field": "consumeds.Shroom",
            "burns": {},
            "mushroomFeeds": {},
            "fpItemSpend": {},
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


def fetch_item_catalog() -> dict[int, dict]:
    """Map itemId → {name, priceFp} from api.pet.game (price is FP wei / 1e18)."""
    payload = _graphql("{ items(limit: 50) { items { id name price } } }")
    items = (((payload.get("data") or {}).get("items") or {}).get("items")) or []
    out: dict[int, dict] = {}
    for it in items:
        try:
            iid = int(it["id"])
            out[iid] = {
                "name": str(it.get("name") or iid),
                "priceFp": float(it.get("price") or 0) / 1e18,
            }
        except (TypeError, ValueError, KeyError):
            continue
    return out


def fetch_burns_for_whitelist(whitelist: dict, max_workers: int = 16) -> dict:
    """Estimate lifetime FP burned from real Shroom buy+burn activity (not pet.fpSpent).

    Method (api.pet.game GraphQL `consumeds`):
      - Count rows with giver=wallet, itemId=0 (Shroom), isSell=false
      - lifetimeFpBurned = count × current catalog Shroom price (FP wei/1e18)
      - Docs: FP used to buy mushrooms is 100% burned

    Also estimates fpItemSpend ≈ burned + shieldsPurchased×shield list price (whitelist
    field; shields are buy-for-spend, mostly not burned).

    Caveat: catalog prices are current list FP; historical USD→FP sizes may differ.
    pet.fpSpent is stake/principal and is intentionally unused.
    """
    catalog = fetch_item_catalog()
    if 0 not in catalog:
        raise RuntimeError("Shroom item (id=0) missing from api.pet.game catalog")
    shroom_price = float(catalog[0]["priceFp"])
    shield_price = float(catalog.get(6, {}).get("priceFp") or 6.0)

    query = """
    query($g: String!) {
      consumeds(where: { giver: $g, itemId: 0, isSell: false }) { totalCount }
    }
    """

    wallets = [str(w["wallet"]) for w in (whitelist.get("wallets") or [])]
    shield_by_wallet = {
        str(w["wallet"]): float(w.get("shieldsPurchased") or 0)
        for w in (whitelist.get("wallets") or [])
    }
    lower_map = {a.lower(): a for a in wallets}

    burns: dict[str, float] = {}
    feeds: dict[str, int] = {}
    spend: dict[str, float] = {}

    def one(addr: str) -> tuple[str, int]:
        payload = _graphql(query, {"g": addr}, timeout=45.0)
        n = int((((payload.get("data") or {}).get("consumeds") or {}).get("totalCount")) or 0)
        return addr, n

    errors = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [pool.submit(one, a) for a in wallets]
        for fut in as_completed(futs):
            try:
                addr, shrooms = fut.result()
            except Exception:
                errors += 1
                continue
            key = lower_map.get(addr.lower(), addr)
            feeds[key] = int(shrooms)
            burned = float(shrooms) * shroom_price
            burns[key] = round(burned, 6)
            shields = float(shield_by_wallet.get(key) or shield_by_wallet.get(addr) or 0.0)
            # Approximate buy-for-spend: mushroom burns + shield purchases at list FP
            spend[key] = round(burned + shields * shield_price, 6)

    missing = 0
    for addr in wallets:
        if addr not in burns:
            burns[addr] = 0.0
            feeds[addr] = 0
            shields = float(shield_by_wallet.get(addr) or 0.0)
            spend[addr] = round(shields * shield_price, 6)
            missing += 1

    nonzero = sum(1 for v in burns.values() if v > 0)
    return {
        "source": GRAPHQL_URL,
        "method": "consumeds(giver,itemId=0 Shroom,isSell:false) × catalog list price FP",
        "field": "consumeds.Shroom (itemId=0)",
        "burnItemIds": [0],
        "burnItemNames": ["Shroom"],
        "unit": "FP (catalog list price)",
        "shroomPriceFp": shroom_price,
        "shieldPriceFp": shield_price,
        "itemPricesFp": {str(i): catalog[i]["priceFp"] for i in sorted(catalog)},
        "itemNames": {str(i): catalog[i]["name"] for i in sorted(catalog)},
        "note": (
            "lifetimeFpBurned = Shroom feed count × catalog Shroom FP price (100% burn per docs). "
            "fpItemSpend ≈ burns + whitelist shieldsPurchased × shield list price (spend, not all burned). "
            "NOT pet.fpSpent (stake/principal). Catalog prices are current; history may vary."
        ),
        "updatedAt": int(time.time()),
        "walletCount": len(burns),
        "walletsWithBurns": nonzero,
        "matchedWhitelistWallets": len(wallets) - missing,
        "missingBurnDataWallets": missing,
        "fetchErrors": errors,
        "burns": burns,
        "mushroomFeeds": feeds,
        "fpItemSpend": spend,
    }



def _map_lookup(m: dict, addr: str, default=0.0):
    if addr in m:
        return m[addr]
    lower = {str(k).lower(): v for k, v in m.items()}
    return lower.get(str(addr).lower(), default)


def wallets_df(data: dict, burns_meta: dict | None = None) -> pd.DataFrame:
    burns_meta = burns_meta or {}
    burns_map = burns_meta.get("burns") or {}
    feeds_map = burns_meta.get("mushroomFeeds") or {}
    spend_map = burns_meta.get("fpItemSpend") or {}
    rows = []
    for w in data.get("wallets", []):
        addr = w["wallet"]
        rows.append(
            {
                "wallet": addr,
                "rank": w.get("rank"),
                "score": float(w.get("score") or 0),
                "stakedFp": float(w.get("stakedFp") or 0),  # staked principal — NOT burn
                "shieldsPurchased": float(w.get("shieldsPurchased") or 0),
                "lootboxesOpened": float(w.get("lootboxesOpened") or 0),
                "diceGamesEntered": float(w.get("diceGamesEntered") or 0),
                "longestPetAliveDays": float(w.get("longestPetAliveDays") or 0),
                "stars": float(w.get("stars") or 0),
                "alivePetCount": float(w.get("alivePetCount") or 0),
                "lifetimeFpBurned": float(_map_lookup(burns_map, addr, 0.0) or 0),
                "mushroomFeeds": float(_map_lookup(feeds_map, addr, 0) or 0),
                "fpItemSpend": float(_map_lookup(spend_map, addr, 0.0) or 0),
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


def data_fingerprint(data: dict) -> dict:
    return {
        "updatedAt": data.get("updatedAt"),
        "walletCount": int(data.get("walletCount") or len(data.get("wallets") or [])),
        "alivePetCount": data.get("alivePetCount"),
    }


def encode_seed(payload: dict) -> str:
    """Deterministic portable seed: imd1. + base64url(zlib(json))."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    compressed = zlib.compress(raw, level=9)
    token = base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
    return SEED_PREFIX + token


def decode_seed(seed: str) -> dict:
    s = (seed or "").strip()
    if not s:
        raise ValueError("Empty seed")
    if s.startswith(SEED_PREFIX):
        token = s[len(SEED_PREFIX) :]
    else:
        # allow bare base64url zlib blob
        token = s
    pad = "=" * (-len(token) % 4)
    try:
        compressed = base64.urlsafe_b64decode(token + pad)
        raw = zlib.decompress(compressed)
        payload = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"Invalid seed: {e}") from e
    if not isinstance(payload, dict):
        raise ValueError("Seed payload must be an object")
    if int(payload.get("v", 0)) != 1:
        raise ValueError(f"Unsupported seed version: {payload.get('v')}")
    return payload


def build_seed_payload(
    *,
    total_pool: float,
    dist_method: str,
    mode: str,
    top_n: int,
    mult: dict,
    blend_score: float,
    blend_burn: float,
    lootbox_as_fp: bool,
    fp_usd: float,
    manual_map: dict[str, float] | None,
    data: dict,
) -> dict:
    payload: dict = {
        "v": 1,
        "pool": float(total_pool),
        "method": dist_method,
        "mode": mode,
        "top_n": int(top_n),
        "data": data_fingerprint(data),
    }
    if mode == "site_blend":
        payload["blend"] = {"score": float(blend_score), "burn": float(blend_burn)}
    if mode == "custom":
        payload["mult"] = {k: float(mult[k]) for k in DEFAULT_MULT}
        payload["lootbox_as_fp"] = bool(lootbox_as_fp)
        payload["fp_usd"] = float(fp_usd)
    if mode == "manual":
        # Full wallet→weight map (compact JSON; zlib keeps seed portable)
        payload["manual"] = {str(k): float(v) for k, v in (manual_map or {}).items()}
    return payload


def widget_state_keys() -> list[str]:
    return [
        "total_pool",
        "dist_method",
        "weight_mode",
        "top_n",
        "blend_score_mult",
        "blend_burn_mult",
        "lootbox_as_fp",
        "fp_usd",
        *[f"mult_{k}" for k in DEFAULT_MULT],
    ]


def _set_widget_value(key: str, value) -> None:
    """Assign a widget-backed session key.

    Streamlit can ignore in-place overwrites of existing widget keys in some
    cases; deleting first makes the next widget instantiation pick up `value`.
    Must run *before* the keyed widget is rendered in this script run.
    """
    if key in st.session_state:
        del st.session_state[key]
    st.session_state[key] = value


def ensure_saved_mult() -> dict[str, float]:
    """Return a complete saved_mult dict (DEFAULT_MULT keys), repairing all-zero wipes."""
    saved_raw = st.session_state.get("saved_mult") or {}
    saved: dict[str, float] = {}
    for k, default in DEFAULT_MULT.items():
        try:
            saved[k] = float(saved_raw[k]) if k in saved_raw else float(default)
        except (TypeError, ValueError):
            saved[k] = float(default)
    if all(v == 0.0 for v in saved.values()) and any(float(v) != 0.0 for v in DEFAULT_MULT.values()):
        saved = {k: float(v) for k, v in DEFAULT_MULT.items()}
    st.session_state["saved_mult"] = saved
    return saved


def hydrate_custom_mult_widgets(*, force: bool = False) -> None:
    """Copy saved_mult → mult_* keys only when missing or force (enter Custom / seed).

    Never call with force=True on ordinary reruns after the user edits a number_input —
    that snaps edits back to saved/defaults.
    """
    saved = ensure_saved_mult()
    for k, val in saved.items():
        sk = f"mult_{k}"
        if force or sk not in st.session_state:
            st.session_state[sk] = float(val)
    if force or "lootbox_as_fp" not in st.session_state:
        st.session_state["lootbox_as_fp"] = bool(st.session_state.get("saved_lootbox_as_fp", False))
    if force or "fp_usd" not in st.session_state:
        st.session_state["fp_usd"] = float(st.session_state.get("saved_fp_usd", 1.0))


def hydrate_blend_widgets(*, force: bool = False) -> None:
    bs = float(st.session_state.get("saved_blend_score", 1.0))
    bb = float(st.session_state.get("saved_blend_burn", 1.0))
    if force or "blend_score_mult" not in st.session_state:
        st.session_state["blend_score_mult"] = bs
    if force or "blend_burn_mult" not in st.session_state:
        st.session_state["blend_burn_mult"] = bb


def current_knob_snapshot() -> dict:
    return {
        "pool": float(st.session_state.get("total_pool", DEFAULT_POOL)),
        "method": st.session_state.get("dist_method", "pro-rata"),
        "mode": st.session_state.get("weight_mode", "site"),
        "top_n": int(st.session_state.get("top_n", 15)),
        "blend_score": float(st.session_state.get("blend_score_mult", 1.0)),
        "blend_burn": float(st.session_state.get("blend_burn_mult", 1.0)),
        "lootbox_as_fp": bool(st.session_state.get("lootbox_as_fp", False)),
        "fp_usd": float(st.session_state.get("fp_usd", 1.0)),
        "mult": {
            k: float((st.session_state.get("saved_mult") or {}).get(k, st.session_state.get(f"mult_{k}", DEFAULT_MULT[k])))
            for k in DEFAULT_MULT
        },
    }


def seed_knob_snapshot(payload: dict) -> dict:
    blend = payload.get("blend") or {}
    mult = payload.get("mult") or {}
    return {
        "pool": float(payload.get("pool", DEFAULT_POOL)),
        "method": payload.get("method", "pro-rata"),
        "mode": payload.get("mode", "site"),
        "top_n": int(payload.get("top_n", 15)),
        "blend_score": float(blend.get("score", 1.0)),
        "blend_burn": float(blend.get("burn", 1.0)),
        "lootbox_as_fp": bool(payload.get("lootbox_as_fp", False)),
        "fp_usd": float(payload.get("fp_usd", 1.0)),
        "mult": {k: float(mult.get(k, DEFAULT_MULT[k])) for k in DEFAULT_MULT},
    }


def format_knob_summary(snap: dict) -> str:
    parts = [
        f"mode=`{snap['mode']}`",
        f"method=`{snap['method']}`",
        f"pool={snap['pool']:g}",
        f"top_n={snap['top_n']}",
    ]
    if snap["mode"] == "site_blend":
        parts.append(f"blend score×{snap['blend_score']:g} burn×{snap['blend_burn']:g}")
    if snap["mode"] == "custom":
        m = snap["mult"]
        parts.append(
            "mult("
            + ", ".join(f"{k}={m[k]:g}" for k in DEFAULT_MULT)
            + ")"
        )
        if snap["lootbox_as_fp"]:
            parts.append(f"lootbox→FP @ ${snap['fp_usd']:g}")
    if snap["mode"] == "manual":
        parts.append("manual weights")
    return " · ".join(parts)


def apply_seed_to_session(payload: dict, wallets: list[str]) -> list[str]:
    """Write seed knobs into session_state. Returns human warnings."""
    warnings: list[str] = []
    mode = payload.get("mode", "site")
    if mode not in MODE_OPTIONS:
        raise ValueError(f"Unknown mode: {mode}")
    method = payload.get("method", "pro-rata")
    if method not in METHOD_OPTIONS:
        raise ValueError(f"Unknown method: {method}")

    _set_widget_value("total_pool", float(payload.get("pool", DEFAULT_POOL)))
    _set_widget_value("dist_method", method)
    _set_widget_value("weight_mode", mode)
    _set_widget_value("top_n", int(payload.get("top_n", 15)))

    blend = payload.get("blend") or {}
    bs = float(blend.get("score", 1.0))
    bb = float(blend.get("burn", 1.0))
    st.session_state["saved_blend_score"] = bs
    st.session_state["saved_blend_burn"] = bb
    _set_widget_value("blend_score_mult", bs)
    _set_widget_value("blend_burn_mult", bb)

    mult = payload.get("mult") or {}
    # Source of truth for custom multipliers (survives Streamlit clearing unused widget keys).
    saved = {k: float(mult.get(k, DEFAULT_MULT[k])) for k in DEFAULT_MULT}
    st.session_state["saved_mult"] = saved
    for k, val in saved.items():
        _set_widget_value(f"mult_{k}", float(val))
    _set_widget_value("lootbox_as_fp", bool(payload.get("lootbox_as_fp", False)))
    _set_widget_value("fp_usd", float(payload.get("fp_usd", 1.0)))
    st.session_state["saved_lootbox_as_fp"] = bool(payload.get("lootbox_as_fp", False))
    st.session_state["saved_fp_usd"] = float(payload.get("fp_usd", 1.0))
    # Next Custom/blend paint should re-hydrate widget keys from saved_* once.
    st.session_state["_hydrate_custom_mult"] = True
    st.session_state["_hydrate_blend"] = True

    if mode == "manual":
        manual = payload.get("manual") or {}
        # Align to current wallet list; missing → 0
        lower = {a.lower(): a for a in wallets}
        rows = []
        used = set()
        for raw_addr, weight in manual.items():
            key = lower.get(str(raw_addr).lower())
            if key is None:
                continue
            rows.append({"wallet": key, "weight": float(weight)})
            used.add(key)
        for addr in wallets:
            if addr not in used:
                rows.append({"wallet": addr, "weight": 0.0})
        st.session_state.manual_df = pd.DataFrame(rows)
        st.session_state.data_version = int(st.session_state.get("data_version", 0)) + 1
        missing = len(wallets) - len(used)
        if missing:
            warnings.append(
                f"Manual seed covered {len(used)}/{len(wallets)} current wallets; "
                f"{missing} missing set to 0."
            )
    return warnings


def queue_seed_from_input() -> None:
    """Button callback: queue pasted seed for apply on the next run (before widgets)."""
    st.session_state["_pending_seed"] = st.session_state.get("seed_load_input") or ""


def init_session_defaults() -> None:
    defaults = {
        "manual_df": None,
        "data_version": 0,
        "total_pool": float(DEFAULT_POOL),
        "dist_method": "pro-rata",
        "weight_mode": "site",
        "top_n": 15,
        "blend_score_mult": 1.0,
        "blend_burn_mult": 1.0,
        "lootbox_as_fp": False,
        "fp_usd": 1.0,
        "seed_load_input": "",
        "seed_fingerprint_warn": None,
        "seed_apply_warnings": [],
        "seed_apply_banner": None,
        "_seed_url_consumed": False,
        "saved_mult": dict(DEFAULT_MULT),
        "saved_lootbox_as_fp": False,
        "saved_fp_usd": 1.0,
        "saved_blend_score": 1.0,
        "saved_blend_burn": 1.0,
        "_prev_weight_mode": None,
        "_hydrate_custom_mult": False,
        "_hydrate_blend": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v
    # Ensure saved_mult always has every DEFAULT_MULT key
    saved = st.session_state.get("saved_mult") or {}
    fixed = {k: float(saved[k]) if k in saved else float(v) for k, v in DEFAULT_MULT.items()}
    st.session_state["saved_mult"] = fixed
    for k, v in fixed.items():
        sk = f"mult_{k}"
        if sk not in st.session_state:
            st.session_state[sk] = float(v)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="IMD Whitelist Sim",
    page_icon="🐾",
    layout="wide",
    initial_sidebar_state="expanded",
)

init_session_defaults()

st.title("🐾 IMD Whitelist Distribution Simulator")
st.caption(
    f"Source: [{DATA_URL}]({DATA_URL}) · Burns: [{GRAPHQL_URL}]({GRAPHQL_URL}) Shroom `consumeds` × list FP price (not `fpSpent`) · "
    "Simulation only — **not** an official airdrop. "
    "Score blurb: staked FP per alive pet; shields 6 FP × 2x; lootboxes $1.50 × 2x; "
    "dice 0.5x; age 0.1x; stars 0.1x. "
    "**Seeds lock the formula/knobs; whitelist/score snapshots may drift.**"
)

# Load whitelist early so seed apply can align manual maps
try:
    data = load_local()
except Exception as e:
    st.error(f"Failed to load local whitelist JSON: {e}")
    st.stop()

wallet_list = [w["wallet"] for w in data.get("wallets") or []]

# Consume ?seed= once per session (or when newly present)
try:
    qp_seed = st.query_params.get("seed")
except Exception:
    qp_seed = None
if isinstance(qp_seed, list):
    qp_seed = qp_seed[0] if qp_seed else None
if qp_seed and not st.session_state.get("_seed_url_consumed"):
    st.session_state["_pending_seed"] = str(qp_seed)
    st.session_state["_seed_url_consumed"] = True

if st.session_state.get("_pending_seed") is not None:
    raw_seed = st.session_state.get("_pending_seed")
    # Clear immediately to avoid loops if we rerun after apply.
    st.session_state["_pending_seed"] = None
    try:
        if not str(raw_seed or "").strip():
            raise ValueError("Paste an imd1.… seed first")
        before = current_knob_snapshot()
        payload = decode_seed(str(raw_seed))
        after = seed_knob_snapshot(payload)
        warns = apply_seed_to_session(payload, wallet_list)
        fp = payload.get("data") or {}
        cur = data_fingerprint(data)
        if fp and (
            fp.get("updatedAt") != cur.get("updatedAt")
            or int(fp.get("walletCount") or -1) != int(cur.get("walletCount") or -2)
        ):
            st.session_state["seed_fingerprint_warn"] = (
                f"Seed data fingerprint differs from current whitelist "
                f"(seed updatedAt={fp.get('updatedAt')}, wallets={fp.get('walletCount')}; "
                f"now updatedAt={cur.get('updatedAt')}, wallets={cur.get('walletCount')}). "
                "Knobs applied; IMD amounts may differ."
            )
        else:
            st.session_state["seed_fingerprint_warn"] = None
        st.session_state["seed_apply_warnings"] = warns
        same = before == after and payload.get("mode") != "manual"
        defaultish = (
            after["mode"] == "site"
            and after["method"] == "pro-rata"
            and float(after["pool"]) == float(DEFAULT_POOL)
            and int(after["top_n"]) == 15
        )
        summary = format_knob_summary(after)
        if same and defaultish:
            banner = (
                f"Seed applied — knobs already matched defaults "
                f"({summary}). UI/allocation look unchanged; that is expected."
            )
        elif same:
            banner = f"Seed applied — knobs already matched current UI ({summary})."
        else:
            banner = f"Seed applied — {summary}"
        st.session_state["seed_apply_banner"] = banner
        # Rerun so keyed widgets instantiate against the new session_state values.
        st.rerun()
    except ValueError as e:
        st.session_state["seed_fingerprint_warn"] = f"Could not load seed: {e}"
        st.session_state["seed_apply_banner"] = None

# Sidebar: pool, method, refresh
with st.sidebar:
    st.header("Pool & method")
    total_pool = st.number_input(
        "Total IMD pool",
        min_value=0.0,
        step=1000.0,
        format="%.0f",
        key="total_pool",
    )
    dist_method = st.selectbox(
        "Distribution method",
        options=METHOD_OPTIONS,
        format_func=lambda m: {
            "pro-rata": "Pro-rata (weight)",
            "sqrt": "Sqrt(weight)",
            "equal": "Equal split",
        }[m],
        key="dist_method",
        help="Primary allocation uses the selected method. Pro-rata is weight_i / sum(weights) × pool.",
    )
    top_n = st.slider("Top N chart", min_value=5, max_value=50, step=1, key="top_n")

    st.divider()
    st.subheader("Data")
    if st.button("🔄 Refresh whitelist + burns", use_container_width=True):
        try:
            with st.spinner("Fetching whitelist…"):
                remote = fetch_remote()
            save_local(remote)
            with st.spinner("Fetching Shroom burns + item spend (GraphQL consumeds)…"):
                burns_payload = fetch_burns_for_whitelist(remote)
            save_burns(burns_payload)
            st.session_state.data_version += 1
            st.session_state.manual_df = None
            st.session_state["_seed_url_consumed"] = False
            st.success(
                f"Updated · {remote.get('walletCount')} wallets · "
                f"alive pets {remote.get('alivePetCount')} · "
                f"burn wallets {burns_payload.get('walletsWithBurns')} · "
                f"shroom price {burns_payload.get('shroomPriceFp')} FP · "
                f"errors {burns_payload.get('fetchErrors', 0)}"
            )
            st.rerun()
        except requests.Timeout:
            st.error("Request timed out. Local JSON unchanged.")
        except requests.RequestException as e:
            st.error(f"Fetch failed: {e}. Local JSON unchanged.")
        except (OSError, json.JSONDecodeError, RuntimeError) as e:
            st.error(f"Could not save/parse data: {e}")

    st.divider()
    st.subheader("Share seed")
    st.caption(
        "Seeds encode formula knobs (not RNG). "
        "Same seed + same data ⇒ same allocation. "
        "If whitelist scores change later, amounts can drift."
    )
    st.text_area("Paste seed", key="seed_load_input", height=80, placeholder="imd1.…")
    st.button(
        "Apply seed",
        use_container_width=True,
        type="primary",
        on_click=queue_seed_from_input,
        help="Loads formula knobs from the seed (before widgets render on the next run).",
    )

burns_meta = load_burns()

df = wallets_df(data, burns_meta)
if df.empty:
    st.warning("No wallets in whitelist data.")
    st.stop()

if st.session_state.get("seed_apply_banner"):
    st.success(st.session_state["seed_apply_banner"])
if st.session_state.get("seed_fingerprint_warn"):
    st.warning(st.session_state["seed_fingerprint_warn"])
for wmsg in st.session_state.get("seed_apply_warnings") or []:
    st.info(wmsg)

meta_cols = st.columns(5)
meta_cols[0].metric("Wallets", int(data.get("walletCount") or len(df)))
meta_cols[1].metric("Alive pets", int(data.get("alivePetCount") or 0))
meta_cols[2].metric("Site score sum", f"{df['score'].sum():,.0f}")
meta_cols[3].metric("FP burned sum", f"{df['lifetimeFpBurned'].sum():,.0f}")
updated = data.get("updatedAt")
burns_updated = burns_meta.get("updatedAt")
meta_cols[4].metric("updatedAt (unix)", updated if updated is not None else "—")

zero_burns = int((df["lifetimeFpBurned"] <= 0).sum())
if not (burns_meta.get("burns") or burns_meta.get("updatedAt")):
    st.warning(
        "No burn cache yet (`fp-burns.json`). Click **Refresh whitelist + burns** "
        "or treat lifetime FP burned as 0 for all wallets."
    )
else:
    st.info(
        f"Burn source: `{burns_meta.get('method') or burns_meta.get('field')}` "
        f"(Shroom list price {burns_meta.get('shroomPriceFp')} FP; updatedAt={burns_updated}). "
        f"{burns_meta.get('walletsWithBurns', len(df) - zero_burns)} wallets with burns; "
        f"{zero_burns} at 0 — treated as 0. "
        "True Shroom burns only — **not** `pet.fpSpent` (stake/principal)."
    )

st.divider()

mode = st.radio(
    "Weight mode",
    options=MODE_OPTIONS,
    format_func=lambda m: {
        "site": "A. Site score",
        "site_blend": "A2. Site score + burns blend",
        "custom": "B. Custom component weights",
        "manual": "C. Manual override table",
    }[m],
    horizontal=True,
    key="weight_mode",
)

weights = pd.Series(dtype=float)
blend_score = float(st.session_state.get("blend_score_mult", 1.0))
blend_burn = float(st.session_state.get("blend_burn_mult", 1.0))
mult = {k: float(st.session_state.get(f"mult_{k}", DEFAULT_MULT[k])) for k in DEFAULT_MULT}
lootbox_as_fp = bool(st.session_state.get("lootbox_as_fp", False))
fp_usd = float(st.session_state.get("fp_usd", 1.0))
manual_map: dict[str, float] | None = None

if mode == "site":
    weights = df["score"].copy()
    st.info("Using each wallet's Analytica `score` as weight.")

elif mode == "site_blend":
    st.markdown(
        "Blend Analytica site score with lifetime FP burned. "
        "`weight = score × scoreMult + lifetimeFpBurned × burnMult`."
    )
    entering_blend = st.session_state.get("_prev_weight_mode") != "site_blend"
    force_blend = entering_blend or bool(st.session_state.pop("_hydrate_blend", False))
    hydrate_blend_widgets(force=force_blend)
    b1, b2 = st.columns(2)
    with b1:
        blend_score = st.number_input("score ×", min_value=0.0, step=0.1, format="%.4f", key="blend_score_mult")
    with b2:
        blend_burn = st.number_input(
            "lifetimeFpBurned ×",
            min_value=0.0,
            step=0.1,
            format="%.4f",
            key="blend_burn_mult",
            help="Shroom feeds × catalog FP price (100% burn). Not stake/fpSpent.",
        )
    st.session_state["saved_blend_score"] = float(blend_score)
    st.session_state["saved_blend_burn"] = float(blend_burn)
    weights = (df["score"] * float(blend_score) + df["lifetimeFpBurned"] * float(blend_burn)).clip(lower=0)

elif mode == "custom":
    st.markdown("Edit multipliers. Defaults approximate the site formula; burns default 1.0× FP.")
    # Hydrate widget keys from saved_mult ONLY when entering Custom or after a seed apply.
    # Overwriting mult_* on every rerun snaps user edits back to defaults.
    entering_custom = st.session_state.get("_prev_weight_mode") != "custom"
    force_custom = entering_custom or bool(st.session_state.pop("_hydrate_custom_mult", False))
    hydrate_custom_mult_widgets(force=force_custom)

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        m_staked = st.number_input(
            "stakedFp × (staked, not burn)",
            min_value=0.0,
            step=0.1,
            format="%.4f",
            key="mult_stakedFp",
            help="Whitelist staked FP principal — not lifetime burn.",
        )
        m_shields = st.number_input(
            "shieldsPurchased ×",
            min_value=0.0,
            step=0.1,
            format="%.4f",
            key="mult_shieldsPurchased",
            help="Site: 6 FP × 2x = 12",
        )
    with c2:
        m_loot = st.number_input(
            "lootboxesOpened ×",
            min_value=0.0,
            step=0.1,
            format="%.4f",
            key="mult_lootboxesOpened",
            help="Site: $1.50 × 2x = 3 (USD face)",
        )
        m_dice = st.number_input(
            "diceGamesEntered ×",
            min_value=0.0,
            step=0.1,
            format="%.4f",
            key="mult_diceGamesEntered",
        )
    with c3:
        m_age = st.number_input(
            "longestPetAliveDays ×",
            min_value=0.0,
            step=0.01,
            format="%.4f",
            key="mult_longestPetAliveDays",
        )
        m_stars = st.number_input(
            "stars ×",
            min_value=0.0,
            step=0.01,
            format="%.4f",
            key="mult_stars",
        )
    with c4:
        m_burn = st.number_input(
            "lifetimeFpBurned ×",
            min_value=0.0,
            step=0.1,
            format="%.4f",
            key="mult_lifetimeFpBurned",
            help="Shroom consumeds × list FP price. True burns only (item Shroom).",
        )

    lootbox_as_fp = st.checkbox(
        "Convert lootbox USD face → FP using FP USD price",
        key="lootbox_as_fp",
        help="If on, loot contribution = lootboxes × (loot_mult / FP_USD). "
        "If off, loot_mult is applied raw (default 3 ≈ $1.50×2).",
    )
    fp_usd = 1.0
    if lootbox_as_fp:
        fp_usd = st.number_input(
            "FP price (USD)",
            min_value=1e-9,
            step=0.01,
            format="%.6f",
            key="fp_usd",
        )
    else:
        fp_usd = float(st.session_state.get("fp_usd", st.session_state.get("saved_fp_usd", 1.0)))

    mult = {
        "stakedFp": float(m_staked),
        "shieldsPurchased": float(m_shields),
        "lootboxesOpened": float(m_loot),
        "diceGamesEntered": float(m_dice),
        "longestPetAliveDays": float(m_age),
        "stars": float(m_stars),
        "lifetimeFpBurned": float(m_burn),
    }
    # Persist so leaving custom mode does not lose multipliers to widget teardown
    st.session_state["saved_mult"] = dict(mult)
    st.session_state["saved_lootbox_as_fp"] = bool(lootbox_as_fp)
    st.session_state["saved_fp_usd"] = float(fp_usd)
    weights = custom_weights(df, mult, lootbox_as_fp, fp_usd)

else:  # manual
    st.markdown("Edit weights (or paste %). Click **Reset from Site score** / **Custom defaults** to refill.")
    rc1, rc2 = st.columns(2)
    reset_site = rc1.button("Reset from Site score", use_container_width=True)
    reset_custom = rc2.button("Reset from Custom defaults", use_container_width=True)

    if reset_site:
        base = df[["wallet", "score"]].rename(columns={"score": "weight"}).copy()
        st.session_state.manual_df = base
        st.session_state.data_version = int(st.session_state.get("data_version", 0)) + 1
    elif st.session_state.manual_df is None:
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
    manual_map = {str(r.wallet): float(r.weight) for r in edited.itertuples(index=False)}

# Remember mode so next run can detect Custom/blend entry (one-shot hydrate).
st.session_state["_prev_weight_mode"] = mode

# Build allocation
alloc = allocate(weights, float(total_pool), dist_method)
out = df[["wallet", "rank", "score", "lifetimeFpBurned", "mushroomFeeds", "fpItemSpend", "stakedFp"]].copy()
out["weight"] = weights.values
wsum = float(out["weight"].sum())
out["weight_pct"] = (out["weight"] / wsum * 100.0) if wsum > 0 else 0.0
out["imd"] = alloc.values
pool = float(total_pool)
out["pct_of_pool"] = (out["imd"] / pool * 100.0) if pool > 0 else 0.0
out = out.sort_values("imd", ascending=False).reset_index(drop=True)

# Current shareable seed (formula knobs + optional data fingerprint)
seed_payload = build_seed_payload(
    total_pool=float(total_pool),
    dist_method=str(dist_method),
    mode=str(mode),
    top_n=int(top_n),
    mult=mult,
    blend_score=float(blend_score),
    blend_burn=float(blend_burn),
    lootbox_as_fp=bool(lootbox_as_fp),
    fp_usd=float(fp_usd),
    manual_map=manual_map,
    data=data,
)
current_seed = encode_seed(seed_payload)

with st.expander("🌱 Shareable seed (Copy / URL)", expanded=False):
    st.markdown(
        "Copy this seed and send it to someone — they paste it under **Share seed → Apply seed** "
        "(sidebar), or open `?seed=…` on the app URL. "
        "The seed **locks the formula** (pool, mode, method, multipliers / manual weights). "
        "It does **not** freeze whitelist scores; if data refreshes, IMD can change slightly."
    )
    st.code(current_seed, language=None)
    sc1, sc2 = st.columns(2)
    with sc1:
        st.download_button(
            "📋 Download seed.txt",
            data=current_seed + "\n",
            file_name="imd_distribution_seed.txt",
            mime="text/plain",
            use_container_width=True,
        )
    with sc2:
        if st.button("🔗 Put seed in URL (?seed=)", use_container_width=True):
            st.query_params["seed"] = current_seed
            st.success("URL query param `seed` updated — copy the browser address bar to share.")
    st.caption(
        f"Fingerprint in seed: updatedAt={seed_payload['data'].get('updatedAt')}, "
        f"wallets={seed_payload['data'].get('walletCount')}, "
        f"alivePets={seed_payload['data'].get('alivePetCount')} · length={len(current_seed)} chars"
    )

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
        "pct_of_pool": ":.4f",
        "lifetimeFpBurned": ":.2f",
        "mushroomFeeds": True,
        "fpItemSpend": ":.2f",
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
display["mushroomFeeds"] = display["mushroomFeeds"].map(lambda v: int(float(v)))
display["fpItemSpend"] = display["fpItemSpend"].map(lambda v: round(float(v), 6))
display["stakedFp"] = display["stakedFp"].map(lambda v: round(float(v), 6))
display["imd"] = display["imd"].map(lambda v: round(float(v), 6))
display["pct_of_pool"] = display["pct_of_pool"].map(lambda v: round(float(v), 4))
# Column order: put % of pool next to IMD
cols = [c for c in display.columns if c != "pct_of_pool"]
if "imd" in cols:
    i = cols.index("imd") + 1
    cols = cols[:i] + ["pct_of_pool"] + cols[i:]
else:
    cols = cols + ["pct_of_pool"]
display = display[cols]
st.dataframe(
    display,
    use_container_width=True,
    height=420,
    column_config={
        "pct_of_pool": st.column_config.NumberColumn(
            "% of pool",
            help="100 × IMD / total pool — share of the distribution each wallet receives.",
            format="%.4f%%",
        ),
        "weight_pct": st.column_config.NumberColumn("weight %", format="%.4f"),
        "imd": st.column_config.NumberColumn("imd", format="%.4f"),
    },
)

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
    glance = df[["wallet", "score", "lifetimeFpBurned", "mushroomFeeds", "fpItemSpend", "stakedFp"]].copy()
    c_a, c_b = st.columns(2)
    with c_a:
        st.markdown("**Top 10 by lifetime FP burned** (Shroom × list price)")
        st.dataframe(
            glance.sort_values("lifetimeFpBurned", ascending=False).head(10).reset_index(drop=True),
            use_container_width=True,
        )
    with c_b:
        st.markdown("**Top 10 by site score** (stakedFp = stake, not burn)")
        st.dataframe(
            glance.sort_values("score", ascending=False).head(10).reset_index(drop=True),
            use_container_width=True,
        )
