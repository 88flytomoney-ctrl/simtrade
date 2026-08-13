# 📈 SimTrade

Simulated stock trading system — record buy/sell trades, track portfolio PnL across HK + US markets, auto-update closing prices daily.

**Live site:** <https://88flytomoney-ctrl.github.io/simtrade/>

## Features

- **Trade recording** — Buy/Sell form; market (HK/US), symbol, volume, executed price, notes
- **Auto timestamp** — ISO timestamp generated on form submit
- **Portfolio dashboard** — total invested capital, current market value, realized/unrealized PnL, with HKD/USD split
- **Holdings table** — per-stock volume, avg cost, current price, PnL breakdown
- **Trade history** — searchable/filterable transaction table
- **Daily price update** — GitHub Actions runs `update_pnl.py` every weekday after market close (22:00 HKT / 14:00 UTC)
- **Pure static deploy** — single `index.html` + two JSON files (`data/trades.json`, `data/pnl_summary.json`)

## Repo layout

```
simtrade/
├── index.html                      # SPA (form + dashboard + history)
├── update_pnl.py                   # Daily PnL updater (fetches prices → pnl_summary.json)
├── data/
│   ├── trades.json                 # All buy/sell transactions
│   └── pnl_summary.json            # Latest computed PnL snapshot
└── .github/workflows/
    └── daily_pnl.yml               # Cron: weekday 14:00 UTC
```

## How trades get added

The web form uses **browser localStorage** — your trades survive reloads/closes. To publish to the deployed repo:

1. Click **⬇ Export trades.json** in the UI to download the file.
2. Replace `data/trades.json` in this repo with the downloaded file.
3. Commit + push to `main`. GitHub Actions will refresh `pnl_summary.json` automatically.

You can also **⬆ Import trades.json** to restore a previous export (useful when swapping machines).

## `update_pnl.py`

| Flag | Description |
|---|---|
| `python3 update_pnl.py` | Fetch latest prices, recompute PnL, write `data/pnl_summary.json` |
| `python3 update_pnl.py --dry-run` | Compute + print summary to stdout; do not write files |

**Price sources (reused from sibling projects):**

- **HK stocks** → Sina Finance real-time API (matches `fetch_sina_realtime()` in [`hkstock2`](https://github.com/88flytomoney-ctrl/hkstock2)); Yahoo Finance fallback (`<code>.HK`)
- **US stocks** → yfinance `Ticker.history(period="15d")` (matches `fetch_us_prices()` in [`usstock2`](https://github.com/88flytomoney-ctrl/usstock2))

PnL uses **FIFO accounting** to decompose realized vs. unrealized gains:

- realized = Σ (sell price − avg cost at time of sale) × sell volume
- unrealized = (current price − avg cost) × held volume

## GitHub Actions

`.github/workflows/daily_pnl.yml` runs `update_pnl.py` every Monday–Friday at 14:00 UTC (22:00 HKT) — after both HK and US markets close. It commits refreshed `pnl_summary.json` and redeploys the Pages site.

## Disclaimer

Simulated trades for tracking/learning only. **Not investment advice.**
