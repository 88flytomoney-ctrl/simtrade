#!/usr/bin/env python3
"""
update_pnl.py — Daily PnL updater for the simtrade simulated trading system.

Reads   data/trades.json
Writes  data/pnl_summary.json

Price logic reused from sibling projects:
  • HK stocks → Sina Finance real-time API  (same approach as hkstock2/generate_predictions.py)
  • US stocks → yfinance .history()          (same approach as usstock2/generate_predictions.py)

Usage:
  python3 update_pnl.py             # fetch prices, recompute PnL, write summary
  python3 update_pnl.py --dry-run  # compute and print only, do not write files

Calculations per stock (per currency):
  • holding_volume   = Σ BUY volume − Σ SELL volume
  • avg_cost          = weighted-average cost of remaining BUY lots after SELL reduction
                        (cost basis tracks remaining shares only)
  • current_price     = latest close from the relevant price source
  • current_value      = holding_volume × current_price
  • unrealized_pnl     = current_value − (holding_volume × avg_cost)
  • realized_pnl       = for each SELL: (sell_price − avg_cost_at_time_of_sale) × sell_volume
                        (running FIFO-style realized PnL; avg cost is the running buy avg)

Totals are split by currency: HK stocks priced in HKD, US stocks priced in USD.
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Match the tz convention used in hkstock2/usstock2
HK_TZ = timezone(timedelta(hours=8))

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
TRADES_FILE = DATA_DIR / "trades.json"
SUMMARY_FILE = DATA_DIR / "pnl_summary.json"


# ─────────────────────────────────────────────────────────────────────────────
# Price fetchers — ported from hkstock2 (Sina) + usstock2 (yfinance)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_hk_close(code):
    """Fetch the latest HK stock close from Sina Finance API.

    Mirrors `fetch_sina_realtime()` in hkstock2/generate_predictions.py:
    - HK code padded to 5 digits
    - GET https://hq.sinajs.cn/list=hk<5digit>
    - gbk-encoded body, CSV field [4] = latest price
    Returns (close, date_str) or (None, None) on failure.
    """
    import requests  # lazy import so --dry-run with no deps works for US-only

    code_5 = str(code).zfill(5)
    url = f"https://hq.sinajs.cn/list=hk{code_5}"
    try:
        resp = requests.get(url, headers={
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0",
        }, timeout=15)
        resp.encoding = "gbk"
        resp.raise_for_status()
    except Exception as e:
        print(f"  ⚠️ Sina fetch failed for HK {code}: {e}")
        return None, None

    m = re.search(rf'hq_str_hk{code_5}="([^"]+)"', resp.text)
    if not m:
        print(f"  ⚠️ No Sina data for HK {code}")
        return None, None
    fields = m.group(1).split(",")
    if len(fields) < 19:
        return None, None
    try:
        # field indices match hkstock2: [4]=current, [5]=low, [6]=high (swapped!), [17]=date
        today_current = float(fields[4])
        sina_low = float(fields[5])
        sina_high = float(fields[6])
        today_low = min(sina_low, sina_high)
        today_high = max(sina_low, sina_high)
        today_close = min(today_current, today_high)
        date_str = fields[17].replace("/", "-")
        if today_close == 0:
            # Market closed / no trade — try Yahoo fallback
            return _fetch_hk_close_yahoo(code), None
        return today_close, date_str
    except (ValueError, IndexError):
        return None, None


def _fetch_hk_close_yahoo(code):
    """Fallback: Yahoo Finance close for HK stock. Code → <last4>.HK like hkstock2."""
    try:
        import yfinance as yf
        yahoo_code = str(code)[-4:]
        sym = f"{yahoo_code}.HK"
        hist = yf.Ticker(sym).history(period="5d")
        if hist.empty:
            return None
        return round(float(hist["Close"].dropna().iloc[-1]), 2)
    except Exception:
        return None


def fetch_us_close(ticker):
    """Fetch the latest US stock close via yfinance.

    Mirrors `fetch_us_prices()` in usstock2/generate_predictions.py:
    - yf.Ticker(ticker).history(period="15d"), dropna, last row Close.
    Returns (close, date_str) or (None, None).
    """
    try:
        import yfinance as yf
        hist = yf.Ticker(str(ticker)).history(period="15d")
        if hist.empty:
            print(f"  ⚠️ Yahoo Finance returned no history for US {ticker}")
            return None, None
        hist = hist.dropna(subset=["Open", "High", "Low", "Close"])
        last = hist.iloc[-1]
        close = round(float(last["Close"]), 2)
        # hist index is tz-aware Timestamp; convert to date string
        date_str = last.name.strftime("%Y-%m-%d")
        return close, date_str
    except Exception as e:
        print(f"  ⚠️ yfinance fetch failed for US {ticker}: {e}")
        return None, None


# ─────────────────────────────────────────────────────────────────────────────
# PnL engine
# ─────────────────────────────────────────────────────────────────────────────

def compute_pnl(trades):
    """Given a list of trade dicts, compute per-symbol + per-currency PnL.

    Returns a dict with the same shape as pnl_summary.json.
    """
    # Group trades by (market, symbol) and sort by timestamp for FIFO accounting
    per_symbol = defaultdict(list)
    for t in trades:
        key = (t["market"], t["symbol"])
        per_symbol[key].append(t)
    for key in per_symbol:
        per_symbol[key].sort(key=lambda x: x["timestamp"])

    holdings = {}
    totals = {
        # HK in HKD
        "hk_total_invested": 0.0,
        "hk_current_value": 0.0,
        "hk_realized_pnl": 0.0,
        "hk_unrealized_pnl": 0.0,
        # US in USD
        "us_total_invested": 0.0,
        "us_current_value": 0.0,
        "us_realized_pnl": 0.0,
        "us_unrealized_pnl": 0.0,
    }

    # Determine which symbols need fresh prices
    symbols_to_price = ()
    price_cache = {}

    # First pass: discover symbols + fetch prices
    symbols_hk = sorted({t["symbol"] for t in trades if t["market"].upper() == "HK"})
    symbols_us = sorted({t["symbol"] for t in trades if t["market"].upper() == "US"})
    print(f"🔎 Pricing {len(symbols_hk)} HK symbol(s) and {len(symbols_us)} US symbol(s)...")

    for sym in symbols_hk:
        close, date_str = fetch_hk_close(sym)
        if close is not None:
            price_cache[("HK", sym)] = (float(close), date_str)
            print(f"  ✅ HK {sym}: {close}  ({date_str})")
        else:
            print(f"  ❌ HK {sym}: no price")

    for sym in symbols_us:
        close, date_str = fetch_us_close(sym)
        if close is not None:
            price_cache[("US", sym)] = (float(close), date_str)
            print(f"  ✅ US {sym}: {close}  ({date_str})")
        else:
            print(f"  ❌ US {sym}: no price")

    # Second pass: FIFO accounting per symbol
    for (market, symbol), sym_trades in per_symbol.items():
        currency = "HKD" if market.upper() == "HK" else "USD"
        # Running buy pool: list of (volume_remaining, price) lots
        buy_pool = []
        realized = 0.0
        total_bought_cost = 0.0  # cumulative cost of all BUY trades (for total invested)

        for t in sym_trades:
            ttype = t["type"].upper()
            vol = float(t["volume"])
            price = float(t["price"])
            if ttype == "BUY":
                buy_pool.append([vol, price])
                total_bought_cost += vol * price
            elif ttype == "SELL":
                # FIFO: drain from oldest buy lot
                sell_vol = vol
                avg_cost_at_sale = 0.0
                while sell_vol > 0 and buy_pool:
                    lot_vol, lot_price = buy_pool[0]
                    take = min(sell_vol, lot_vol)
                    avg_cost_at_sale += take * lot_price
                    lot_vol -= take
                    sell_vol -= take
                    if lot_vol <= 0:
                        buy_pool.pop(0)
                    else:
                        buy_pool[0][0] = lot_vol
                # realized = sell proceeds − cost basis of those shares
                realized += (vol - sell_vol) * price - avg_cost_at_sale
                # any unsold sell volume is treated as short-selling against nothing
                # (record as realized at sale price, zero cost basis)
                if sell_vol > 0:
                    realized += sell_vol * price

        # Current holdings = remaining buy-pool volume + weighted avg cost
        holding_volume = sum(lot[0] for lot in buy_pool)
        if holding_volume > 0:
            avg_cost = sum(lot[0] * lot[1] for lot in buy_pool) / holding_volume
        else:
            avg_cost = 0.0

        cur_price, price_date = price_cache.get((market, symbol), (None, None))
        if cur_price is not None and holding_volume > 0:
            current_value = holding_volume * cur_price
            cost_basis = holding_volume * avg_cost
            unrealized = current_value - cost_basis
        else:
            current_value = 0.0
            unrealized = 0.0

        holdings[f"{market}:{symbol}"] = {
            "market": market,
            "symbol": symbol,
            "currency": currency,
            "holding_volume": round(holding_volume, 6),
            "avg_cost": round(avg_cost, 4),
            "current_price": cur_price,
            "price_date": price_date,
            "current_value": round(current_value, 2),
            "total_bought_cost": round(total_bought_cost, 2),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "total_pnl": round(realized + unrealized, 2),
        }

        bucket = "hk" if market.upper() == "HK" else "us"
        # Note: "total_invested" = cumulative capital deployed into BUYs (gross)
        totals[f"{bucket}_total_invested"] += total_bought_cost
        totals[f"{bucket}_current_value"] += current_value
        totals[f"{bucket}_realized_pnl"] += realized
        totals[f"{bucket}_unrealized_pnl"] += unrealized

    # Round totals
    for k in totals:
        totals[k] = round(totals[k], 2)

    holdings = dict(sorted(holdings.items(), key=lambda x: x[0]))
    return {
        "last_updated": datetime.now(HK_TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "holdings": holdings,
        **totals,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def load_trades():
    if not TRADES_FILE.exists():
        print(f"⚠️ {TRADES_FILE} not found, starting with empty trade book.")
        return []
    with open(TRADES_FILE, "r", encoding="utf-8") as f:
        text = f.read().strip()
        if not text:
            return []
        return json.loads(text)


def main():
    parser = argparse.ArgumentParser(description="Daily PnL updater for simtrade")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute and print summary without writing files")
    args = parser.parse_args()

    print(f"🚀 update_pnl.py — {datetime.now(HK_TZ).strftime('%Y-%m-%d %H:%M')} HKT")
    trades = load_trades()
    print(f"  → Loaded {len(trades)} trade(s)")

    if not trades:
        summary = {
            "last_updated": datetime.now(HK_TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "holdings": {},
            "hk_total_invested": 0.0, "hk_current_value": 0.0,
            "hk_realized_pnl": 0.0, "hk_unrealized_pnl": 0.0,
            "us_total_invested": 0.0, "us_current_value": 0.0,
            "us_realized_pnl": 0.0, "us_unrealized_pnl": 0.0,
        }
    else:
        summary = compute_pnl(trades)

    print("\n── PnL Summary ──────────────────────────────────────────────")
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    if args.dry_run:
        print("\n🟡 --dry-run: NOT writing files")
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n✅ Written {SUMMARY_FILE}")


if __name__ == "__main__":
    main()
