#!/usr/bin/env python3
"""
Option Screener - CSP / CC / Vertical Spread scanner
======================================================
Scans a watchlist of tickers using free Yahoo Finance data (via yfinance)
and ranks candidates for:
  - Cash-Secured Puts (CSP)     -> wheel entry
  - Covered Calls (CC)          -> wheel exit
  - Vertical Spreads            -> bull put spread / bear call spread,
                                    selectable width (default $5)

Requirements:
    pip install yfinance pandas numpy scipy tabulate

Usage:
    python option_screener.py                      # uses WATCHLIST below
    python option_screener.py AAPL MSFT NVDA        # override watchlist via CLI
    python option_screener.py --dte-min 21 --dte-max 45 --width 5

Notes on data quality:
    Yahoo's free option chain data does NOT include greeks directly for
    every contract in a reliable way, so delta is estimated locally with
    Black-Scholes using the contract's implied volatility, the stock's
    last price, and time-to-expiration. This is standard practice for
    free-data screeners and is plenty accurate for ranking/filtering
    purposes (not for precise risk management).
"""

import sys
import argparse
import warnings
from datetime import datetime, timezone
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import norm

warnings.filterwarnings("ignore")

try:
    import yfinance as yf
except ImportError:
    print("Missing dependency. Run: pip install yfinance pandas numpy scipy tabulate")
    sys.exit(1)

try:
    from tabulate import tabulate
    HAS_TABULATE = True
except ImportError:
    HAS_TABULATE = False


# ----------------------------------------------------------------------------
# CONFIG - edit your watchlist here, or pass tickers on the command line
# ----------------------------------------------------------------------------
WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "SPY", "QQQ",
]

RISK_FREE_RATE = 0.045  # rough current T-bill rate, used for BS delta estimate


# ----------------------------------------------------------------------------
# Black-Scholes delta (since Yahoo's free chain doesn't reliably expose greeks)
# ----------------------------------------------------------------------------
def bs_delta(option_type, S, K, T, r, sigma):
    """Black-Scholes delta. option_type: 'call' or 'put'. T in years."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return np.nan
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    if option_type == "call":
        return norm.cdf(d1)
    else:
        return norm.cdf(d1) - 1


def annualized_yield_pct(premium, strike, dte_days):
    """Return premium as an annualized % of capital at risk (strike)."""
    if dte_days <= 0 or strike <= 0:
        return 0.0
    return (premium / strike) * (365.0 / dte_days) * 100.0


# ----------------------------------------------------------------------------
# Data fetch helpers
# ----------------------------------------------------------------------------
@dataclass
class TickerData:
    ticker: str
    price: float
    expirations: list = field(default_factory=list)


def get_ticker_data(ticker):
    tk = yf.Ticker(ticker)
    try:
        price = tk.fast_info.get("lastPrice") or tk.fast_info.get("last_price")
    except Exception:
        price = None
    if not price:
        hist = tk.history(period="1d")
        if hist.empty:
            return None
        price = hist["Close"].iloc[-1]
    expirations = tk.options
    return TickerData(ticker=ticker, price=float(price), expirations=list(expirations))


def get_next_earnings_days(ticker):
    """Return days until next earnings announcement, or None if unavailable."""
    try:
        tk = yf.Ticker(ticker)
        earnings_date = None

        # yfinance has changed this API across versions; try a few approaches.
        try:
            cal = tk.calendar
            if isinstance(cal, dict):
                dates = cal.get("Earnings Date")
                if dates:
                    earnings_date = dates[0]
            elif cal is not None and hasattr(cal, "loc"):
                try:
                    val = cal.loc["Earnings Date"]
                    earnings_date = val.iloc[0] if hasattr(val, "iloc") else val[0]
                except Exception:
                    earnings_date = None
        except Exception:
            earnings_date = None

        if earnings_date is None:
            try:
                ed_df = tk.get_earnings_dates(limit=8)
                if ed_df is not None and not ed_df.empty:
                    today = datetime.now(timezone.utc).date()
                    future = [d for d in ed_df.index if d.date() >= today]
                    if future:
                        earnings_date = min(future)
            except Exception:
                earnings_date = None

        if earnings_date is None:
            return None

        if isinstance(earnings_date, str):
            earnings_date = datetime.strptime(earnings_date, "%Y-%m-%d")
        if hasattr(earnings_date, "to_pydatetime"):
            earnings_date = earnings_date.to_pydatetime()
        if hasattr(earnings_date, "date"):
            earnings_date = earnings_date.date()

        today = datetime.now(timezone.utc).date()
        return (earnings_date - today).days
    except Exception:
        return None


def dte_from_expiration(exp_str):
    exp_date = datetime.strptime(exp_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (exp_date - now).days


def filter_expirations(expirations, dte_min, dte_max):
    out = []
    for exp in expirations:
        d = dte_from_expiration(exp)
        if dte_min <= d <= dte_max:
            out.append((exp, d))
    return out


def get_chain(ticker_obj, expiration):
    chain = ticker_obj.option_chain(expiration)
    return chain.calls, chain.puts


# ----------------------------------------------------------------------------
# Scanners
# ----------------------------------------------------------------------------
def scan_csp(tk_data, tk_obj, dte_min, dte_max, target_delta=-0.30, delta_tol=0.15):
    """Cash-secured puts: sell OTM puts below current price."""
    results = []
    for exp, dte in filter_expirations(tk_data.expirations, dte_min, dte_max):
        try:
            _, puts = get_chain(tk_obj, exp)
        except Exception:
            continue
        T = dte / 365.0
        for _, row in puts.iterrows():
            K = row["strike"]
            if K >= tk_data.price:  # want OTM puts only
                continue
            bid = row.get("bid", 0) or 0
            iv = row.get("impliedVolatility", np.nan)
            if bid <= 0 or pd.isna(iv) or iv <= 0:
                continue
            delta = bs_delta("put", tk_data.price, K, T, RISK_FREE_RATE, iv)
            if pd.isna(delta) or abs(delta - target_delta) > delta_tol:
                continue
            ayield = annualized_yield_pct(bid, K, dte)
            results.append({
                "ticker": tk_data.ticker, "strategy": "CSP", "expiration": exp,
                "dte": dte, "strike": K, "price": bid, "delta": round(delta, 3),
                "iv": round(iv * 100, 1), "ann_yield_%": round(ayield, 2),
                "underlying_px": round(tk_data.price, 2),
            })
    return results


def scan_cc(tk_data, tk_obj, dte_min, dte_max, target_delta=0.30, delta_tol=0.15):
    """Covered calls: sell OTM calls above current price."""
    results = []
    for exp, dte in filter_expirations(tk_data.expirations, dte_min, dte_max):
        try:
            calls, _ = get_chain(tk_obj, exp)
        except Exception:
            continue
        T = dte / 365.0
        for _, row in calls.iterrows():
            K = row["strike"]
            if K <= tk_data.price:  # want OTM calls only
                continue
            bid = row.get("bid", 0) or 0
            iv = row.get("impliedVolatility", np.nan)
            if bid <= 0 or pd.isna(iv) or iv <= 0:
                continue
            delta = bs_delta("call", tk_data.price, K, T, RISK_FREE_RATE, iv)
            if pd.isna(delta) or abs(delta - target_delta) > delta_tol:
                continue
            ayield = annualized_yield_pct(bid, K, dte)
            results.append({
                "ticker": tk_data.ticker, "strategy": "CC", "expiration": exp,
                "dte": dte, "strike": K, "price": bid, "delta": round(delta, 3),
                "iv": round(iv * 100, 1), "ann_yield_%": round(ayield, 2),
                "underlying_px": round(tk_data.price, 2),
            })
    return results


def scan_vertical_puts(tk_data, tk_obj, dte_min, dte_max, width=5.0,
                        target_delta=-0.30, delta_tol=0.15):
    """Bull put spread: sell OTM put, buy further OTM put `width` lower."""
    results = []
    for exp, dte in filter_expirations(tk_data.expirations, dte_min, dte_max):
        try:
            _, puts = get_chain(tk_obj, exp)
        except Exception:
            continue
        T = dte / 365.0
        puts = puts.sort_values("strike")
        strikes = puts["strike"].values
        for _, short_row in puts.iterrows():
            K_short = short_row["strike"]
            if K_short >= tk_data.price:
                continue
            short_bid = short_row.get("bid", 0) or 0
            short_iv = short_row.get("impliedVolatility", np.nan)
            if short_bid <= 0 or pd.isna(short_iv) or short_iv <= 0:
                continue
            delta = bs_delta("put", tk_data.price, K_short, T, RISK_FREE_RATE, short_iv)
            if pd.isna(delta) or abs(delta - target_delta) > delta_tol:
                continue
            K_long = K_short - width
            long_match = puts.iloc[(puts["strike"] - K_long).abs().argsort()[:1]]
            if long_match.empty:
                continue
            long_row = long_match.iloc[0]
            if abs(long_row["strike"] - K_long) > max(1.0, width * 0.5):  # allow for varying strike spacing
                continue
            long_ask = long_row.get("ask", 0) or 0
            if long_ask <= 0:
                continue
            net_credit = short_bid - long_ask
            actual_width = K_short - long_row["strike"]
            if net_credit <= 0 or actual_width <= 0:
                continue
            max_loss = actual_width - net_credit
            roi = (net_credit / max_loss) * 100 if max_loss > 0 else 0
            ayield = annualized_yield_pct(net_credit, max_loss, dte)
            results.append({
                "ticker": tk_data.ticker, "strategy": "BULL PUT SPR", "expiration": exp,
                "dte": dte, "short_strike": K_short, "long_strike": long_row["strike"],
                "width": round(actual_width, 2), "credit": round(net_credit, 2),
                "delta": round(delta, 3), "max_loss": round(max_loss, 2),
                "roi_%": round(roi, 1), "ann_yield_%": round(ayield, 2),
                "underlying_px": round(tk_data.price, 2),
            })
    return results


def scan_vertical_calls(tk_data, tk_obj, dte_min, dte_max, width=5.0,
                         target_delta=0.30, delta_tol=0.15):
    """Bear call spread: sell OTM call, buy further OTM call `width` higher."""
    results = []
    for exp, dte in filter_expirations(tk_data.expirations, dte_min, dte_max):
        try:
            calls, _ = get_chain(tk_obj, exp)
        except Exception:
            continue
        T = dte / 365.0
        calls = calls.sort_values("strike")
        for _, short_row in calls.iterrows():
            K_short = short_row["strike"]
            if K_short <= tk_data.price:
                continue
            short_bid = short_row.get("bid", 0) or 0
            short_iv = short_row.get("impliedVolatility", np.nan)
            if short_bid <= 0 or pd.isna(short_iv) or short_iv <= 0:
                continue
            delta = bs_delta("call", tk_data.price, K_short, T, RISK_FREE_RATE, short_iv)
            if pd.isna(delta) or abs(delta - target_delta) > delta_tol:
                continue
            K_long = K_short + width
            long_match = calls.iloc[(calls["strike"] - K_long).abs().argsort()[:1]]
            if long_match.empty:
                continue
            long_row = long_match.iloc[0]
            if abs(long_row["strike"] - K_long) > max(1.0, width * 0.5):
                continue
            long_ask = long_row.get("ask", 0) or 0
            if long_ask <= 0:
                continue
            net_credit = short_bid - long_ask
            actual_width = long_row["strike"] - K_short
            if net_credit <= 0 or actual_width <= 0:
                continue
            max_loss = actual_width - net_credit
            roi = (net_credit / max_loss) * 100 if max_loss > 0 else 0
            ayield = annualized_yield_pct(net_credit, max_loss, dte)
            results.append({
                "ticker": tk_data.ticker, "strategy": "BEAR CALL SPR", "expiration": exp,
                "dte": dte, "short_strike": K_short, "long_strike": long_row["strike"],
                "width": round(actual_width, 2), "credit": round(net_credit, 2),
                "delta": round(delta, 3), "max_loss": round(max_loss, 2),
                "roi_%": round(roi, 1), "ann_yield_%": round(ayield, 2),
                "underlying_px": round(tk_data.price, 2),
            })
    return results


def filter_by_credit(results, min_credit=None, max_credit=None):
    """Filter a list of spread result dicts by net credit. Returns (filtered, before_count)."""
    before = len(results)
    if min_credit is None and max_credit is None:
        return results, before
    out = []
    for r in results:
        c = r.get("credit", 0)
        if min_credit is not None and c < min_credit:
            continue
        if max_credit is not None and c > max_credit:
            continue
        out.append(r)
    return out, before


def filter_by_premium(results, min_premium=None, max_premium=None):
    """Filter a list of CSP/CC result dicts by option premium (bid price). Returns (filtered, before_count)."""
    before = len(results)
    if min_premium is None and max_premium is None:
        return results, before
    out = []
    for r in results:
        p = r.get("price", 0)
        if min_premium is not None and p < min_premium:
            continue
        if max_premium is not None and p > max_premium:
            continue
        out.append(r)
    return out, before


# ----------------------------------------------------------------------------
# Scoring / ranking
# ----------------------------------------------------------------------------
def combo_score(row, target_delta):
    """Weighted score: annualized yield + delta proximity + IV, normalized-ish."""
    yield_component = row.get("ann_yield_%", row.get("roi_%", 0)) or 0
    delta = row.get("delta", 0) or 0
    delta_component = max(0, 20 - abs(abs(delta) - abs(target_delta)) * 100)
    iv = row.get("iv", 0) or 0
    iv_component = min(iv, 100) * 0.3
    return round(yield_component * 0.6 + delta_component * 0.25 + iv_component * 0.15, 2)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Option screener for CSP/CC/vertical spreads")
    parser.add_argument("tickers", nargs="*", help="Override watchlist tickers")
    parser.add_argument("--dte-min", type=int, default=None,
                         help="Min days to expiration (default 21, or set by --dte-preset)")
    parser.add_argument("--dte-max", type=int, default=None,
                         help="Max days to expiration (default 45, or set by --dte-preset)")
    parser.add_argument("--dte-preset", default=None,
                         choices=["weekly", "biweekly", "monthly", "quarterly"],
                         help="Shortcut for common DTE windows: "
                              "weekly=5-10, biweekly=10-21, monthly=21-45, quarterly=60-90")
    parser.add_argument("--width", type=float, default=5.0, help="Vertical spread width in $")
    parser.add_argument("--delta-tol", type=float, default=0.15,
                         help="How far strikes can be from the target delta (0.30). "
                              "Bigger number = more results, further from current price. "
                              "Default 0.15 (i.e. delta 0.15-0.45). Try 0.25-0.30 for a much wider net.")
    parser.add_argument("--min-credit", type=float, default=None,
                         help="Minimum net credit ($) for vertical spreads, e.g. 0.15")
    parser.add_argument("--max-credit", type=float, default=None,
                         help="Maximum net credit ($) for vertical spreads, e.g. 0.50")
    parser.add_argument("--min-premium", type=float, default=None,
                         help="Minimum premium ($) for CSP/CC single options, e.g. 0.20")
    parser.add_argument("--max-premium", type=float, default=None,
                         help="Maximum premium ($) for CSP/CC single options, e.g. 2.00")
    parser.add_argument("--strategies", nargs="+", default=["csp", "cc", "vertical"],
                         choices=["csp", "cc", "vertical"])
    parser.add_argument("--top", type=int, default=15, help="Show top N results per strategy (0 = show all)")
    parser.add_argument("--csv", default=None, help="Optional path to save full results as CSV")
    args = parser.parse_args()

    DTE_PRESETS = {
        "weekly": (5, 10),
        "biweekly": (10, 21),
        "monthly": (21, 45),
        "quarterly": (60, 90),
    }
    if args.dte_preset:
        preset_min, preset_max = DTE_PRESETS[args.dte_preset]
        args.dte_min = args.dte_min if args.dte_min is not None else preset_min
        args.dte_max = args.dte_max if args.dte_max is not None else preset_max
    else:
        args.dte_min = args.dte_min if args.dte_min is not None else 21
        args.dte_max = args.dte_max if args.dte_max is not None else 45

    if args.dte_min > args.dte_max:
        parser.error("--dte-min cannot be greater than --dte-max")

    watchlist = args.tickers if args.tickers else WATCHLIST
    print(f"Scanning {len(watchlist)} tickers: {', '.join(watchlist)}")
    print(f"DTE window: {args.dte_min}-{args.dte_max} days | Spread width: ${args.width}\n")

    all_csp, all_cc, all_vert = [], [], []

    for ticker in watchlist:
        print(f"  -> {ticker}...", end=" ", flush=True)
        try:
            tk_obj = yf.Ticker(ticker)
            tk_data = get_ticker_data(ticker)
            if tk_data is None or not tk_data.expirations:
                print("no data/options, skipping")
                continue
        except Exception as e:
            print(f"error ({e}), skipping")
            continue

        n_before = len(all_csp) + len(all_cc) + len(all_vert)
        if "csp" in args.strategies:
            all_csp.extend(scan_csp(tk_data, tk_obj, args.dte_min, args.dte_max, delta_tol=args.delta_tol))
        if "cc" in args.strategies:
            all_cc.extend(scan_cc(tk_data, tk_obj, args.dte_min, args.dte_max, delta_tol=args.delta_tol))
        if "vertical" in args.strategies:
            all_vert.extend(scan_vertical_puts(tk_data, tk_obj, args.dte_min, args.dte_max, args.width,
                                                delta_tol=args.delta_tol))
            all_vert.extend(scan_vertical_calls(tk_data, tk_obj, args.dte_min, args.dte_max, args.width,
                                                 delta_tol=args.delta_tol))
        n_after = len(all_csp) + len(all_cc) + len(all_vert)
        print(f"{n_after - n_before} candidates found")

    def show(results, name, target_delta):
        if not results:
            print(f"\nNo {name} candidates matched your filters.")
            return None
        df = pd.DataFrame(results)
        df["score"] = df.apply(lambda r: combo_score(r, target_delta), axis=1)
        df = df.sort_values("score", ascending=False)
        if args.top > 0:
            df = df.head(args.top)
        df = df.reset_index(drop=True)
        print(f"\n=== Top {name} candidates ===")
        if HAS_TABULATE:
            print(tabulate(df, headers="keys", tablefmt="simple", showindex=False))
        else:
            print(df.to_string(index=False))
        return df

    if "csp" in args.strategies and (args.min_premium is not None or args.max_premium is not None):
        all_csp, before_count = filter_by_premium(all_csp, args.min_premium, args.max_premium)
        print(f"\nPremium filter CSP (${args.min_premium}-${args.max_premium}): "
              f"{before_count} candidates -> {len(all_csp)} passed")
    if "cc" in args.strategies and (args.min_premium is not None or args.max_premium is not None):
        all_cc, before_count = filter_by_premium(all_cc, args.min_premium, args.max_premium)
        print(f"Premium filter CC (${args.min_premium}-${args.max_premium}): "
              f"{before_count} candidates -> {len(all_cc)} passed")

    if "vertical" in args.strategies and (args.min_credit is not None or args.max_credit is not None):
        all_vert, before_count = filter_by_credit(all_vert, args.min_credit, args.max_credit)
        print(f"\nCredit filter (${args.min_credit}-${args.max_credit}): "
              f"{before_count} candidates -> {len(all_vert)} passed")

    csp_df = show(all_csp, "CSP", -0.30) if "csp" in args.strategies else None
    cc_df = show(all_cc, "CC", 0.30) if "cc" in args.strategies else None
    vert_df = show(all_vert, "Vertical Spread", 0.30) if "vertical" in args.strategies else None

    if args.csv:
        frames = [d for d in [csp_df, cc_df, vert_df] if d is not None]
        if frames:
            combined = pd.concat(frames, ignore_index=True, sort=False)
            combined.to_csv(args.csv, index=False)
            print(f"\nSaved full results to {args.csv}")


if __name__ == "__main__":
    main()
