# BTC/ETH Options OI Collector

Free, public-data collector that polls **Deribit** every 5 minutes and stores per-strike option open interest, IV, volume, and greeks for **BTC and ETH** on both the **front weekly** and **front monthly** expiries. Designed to run for free on **GitHub Actions** with results committed back to the repo as Parquet files.

After ~7-14 days of polling you have enough history to backtest options-OI strategies (walls, unwinds, put/call OI ratios, gamma exposure, IV skew) on the venue that actually matters.

## What gets collected

Two scheduled jobs:

1. **Daily strike picker** — runs once at 03:30 UTC. Locks in the strike list for the day:
   - **Front weekly expiry** (next Friday that isn't end-of-month) — variable count, takes whatever is symmetrically available around ATM (typically 13–25 strikes).
   - **Front monthly expiry** (last Friday of nearest month) — targets 20 strikes each side of ATM (41 total). Reduces if the chain doesn't have that many.

2. **5-minute snapshot** — runs every 5 minutes. For each currency × each expiry kind, writes one wide row containing:
   - Spot index, perp price, perp OI, perp funding rate
   - Per strike: OI, IV, volume, mark price, delta, gamma, theta, vega — for both calls and puts
   - Metadata: `atm_strike`, `strike_spacing`, `expiry`, `prev_gap_minutes`, `is_clean`

## File layout

```
data/
  strikes/
    BTC/2026-05-02.json     # daily strike pick (weekly + monthly)
    ETH/2026-05-02.json
  oi/
    BTC/
      weekly/2026-05-02.parquet      # all 5m snapshots that day, weekly chain
      monthly/2026-05-02.parquet     # all 5m snapshots that day, monthly chain
    ETH/
      weekly/2026-05-02.parquet
      monthly/2026-05-02.parquet
```

## Schema (wide parquet)

Columns per row:

| Column | Type | Notes |
|---|---|---|
| `ts` | datetime UTC | 5-min bucket (floored) |
| `ts_iso` | str | ISO timestamp |
| `ccy` | str | BTC / ETH |
| `expiry_kind` | str | weekly / monthly |
| `expiry` | str | e.g. "8MAY26" |
| `spot` | float | Deribit index price |
| `perp` | float | Deribit perp mark |
| `perp_oi` | float | Deribit perp open interest |
| `perp_funding` | float | 8h funding rate |
| `atm_strike` | int | ATM strike at the morning pick |
| `strike_spacing` | int | $1000 BTC, $50/$100 ETH |
| `stale_pick_days` | int | 0 = today's pick; >0 = picker missed today, fallback used |
| `prev_gap_minutes` | float | gap since previous snapshot |
| `is_clean` | bool | true if gap was within ~5min of expected |
| `C_<offset>_oi` | float | call OI at offset N from ATM (e.g. `C_-5_oi`, `C_+0_oi`, `C_+10_oi`) |
| `C_<offset>_iv` | float | call IV (decimal, e.g. 0.55 = 55%) |
| `C_<offset>_volume` | float | 24h volume |
| `C_<offset>_mark` | float | mark price |
| `C_<offset>_delta` | float | option delta |
| `C_<offset>_gamma` | float | |
| `C_<offset>_theta` | float | |
| `C_<offset>_vega` | float | |
| `P_<offset>_*` | same | mirror for puts |

**Offsets are relative to ATM at the daily pick.** `+0` = ATM strike, `-1` = one strike below, `+1` = one strike above. Strike spacing is logged in `strike_spacing` so you can convert offsets to dollar strikes:
`absolute_strike = atm_strike + offset * strike_spacing`

## Setup (one-time)

1. **Push this repo to a new public GitHub repo.**
2. **Settings → Actions → General → Workflow permissions → "Read and write permissions"** (so the workflows can commit data back).
3. **Actions tab** — enable workflows if prompted. The two workflows (`pick-strikes`, `collect-oi`) will start firing on their crons.
4. **Manually trigger `pick-strikes` once** from the Actions tab to seed today's strike list, otherwise the first collect run has no strike file to use.
5. **Verify**: after the first hour you should see `data/oi/BTC/monthly/<today>.parquet` with ~12 rows. Click into it to confirm.

## Local testing

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Pick today's strikes:
python pick_strikes.py

# Take one 5m snapshot:
python collect.py

# Sanity-check accumulated data:
python check.py
```

## Caveats

- **GitHub free-tier cron has 5-15min jitter** under load. Some 5m buckets will be missed; `prev_gap_minutes` records the actual gap so backtests can drop unreliable transitions.
- **Public repo** = your committed parquet data is publicly visible. This is fine for market data but don't ever commit API keys.
- **Strike chain is sparse on weeklies.** Don't expect 41 strikes on the weekly — Deribit lists 15-25 around ATM. The collector takes whatever's symmetric.
- **Greeks come from a separate ticker call per instrument**, so each snapshot makes ~75 HTTP calls (Deribit handles this fine; no auth, no rate limit on public). Each snapshot takes 30-60 seconds.
- **First useful backtest** is ~7 days of accumulated data. Strategies looking at "OI in the last 15min" need at least a few hundred snapshots before features stabilize.

## What this enables (later)

Once you have a couple weeks of data, you can backtest:

- **Walls**: strikes with persistently high OI act as price magnets / barriers
- **Unwinds**: a key strike's OI dropping over a few candles often precedes price moving past it
- **Put/Call OI ratio shifts** at specific strike clusters
- **Gamma exposure (GEX)** as dealer-flow proxy
- **IV skew regime** (25-delta call IV − 25-delta put IV) as sentiment

The same parquet shape is consumed directly by pandas; backtest code is straightforward once data is there.
