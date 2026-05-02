"""
5-minute collector. Writes TWO parquets per currency per day:
  data/oi/<CCY>/monthly/<YYYY-MM-DD>.parquet
  data/oi/<CCY>/weekly/<YYYY-MM-DD>.parquet

Each parquet has wide schema with relative-offset columns (C_-20_oi ... C_+20_oi
for monthly; weekly may have fewer offsets).

Per-row fields:
  ts (UTC timestamp, 5-min bucket)
  ts_iso
  ccy
  spot          (Deribit index price)
  perp          (Deribit perp mark)
  perp_oi       (Deribit perp open interest)
  perp_funding  (8h funding)
  expiry        (e.g. "8MAY26")
  expiry_kind   ("weekly" | "monthly")
  atm_strike
  strike_spacing
  stale_pick_days   (0 = today's pick; >0 = fell back to earlier day)
  prev_gap_minutes  (since previous snapshot in this day's file)
  is_clean          (True if gap was within ~5min)

  For each offset O in (-N..+N):
    C_O_oi, C_O_iv, C_O_volume, C_O_mark, C_O_delta, C_O_gamma, C_O_theta, C_O_vega
    P_O_... same fields

Atomic write: tmp file then rename. Dedup by ts (skip if bucket already saved).
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from deribit import book_summary, get_index_price, get_ticker

CCY_INDEX = {"BTC": "btc_usd", "ETH": "eth_usd"}
PERP = {"BTC": "BTC-PERPETUAL", "ETH": "ETH-PERPETUAL"}
GREEK_FIELDS = ["delta", "gamma", "theta", "vega"]


# ---------- helpers ----------

def round_to_5min(t: datetime) -> datetime:
    return t.replace(minute=(t.minute // 5) * 5, second=0, microsecond=0)


def latest_strike_pick(strikes_root: Path, ccy: str, today_utc: datetime) -> dict | None:
    cand = strikes_root / ccy / f"{today_utc.date().isoformat()}.json"
    if cand.exists():
        return json.loads(cand.read_text())
    for days_back in range(1, 15):
        prev = today_utc - timedelta(days=days_back)
        cand = strikes_root / ccy / f"{prev.date().isoformat()}.json"
        if cand.exists():
            data = json.loads(cand.read_text())
            data["_stale_pick_days"] = days_back
            return data
    return None


# ---------- option chain ----------

def fetch_chain(ccy: str) -> dict[str, dict]:
    rows = book_summary(ccy, kind="option")
    out: dict[str, dict] = {}
    for r in rows:
        out[r["instrument_name"]] = {
            "oi": float(r.get("open_interest") or 0.0),
            "iv": float(r.get("mark_iv") or 0.0) / 100.0,
            "volume_24h": float(r.get("volume") or 0.0),
            "mark_price": float(r.get("mark_price") or 0.0),
            "underlying_price": float(r.get("underlying_price") or 0.0),
        }
    return out


def fetch_greeks(instrument: str) -> dict[str, float]:
    try:
        t = get_ticker(instrument)
        g = t.get("greeks") or {}
        return {f: float(g.get(f) or 0.0) for f in GREEK_FIELDS}
    except Exception:
        return {f: 0.0 for f in GREEK_FIELDS}


# ---------- per-pick row builder ----------

def build_row(ccy: str, pick_block: dict, expiry_kind: str,
              chain: dict[str, dict], spot: float,
              perp_price: float, perp_oi: float, perp_funding: float,
              bucket: datetime, stale_days: int) -> dict:
    """Build one wide row for a given pick (weekly or monthly)."""
    row: dict[str, Any] = {
        "ts": bucket,
        "ts_iso": bucket.isoformat(),
        "ccy": ccy,
        "expiry_kind": expiry_kind,
        "spot": spot,
        "perp": perp_price,
        "perp_oi": perp_oi,
        "perp_funding": perp_funding,
        "atm_strike": pick_block["atm_strike"],
        "strike_spacing": pick_block["strike_spacing"],
        "expiry": pick_block["expiry"],
        "stale_pick_days": stale_days,
    }
    for inst in pick_block["instruments"]:
        offset = inst["offset"]
        for side in ("C", "P"):
            instr = inst[side]
            base = chain.get(instr) if instr else None
            if base is None:
                row[f"{side}_{offset:+d}_oi"] = 0.0
                row[f"{side}_{offset:+d}_iv"] = 0.0
                row[f"{side}_{offset:+d}_volume"] = 0.0
                row[f"{side}_{offset:+d}_mark"] = 0.0
                for g in GREEK_FIELDS:
                    row[f"{side}_{offset:+d}_{g}"] = 0.0
                continue
            row[f"{side}_{offset:+d}_oi"] = base["oi"]
            row[f"{side}_{offset:+d}_iv"] = base["iv"]
            row[f"{side}_{offset:+d}_volume"] = base["volume_24h"]
            row[f"{side}_{offset:+d}_mark"] = base["mark_price"]
            greeks = fetch_greeks(instr)
            for g in GREEK_FIELDS:
                row[f"{side}_{offset:+d}_{g}"] = greeks[g]
    return row


# ---------- parquet append ----------

def append_to_daily_parquet(row: dict, out_path: Path, label: str) -> tuple[int, int]:
    df_new = pd.DataFrame([row])
    df_new["ts"] = pd.to_datetime(df_new["ts"], utc=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if out_path.exists():
        df_existing = pd.read_parquet(out_path)
        rows_before = len(df_existing)
        df_existing["ts"] = pd.to_datetime(df_existing["ts"], utc=True)
        if (df_existing["ts"] == df_new["ts"].iloc[0]).any():
            print(f"  {label}: bucket {row['ts_iso']} already present; skip")
            return rows_before, rows_before
        last_ts = df_existing["ts"].max()
        gap_min = (df_new["ts"].iloc[0] - last_ts).total_seconds() / 60.0
        df_new["prev_gap_minutes"] = gap_min
        df_new["is_clean"] = abs(gap_min - 5.0) < 1.0
        all_cols = sorted(set(df_existing.columns) | set(df_new.columns))
        df_existing = df_existing.reindex(columns=all_cols)
        df_new = df_new.reindex(columns=all_cols)
        combined = pd.concat([df_existing, df_new], ignore_index=True)
    else:
        rows_before = 0
        df_new["prev_gap_minutes"] = pd.NA
        df_new["is_clean"] = True
        combined = df_new

    tmp = out_path.with_suffix(".parquet.tmp")
    combined.to_parquet(tmp, index=False, compression="zstd")
    os.replace(tmp, out_path)
    return rows_before, len(combined)


# ---------- main collector per currency ----------

def collect_one_currency(ccy: str, strikes_root: Path, out_root: Path) -> None:
    now = datetime.now(timezone.utc)
    pick = latest_strike_pick(strikes_root, ccy, now)
    if pick is None:
        print(f"[{ccy}] no strike file available; skipping")
        return

    bucket = round_to_5min(now)
    stale_days = pick.get("_stale_pick_days", 0)
    spot = get_index_price(CCY_INDEX[ccy])

    perp_t = get_ticker(PERP[ccy])
    perp_price = float(perp_t.get("mark_price") or 0.0)
    perp_oi = float(perp_t.get("open_interest") or 0.0)
    perp_funding = float(
        perp_t.get("funding_8h")
        or perp_t.get("current_funding")
        or 0.0
    )

    chain = fetch_chain(ccy)
    date_str = bucket.date().isoformat()

    for kind in ("weekly", "monthly"):
        block = pick.get(kind)
        if not block:
            print(f"[{ccy}] {kind}: no expiry available; skipping")
            continue
        try:
            row = build_row(
                ccy, block, kind, chain, spot,
                perp_price, perp_oi, perp_funding,
                bucket, stale_days,
            )
        except Exception as e:
            print(f"[{ccy}] {kind}: build_row failed: {e}")
            continue
        out_path = out_root / ccy / kind / f"{date_str}.parquet"
        before, after = append_to_daily_parquet(row, out_path, f"[{ccy}] {kind}")
        print(f"[{ccy}] {kind} {row['ts_iso']}  spot=${row['spot']:,.2f}  "
              f"perp=${row['perp']:,.2f}  perp_oi={row['perp_oi']:,.0f}  "
              f"atm=${row['atm_strike']:,}  strikes={len(block['strikes'])}  "
              f"rows {before}->{after}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--currencies", default="BTC,ETH")
    ap.add_argument("--strikes-dir", default="data/strikes")
    ap.add_argument("--out-dir", default="data/oi")
    args = ap.parse_args()

    strikes_root = Path(args.strikes_dir)
    out_root = Path(args.out_dir)

    for ccy in args.currencies.split(","):
        ccy = ccy.strip().upper()
        try:
            collect_one_currency(ccy, strikes_root, out_root)
        except Exception as e:
            print(f"[{ccy}] FATAL: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
