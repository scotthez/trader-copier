#!/usr/bin/env python3
"""Risk report for prop-firm sizing: reads the trader's own journal/runs, scores each closed signal
in R (risk multiples, currency-free), and solves for the risk_pct_per_leg that stays under a daily and
total drawdown limit.

R convention: one leg stopped at the signal's ORIGINAL stop = -1R. A leg that reached its TP scores
+(reward distance / original stop distance) R. A leg moved to break-even and closed there ~0R. So a
clean full stop on a 3-leg signal is about -3R, and risk_pct_per_leg is the money value of 1R.

Usage:  python3 scripts/risk_report.py [trader.sqlite] [--provider wolves] [--daily 5] [--total 10] [--safety 0.6]
Read-only. Does not touch the database or place anything.
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime


def _close_price(leg: dict, closes: dict) -> float | None:
    """Price the leg actually closed at: the journal's leg_closed price if we have it, else the TP/SL
    the state implies. None when the leg never reached a close (still open, pending, cancelled)."""
    p = closes.get(leg["n"])
    if p is not None:
        return p
    st = leg.get("state")
    if st == "CLOSED_TP":
        return leg.get("tp")
    if st == "CLOSED_SL":
        return leg.get("sl_current")
    return None


def run_r(run: dict, closes: dict) -> float | None:
    """Net R of one signal across its legs, or None if nothing closed (so it doesn't count as a 0)."""
    sig = run["signal"]
    buy = sig["side"] == "BUY"
    scored = 0.0
    any_closed = False
    for leg in run["legs"]:
        entry = leg.get("entry_price")
        if entry is None:
            continue                                  # never filled
        risk = abs(entry - sig["sl"])
        if risk <= 0:
            continue
        close = _close_price(leg, closes)
        if close is None:
            continue                                  # still open / cancelled
        any_closed = True
        scored += ((close - entry) if buy else (entry - close)) / risk
    return scored if any_closed else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Prop-firm risk report from the trader journal")
    ap.add_argument("db", nargs="?", default="trader.sqlite")
    ap.add_argument("--provider", default="wolves")
    ap.add_argument("--daily", type=float, default=5.0, help="max daily drawdown %% (prop rule)")
    ap.add_argument("--total", type=float, default=10.0, help="max total drawdown %% (prop rule)")
    ap.add_argument("--safety", type=float, default=0.6, help="use this fraction of the limit (0-1)")
    a = ap.parse_args(argv)

    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row
    # close prices per (run_id, leg) from the journal
    closes: dict[str, dict] = defaultdict(dict)
    for r in conn.execute("SELECT run_id, detail FROM journal WHERE provider=? AND kind='leg_closed'", (a.provider,)):
        d = json.loads(r["detail"])
        if r["run_id"] is not None and d.get("price") is not None:
            closes[r["run_id"]][d["leg"]] = d["price"]

    rows = conn.execute("SELECT id, data FROM runs WHERE provider=? ORDER BY telegram_msg_id", (a.provider,)).fetchall()
    per_day: dict[str, float] = defaultdict(float)
    results: list[tuple[str, float]] = []           # (date, runR) in time order
    n_signals = n_scored = 0
    for row in rows:
        run = json.loads(row["data"])
        n_signals += 1
        rr = run_r(run, closes.get(row["id"], {}))
        if rr is None:
            continue
        n_scored += 1
        day = run["signal"]["received_at"][:10]
        per_day[day] += rr
        results.append((day, rr))

    if n_scored == 0:
        print(f"No closed {a.provider} signals in {a.db} yet — let it trade for a while, then re-run.")
        return 1

    wins = sum(1 for _, r in results if r > 0.05)
    losses = sum(1 for _, r in results if r < -0.05)
    scratch = n_scored - wins - losses
    total_r = sum(r for _, r in results)

    # worst single day, and worst peak-to-trough drawdown on the cumulative R curve
    worst_day = min(per_day.values())
    equity, peak, max_dd = 0.0, 0.0, 0.0
    streak = worst_streak = 0
    for _, r in results:
        equity += r
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
        streak = streak + 1 if r < -0.05 else 0
        worst_streak = max(worst_streak, streak)

    print(f"=== {a.provider} risk report ({a.db}) ===")
    print(f"signals: {n_signals} total, {n_scored} with a closed outcome")
    print(f"win/scratch/loss: {wins}/{scratch}/{losses}  ({wins/n_scored*100:.0f}% win)")
    print(f"total: {total_r:+.1f}R over {len(per_day)} trading days  (avg {total_r/n_scored:+.2f}R/signal)")
    print(f"worst single day: {worst_day:+.1f}R")
    print(f"worst drawdown (peak->trough): {max_dd:+.1f}R")
    print(f"longest losing streak: {worst_streak} signals")

    # risk that keeps the historical worst day under --daily and worst drawdown under --total
    daily_cap = a.daily / abs(worst_day) if worst_day < 0 else float("inf")
    total_cap = a.total / abs(max_dd) if max_dd < 0 else float("inf")
    binding = "daily" if daily_cap <= total_cap else "total"
    rec = min(daily_cap, total_cap) * a.safety
    print()
    print(f"to stay under {a.daily:.0f}% day / {a.total:.0f}% total on THIS history:")
    print(f"  max risk per leg: {min(daily_cap, total_cap):.2f}% (binding limit: {binding})")
    print(f"  recommended (x{a.safety:.0%} safety): risk_pct_per_leg: {rec:.2f}")
    print(f"  -> worst historical day would have been {abs(worst_day)*rec:.1f}% of equity, "
          f"worst drawdown {abs(max_dd)*rec:.1f}%")
    print()
    print("NOTE: this is the bot's signals only. If other copiers trade the same prop account, their")
    print("drawdown counts against the prop limits too and is NOT in this number.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
