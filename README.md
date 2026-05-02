# BTC/ETH Options OI Collector

Free, public-data collector that polls **Deribit** every 5 minutes and stores per-strike option open interest, IV, volume, mark price, and greeks for **BTC and ETH** on both the **front weekly** and **front monthly** expiries. Runs on **GitHub Actions**, scheduled by **cron-job.org** (free), with results committed back to the repo as Parquet files.

After ~7-14 days of polling you have enough history to backtest options-OI strategies (walls, unwinds, put/call OI ratios, gamma exposure, IV skew) on the venue that actually matters.

## Architecture

```
cron-job.org (every 5 min)
        │
        │  POST /repos/Ab-verse/collector/actions/workflows/collect.yml/dispatches
        ▼
GitHub Actions (collect-oi runner)
        │
        │  reads data/strikes/<CCY>/<today>.json
        │  fetches Deribit option chain + perp ticker
        │  writes/appends data/oi/<CCY>/{weekly,monthly}/<today>.parquet
        │  commits + pushes back to main
        ▼
Repo (this you)

GitHub Actions (pick-strikes runner, daily 03:30 UTC)
        │
        │  picks ATM strike + 41 contracts (monthly) and ~15-25 (weekly)
        │  writes data/strikes/<CCY>/<today>.json
        ▼
Same repo
```

**Why two scheduling sources?**
- `pick-strikes` runs once a day → GitHub-native cron is fine (low risk of being dropped).
- `collect-oi` runs every 5 min → GitHub free-tier cron silently dropped ~85% of these firings, so an external trigger (cron-job.org) calls the workflow-dispatch API directly.

## What gets collected

1. **Daily strike picker** (`pick-strikes`) — runs once at 03:30 UTC. Locks in the strike list for the day:
   - **Front weekly expiry** (next Friday that isn't end-of-month) — variable count, takes whatever is symmetrically available around ATM (typically 13-25 strikes).
   - **Front monthly expiry** (last Friday of nearest month) — targets 20 strikes each side of ATM (41 total). Reduces if the chain doesn't have that many.

2. **5-minute snapshot** (`collect-oi`) — triggered every 5 minutes. For each currency × each expiry kind, writes one wide row containing:
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
| `C_<offset>_mark` | float | mark price (Deribit theoretical fair value) |
| `C_<offset>_delta` | float | option delta |
| `C_<offset>_gamma` | float | |
| `C_<offset>_theta` | float | |
| `C_<offset>_vega` | float | |
| `P_<offset>_*` | same | mirror for puts |

**Offsets are relative to ATM at the daily pick.** `+0` = ATM strike, `-1` = one strike below, `+1` = one strike above. Strike spacing is logged in `strike_spacing` so you can convert offsets to dollar strikes:
`absolute_strike = atm_strike + offset * strike_spacing`

## Setup (one-time)

### 1. Push this repo to a new public GitHub repo
Public is required for free GitHub Actions minutes (and the data is public market data anyway).

### 2. GitHub repo settings
**Settings → Actions → General → Workflow permissions** → select **"Read and write permissions"** → Save.

### 3. Enable workflows
Open the **Actions** tab. If prompted *"I understand my workflows, go ahead and enable them"*, click it.

### 4. Seed today's strike list
**Actions → pick-strikes → Run workflow** (manually, once). After it completes, you'll see commits adding `data/strikes/BTC/<today>.json` and `data/strikes/ETH/<today>.json`. From tomorrow onward, this fires automatically at 03:30 UTC.

### 5. Set up the external 5-min trigger (cron-job.org)

Required because GitHub free-tier cron drops 5-min schedules under load.

a. **Create a fine-scoped GitHub PAT**:
   - https://github.com/settings/personal-access-tokens → "Generate new token" (Fine-grained)
   - Repository access: select your `collector` repo only
   - Permissions: **Actions = Read and write** (Metadata is auto-added as Read-only)
   - Expiration: 90 days (rotate before expiry)
   - Copy the token immediately — shown only once

b. **Sign up at cron-job.org**: https://console.cron-job.org/signup

c. **Create a cronjob**:
   - **URL**: `https://api.github.com/repos/<USER>/<REPO>/actions/workflows/collect.yml/dispatches`
   - **Method**: `POST`
   - **Schedule**: every 5 minutes
   - **Time zone**: UTC
   - **Timeout**: 30 seconds
   - **Treat redirects as success**: OFF
   - **Body**: `{"ref": "main"}`
   - **Headers**:
     - `Authorization: Bearer <YOUR_PAT>`
     - `Accept: application/vnd.github+json`
     - `X-GitHub-Api-Version: 2022-11-28`
   - **Notifications**: on failure ON, on disable ON, on success OFF
   - Test with "Execute now" — should return HTTP 204. Then save.

### 6. Verify

After ~30 minutes of cron-job.org firing:

```powershell
git pull
python check.py
```

Expected: ~6 new snapshots in 30 min, `max gap` ~5-7 min, `clean ratio` >80%.

## Local testing

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Pick today's strikes (writes data/strikes/<CCY>/<today>.json):
python pick_strikes.py

# Take one 5m snapshot (appends to data/oi/<CCY>/{weekly,monthly}/<today>.parquet):
python collect.py

# Sanity-check accumulated data:
python check.py
```

## Files

| File | Purpose |
|---|---|
| `pick_strikes.py` | Daily strike list selector (weekly + monthly) |
| `collect.py` | 5-min snapshot writer; appends one row to today's parquet |
| `check.py` | Read-only health summary + recent OI deltas |
| `deribit.py` | Thin Deribit public-API client (no auth) |
| `.github/workflows/pick.yml` | GitHub-native cron at 03:30 UTC daily |
| `.github/workflows/collect.yml` | `workflow_dispatch` only — triggered externally |

## Caveats

- **Strike chain is sparse on weeklies.** Don't expect 41 strikes on the weekly — Deribit lists 15-25 around ATM. The collector takes whatever's symmetric.
- **Greeks come from a separate ticker call per instrument**, so each snapshot makes ~75 HTTP calls (Deribit handles this fine; no auth, no rate limit on public). Each snapshot takes 30-60 seconds.
- **cron-job.org free-tier reliability**: ~5-10 min jitter is typical. The `prev_gap_minutes` and `is_clean` columns let backtests drop unreliable transitions.
- **PAT expiry**: GitHub fine-scoped tokens expire (default 90 days). When yours expires, cron-job.org will start failing with HTTP 401 and email you. Generate a new token, replace the header value.
- **Public repo** = committed parquet data is publicly visible. Fine for market data; never commit API keys or `.env`.
- **First useful backtest** is ~7 days of accumulated data. Strategies looking at "OI in the last 15min" need at least a few hundred snapshots before features stabilize.

## What this enables (later)

Once you have a couple weeks of data, you can backtest:

- **Walls**: strikes with persistently high OI act as price magnets / barriers
- **Unwinds**: a key strike's OI dropping over a few candles often precedes price moving past it
- **Put/Call OI ratio shifts** at specific strike clusters
- **Gamma exposure (GEX)** as dealer-flow proxy
- **IV skew regime** (25-delta call IV − 25-delta put IV) as sentiment

The same parquet shape is consumed directly by pandas; backtest code is straightforward once data is there.
