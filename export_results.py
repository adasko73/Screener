#!/usr/bin/env python3
"""
export_results.py
==================
Runs the real scanning logic from option_screener.py using the settings in
screener_config.json, and writes the results to docs/results.json for the
mobile web page to read.

This is the piece that GitHub Actions runs on a schedule. It does NOT modify
option_screener.py - it imports and reuses its functions directly, so the
CLI tool and the deployed scanner always share identical scan logic.

Usage:
    python export_results.py
    python export_results.py --config screener_config.json --out docs/results.json
"""

import json
import argparse
from datetime import datetime, timezone

import option_screener as sc  # the real scanner - unmodified


def to_jsonable(obj):
    """Convert numpy/pandas scalar types to plain Python types for json.dump."""
    if hasattr(obj, "item"):
        return obj.item()
    return obj


def clean_rows(rows):
    return [{k: to_jsonable(v) for k, v in row.items()} for row in rows]


def get_daily_change_pct(ticker_obj, last_price):
    """Percent change vs previous close. Returns None if unavailable."""
    try:
        prev_close = ticker_obj.fast_info.get("previousClose") or ticker_obj.fast_info.get("previous_close")
        if prev_close and last_price:
            return round((last_price - prev_close) / prev_close * 100.0, 2)
    except Exception:
        pass
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="screener_config.json")
    parser.add_argument("--out", default="docs/results.json")
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)

    watchlist = cfg.get("watchlist", sc.WATCHLIST)
    dte_min = int(cfg.get("dte_min", 21))
    dte_max = int(cfg.get("dte_max", 45))
    width = float(cfg.get("width", 5.0))
    delta_tol = float(cfg.get("delta_tol", 0.15))
    min_credit = float(cfg["min_credit"]) if cfg.get("min_credit") not in (None, "") else None
    max_credit = float(cfg["max_credit"]) if cfg.get("max_credit") not in (None, "") else None
    min_premium = float(cfg["min_premium"]) if cfg.get("min_premium") not in (None, "") else None
    max_premium = float(cfg["max_premium"]) if cfg.get("max_premium") not in (None, "") else None
    strategies = cfg.get("strategies", {"csp": True, "cc": True, "vertical": True})
    top_n = int(cfg.get("top_n", 0))

    all_csp, all_cc, all_put_spreads, all_call_spreads = [], [], [], []
    prices = {}
    errors = []

    for ticker in watchlist:
        try:
            tk_obj = __import__("yfinance").Ticker(ticker)
            tk_data = sc.get_ticker_data(ticker)
            if tk_data is None or not tk_data.expirations:
                errors.append(f"{ticker}: no data/options")
                continue
        except Exception as e:
            errors.append(f"{ticker}: {e}")
            continue

        chg_pct = get_daily_change_pct(tk_obj, tk_data.price)
        earnings_days = sc.get_next_earnings_days(ticker)
        prices[ticker] = {
            "price": round(tk_data.price, 2),
            "chg_pct": chg_pct,
            "earnings_days": earnings_days,
        }

        if strategies.get("csp"):
            all_csp.extend(sc.scan_csp(tk_data, tk_obj, dte_min, dte_max, delta_tol=delta_tol))
        if strategies.get("cc"):
            all_cc.extend(sc.scan_cc(tk_data, tk_obj, dte_min, dte_max, delta_tol=delta_tol))
        if strategies.get("vertical"):
            all_put_spreads.extend(
                sc.scan_vertical_puts(tk_data, tk_obj, dte_min, dte_max, width, delta_tol=delta_tol)
            )
            all_call_spreads.extend(
                sc.scan_vertical_calls(tk_data, tk_obj, dte_min, dte_max, width, delta_tol=delta_tol)
            )

    if strategies.get("csp") and (min_premium is not None or max_premium is not None):
        all_csp, _ = sc.filter_by_premium(all_csp, min_premium, max_premium)
    if strategies.get("cc") and (min_premium is not None or max_premium is not None):
        all_cc, _ = sc.filter_by_premium(all_cc, min_premium, max_premium)
    if strategies.get("vertical") and (min_credit is not None or max_credit is not None):
        all_put_spreads, _ = sc.filter_by_credit(all_put_spreads, min_credit, max_credit)
        all_call_spreads, _ = sc.filter_by_credit(all_call_spreads, min_credit, max_credit)

    def rank(results, target_delta):
        for r in results:
            r["score"] = sc.combo_score(r, target_delta)
        results.sort(key=lambda r: r["score"], reverse=True)
        if top_n > 0:
            results = results[:top_n]
        return results

    all_csp = rank(all_csp, -0.30)
    all_cc = rank(all_cc, 0.30)
    all_put_spreads = rank(all_put_spreads, 0.30)
    all_call_spreads = rank(all_call_spreads, 0.30)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "dte_min": dte_min, "dte_max": dte_max, "width": width,
            "delta_tol": delta_tol, "min_credit": min_credit, "max_credit": max_credit,
            "min_premium": min_premium, "max_premium": max_premium,
        },
        "prices": prices,
        "csp": clean_rows(all_csp),
        "cc": clean_rows(all_cc),
        "put_spreads": clean_rows(all_put_spreads),
        "call_spreads": clean_rows(all_call_spreads),
        "errors": errors,
    }

    with open(args.out, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {args.out}: {len(all_csp)} CSP, {len(all_cc)} CC, "
          f"{len(all_put_spreads)} put spreads, {len(all_call_spreads)} call spreads. "
          f"{len(errors)} ticker errors.")


if __name__ == "__main__":
    main()
