"""
Sanity-check the accumulated OI dataset.

Reads everything under data/oi/ and prints, for each (currency, expiry-kind):
  - days covered
  - total snapshots
  - expected snapshots if 5-min cadence had been perfect
  - clean snapshot ratio (is_clean)
  - longest gap in minutes
  - latest snapshot timestamp + spot/perp/atm

Then for the most recent day, shows a small "deltas" table:
  - top 5 strikes by absolute OI change in the last 6 snapshots (~30min)
  - splits into calls / puts
  - flags any -10%-or-greater drop on a strike with OI > 100 contracts
    (the kind of "wall starts unwinding" signal your strategy looks for)

Usage:
  python inspect.py                      # default: data/oi
  python inspect.py --root data/oi       # explicit
  python inspect.py --currencies BTC     # only BTC
  python inspect.py --kind monthly       # only monthly chain

Read-only. Safe to run while collector is active.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def list_parquets(root: Path, ccy: str, kind: str) -> list[Path]:
    folder = root / ccy / kind
    if not folder.exists():
        return []
    return sorted(folder.glob("*.parquet"))


def load_concat(paths: list[Path]) -> pd.DataFrame:
    if not paths:
        return pd.DataFrame()
    frames = []
    for p in paths:
        try:
            df = pd.read_parquet(p)
            frames.append(df)
        except Exception as e:
            print(f"  (failed reading {p.name}: {e})")
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.sort_values("ts").reset_index(drop=True)


def summarize_coverage(df: pd.DataFrame, label: str) -> None:
    n = len(df)
    if n == 0:
        print(f"  {label}: no data")
        return
    days = sorted(df["ts"].dt.date.unique())
    span_min = (df["ts"].max() - df["ts"].min()).total_seconds() / 60.0
    expected = int(span_min / 5) + 1 if span_min > 0 else 1
    pct = n / expected * 100 if expected else 0
    clean_pct = float(df.get("is_clean", pd.Series([True]*n)).fillna(True).mean()) * 100
    gaps = df["ts"].diff().dt.total_seconds().div(60).dropna()
    max_gap = float(gaps.max()) if len(gaps) else 0.0
    last = df.iloc[-1]
    print(f"  {label}:")
    print(f"     days       : {len(days)}  ({days[0]} -> {days[-1]})")
    print(f"     snapshots  : {n}  /  expected {expected}  ({pct:.0f}% coverage)")
    print(f"     clean ratio: {clean_pct:.1f}%   max gap: {max_gap:.1f} min")
    print(f"     latest     : {last['ts'].isoformat()}  "
          f"spot=${last['spot']:,.2f}  perp=${last['perp']:,.2f}  "
          f"atm=${last['atm_strike']:,}  expiry={last['expiry']}")


def oi_columns(df: pd.DataFrame, side: str) -> list[str]:
    return sorted(
        [c for c in df.columns if c.startswith(f"{side}_") and c.endswith("_oi")],
        key=lambda c: int(c.split("_")[1]),
    )


def show_recent_deltas(df: pd.DataFrame, label: str, lookback_snaps: int = 6) -> None:
    if len(df) < 2:
        return
    today = df["ts"].dt.date.iloc[-1]
    today_df = df[df["ts"].dt.date == today].reset_index(drop=True)
    if len(today_df) < 2:
        print(f"  {label}: only one snapshot today; can't compute deltas yet.")
        return
    n = min(lookback_snaps + 1, len(today_df))
    head = today_df.iloc[-n]
    tail = today_df.iloc[-1]
    head_ts, tail_ts = head["ts"], tail["ts"]
    span_min = (tail_ts - head_ts).total_seconds() / 60

    rows = []
    spacing = int(tail.get("strike_spacing", 1) or 1)
    atm = int(tail.get("atm_strike", 0) or 0)
    for side in ("C", "P"):
        for c in oi_columns(today_df, side):
            o0 = float(head.get(c) or 0.0)
            o1 = float(tail.get(c) or 0.0)
            if o1 < 1.0 and o0 < 1.0:
                continue
            d_abs = o1 - o0
            d_pct = (d_abs / o0 * 100) if o0 > 0 else float("inf")
            offset = int(c.split("_")[1])
            strike = atm + offset * spacing
            rows.append({
                "side": side,
                "offset": offset,
                "strike": strike,
                "oi_then": o0,
                "oi_now": o1,
                "delta": d_abs,
                "pct": d_pct,
            })
    if not rows:
        return
    out = pd.DataFrame(rows)
    print(f"  {label}: deltas over last {span_min:.0f} min  ({head_ts.strftime('%H:%M')} -> {tail_ts.strftime('%H:%M')})")

    # Top 5 by absolute delta
    top_abs = out.reindex(out["delta"].abs().sort_values(ascending=False).index).head(5)
    print(f"     Top 5 by |delta|:")
    for _, r in top_abs.iterrows():
        sign = "+" if r["delta"] >= 0 else ""
        pct = "n/a" if r["pct"] in (float("inf"), float("-inf")) else f"{r['pct']:+.1f}%"
        print(f"       {r['side']} {int(r['strike']):>7} (off {int(r['offset']):+d}): "
              f"{r['oi_then']:>9,.1f} -> {r['oi_now']:>9,.1f}  "
              f"({sign}{r['delta']:>9,.1f}, {pct})")

    # Unwind candidates
    flagged = out[(out["delta"] < 0) & (out["pct"] <= -10) & (out["oi_then"] >= 100)]
    if len(flagged):
        print(f"     UNWIND CANDIDATES (>=10% drop on OI>=100):")
        for _, r in flagged.iterrows():
            print(f"       {r['side']} {int(r['strike']):>7}: "
                  f"{r['oi_then']:>9,.1f} -> {r['oi_now']:>9,.1f} "
                  f"({r['pct']:+.1f}%)")


def show_perp_summary(df: pd.DataFrame, label: str) -> None:
    if len(df) < 2:
        return
    today = df["ts"].dt.date.iloc[-1]
    today_df = df[df["ts"].dt.date == today]
    if len(today_df) < 2:
        return
    perp = today_df["perp"]
    perp_oi = today_df["perp_oi"]
    funding = today_df["perp_funding"]
    print(f"  {label} perp today:")
    print(f"     price : ${perp.iloc[0]:,.2f} -> ${perp.iloc[-1]:,.2f}  "
          f"({(perp.iloc[-1]/perp.iloc[0]-1)*100:+.2f}%)  "
          f"range ${perp.min():,.0f}-${perp.max():,.0f}")
    print(f"     OI    : {perp_oi.iloc[0]:,.0f} -> {perp_oi.iloc[-1]:,.0f}  "
          f"({(perp_oi.iloc[-1]-perp_oi.iloc[0])/max(1,perp_oi.iloc[0])*100:+.2f}%)")
    print(f"     funding (latest): {funding.iloc[-1]*100:+.4f}%  "
          f"(annualized ~{funding.iloc[-1]*3*365*100:+.1f}%)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/oi", help="OI parquet root")
    ap.add_argument("--currencies", default="BTC,ETH")
    ap.add_argument("--kind", default="both", choices=["both", "weekly", "monthly"])
    ap.add_argument("--lookback", type=int, default=6,
                    help="Snapshots to look back for delta table")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"No data root at {root}. Has the collector run yet?")
        return

    kinds = ["weekly", "monthly"] if args.kind == "both" else [args.kind]

    print("=" * 70)
    print(f"  OI COLLECTOR :: dataset health  (root={root})")
    print("=" * 70)

    for ccy in [c.strip().upper() for c in args.currencies.split(",")]:
        print(f"\n[{ccy}]")
        for kind in kinds:
            paths = list_parquets(root, ccy, kind)
            df = load_concat(paths)
            label = f"{kind:>7}"
            summarize_coverage(df, label)

    print("\n" + "=" * 70)
    print("  RECENT DELTAS  (latest day only)")
    print("=" * 70)
    for ccy in [c.strip().upper() for c in args.currencies.split(",")]:
        print(f"\n[{ccy}]")
        for kind in kinds:
            paths = list_parquets(root, ccy, kind)
            df = load_concat(paths)
            if df.empty:
                continue
            show_perp_summary(df, kind)
            show_recent_deltas(df, kind, lookback_snaps=args.lookback)


if __name__ == "__main__":
    main()
