"""
Daily strike picker. Runs once a day at 03:30 UTC.

For each currency we pick TWO expiries:
  - "weekly": front-weekly (next Friday). Variable strike count — capped at
              whatever Deribit lists symmetrically around ATM.
  - "monthly": front-monthly (last Friday of nearest month). Always tries
               to fill 41 strikes (20 below + ATM + 20 above).

Output: data/strikes/<CCY>/<UTC-date>.json with both picks.

Schema:
{
  "currency": "BTC",
  "picked_at_utc": "2026-05-02T03:30:00+00:00",
  "spot_index": 78250.5,
  "weekly": {
    "expiry": "8MAY26",
    "expiry_ts_ms": ...,
    "atm_strike": 78000,
    "strike_spacing": 1000,
    "n_strikes_each_side": 10,    # ACTUAL — variable
    "strikes": [...],
    "instruments": [{strike, offset, C, P}, ...]
  },
  "monthly": { ...same shape, target n=20 each side... }
}
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from deribit import get_index_price, get_instruments


CCY_INDEX = {"BTC": "btc_usd", "ETH": "eth_usd"}


def detect_strike_spacing(strikes: list[int]) -> int:
    if len(strikes) < 3:
        return 1
    s = sorted(set(strikes))
    diffs = [s[i + 1] - s[i] for i in range(len(s) - 1)]
    counts: dict[int, int] = {}
    for d in diffs:
        counts[d] = counts.get(d, 0) + 1
    return max(counts.items(), key=lambda x: x[1])[0]


def is_last_friday_of_month(dt: datetime) -> bool:
    """True if dt is a Friday and there is no later Friday in the same month."""
    if dt.weekday() != 4:
        return False
    next_fri = dt + timedelta(days=7)
    return next_fri.month != dt.month


def pick_front_weekly(now_utc: datetime, instruments: list[dict]) -> tuple[str, int] | None:
    """
    Front weekly expiry = next Friday's expiry that is NOT also a monthly.
    Returns (label, ts_ms) or None.
    """
    seen: dict[int, str] = {}
    for inst in instruments:
        exp_ms = int(inst["expiration_timestamp"])
        if exp_ms not in seen:
            seen[exp_ms] = inst["instrument_name"].split("-")[1]
    sorted_exps = sorted(seen.items())
    now_ms = int(now_utc.timestamp() * 1000)

    for ms, lab in sorted_exps:
        if ms <= now_ms:
            continue
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        if dt.weekday() == 4 and not is_last_friday_of_month(dt):
            return lab, ms
    return None


def pick_front_monthly(now_utc: datetime, instruments: list[dict]) -> tuple[str, int] | None:
    """Front monthly = last Friday of the nearest month with a future expiry."""
    seen: dict[int, str] = {}
    for inst in instruments:
        exp_ms = int(inst["expiration_timestamp"])
        if exp_ms not in seen:
            seen[exp_ms] = inst["instrument_name"].split("-")[1]
    sorted_exps = sorted(seen.items())
    now_ms = int(now_utc.timestamp() * 1000)

    for ms, lab in sorted_exps:
        if ms <= now_ms:
            continue
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        if is_last_friday_of_month(dt):
            return lab, ms
    return None


def build_pick_for_expiry(
    instruments: list[dict],
    expiry_ms: int,
    expiry_label: str,
    spot: float,
    target_each_side: int,
    require_full: bool,
) -> dict:
    """
    Pick strikes around ATM for a given expiry.
      - target_each_side: desired strikes on each side
      - require_full: if True, REDUCE the number to whatever fits symmetrically
                      around ATM in the chain. If False (weekly), still take a
                      symmetric window using whatever we can.

    Both modes always center on the ATM strike. The difference is whether we
    cap the symmetric window or refuse to under-fill.
    """
    chain = [i for i in instruments
             if int(i["expiration_timestamp"]) == expiry_ms]
    strikes = sorted({int(i["strike"]) for i in chain})
    if not strikes:
        raise RuntimeError(f"No strikes for expiry {expiry_label}")

    spacing = detect_strike_spacing(strikes)
    atm = min(strikes, key=lambda k: abs(k - spot))
    atm_idx = strikes.index(atm)

    # Symmetric window: take min(target, distance to either edge) on each side
    n_below = min(target_each_side, atm_idx)
    n_above = min(target_each_side, len(strikes) - 1 - atm_idx)
    # For monthly we want the FULL count if available. If chain is too sparse
    # we still take what's available (no shifting — keep symmetric).
    n_each = min(n_below, n_above)
    if require_full:
        # If the symmetric window is undersized, log it but still proceed
        if n_each < target_each_side:
            note = (f"WARN: monthly chain only has {n_each} strikes per side "
                    f"around ATM (wanted {target_each_side})")
        else:
            note = ""
    else:
        # Weekly: explicitly variable, take symmetric window
        note = (f"weekly chain: {n_each} strikes each side"
                if n_each < target_each_side else "")

    chosen = strikes[atm_idx - n_each: atm_idx + n_each + 1]
    new_atm_idx = chosen.index(atm)

    by_key = {}
    for inst in chain:
        by_key[(int(inst["strike"]),
                inst["option_type"][0].upper())] = inst["instrument_name"]

    instr_rows = []
    for idx, strike in enumerate(chosen):
        offset = idx - new_atm_idx
        instr_rows.append({
            "strike": strike,
            "offset": offset,
            "C": by_key.get((strike, "C")),
            "P": by_key.get((strike, "P")),
        })

    return {
        "expiry": expiry_label,
        "expiry_ts_ms": expiry_ms,
        "atm_strike": atm,
        "strike_spacing": spacing,
        "n_strikes_each_side": n_each,
        "strikes": chosen,
        "note": note,
        "instruments": instr_rows,
    }


def pick_for_currency(ccy: str, now_utc: datetime, target_each_side: int = 20) -> dict:
    instruments = get_instruments(ccy, kind="option", expired=False)
    spot = get_index_price(CCY_INDEX[ccy])

    weekly = pick_front_weekly(now_utc, instruments)
    monthly = pick_front_monthly(now_utc, instruments)

    out: dict = {
        "currency": ccy,
        "picked_at_utc": now_utc.isoformat(timespec="seconds"),
        "spot_index": spot,
    }

    if weekly:
        wl, wms = weekly
        out["weekly"] = build_pick_for_expiry(
            instruments, wms, wl, spot,
            target_each_side=target_each_side,
            require_full=False,
        )
    else:
        out["weekly"] = None

    if monthly:
        ml, mms = monthly
        out["monthly"] = build_pick_for_expiry(
            instruments, mms, ml, spot,
            target_each_side=target_each_side,
            require_full=True,
        )
    else:
        out["monthly"] = None

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--currencies", default="BTC,ETH")
    ap.add_argument("--out-dir", default="data/strikes")
    ap.add_argument("--n", type=int, default=20)
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    date_str = now.date().isoformat()
    root = Path(args.out_dir)

    for ccy in args.currencies.split(","):
        ccy = ccy.strip().upper()
        try:
            picked = pick_for_currency(ccy, now, target_each_side=args.n)
        except Exception as e:
            print(f"[{ccy}] ERROR: {e}")
            continue
        out_dir = root / ccy
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{date_str}.json"
        out.write_text(json.dumps(picked, indent=2))

        spot = picked["spot_index"]
        w = picked.get("weekly")
        m = picked.get("monthly")
        wsum = (f"weekly={w['expiry']} atm=${w['atm_strike']:,} "
                f"n_each={w['n_strikes_each_side']} ({len(w['strikes'])} total)"
                if w else "weekly=NONE")
        msum = (f"monthly={m['expiry']} atm=${m['atm_strike']:,} "
                f"n_each={m['n_strikes_each_side']} ({len(m['strikes'])} total)"
                if m else "monthly=NONE")
        print(f"[{ccy}] spot=${spot:,.2f}  {wsum}  {msum}")
        if w and w.get("note"):
            print(f"     weekly note: {w['note']}")
        if m and m.get("note"):
            print(f"     monthly note: {m['note']}")


if __name__ == "__main__":
    main()
