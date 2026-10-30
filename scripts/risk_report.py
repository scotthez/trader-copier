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
    present = [r["provider"] for r in conn.execute("SELECT DISTINCT provider FROM runs ORDER BY provider")]
    if not present:
        print(f"No runs in {a.db} yet.")
        return 1
    wanted = present if a.provider.lower() == "all" else [p.strip() for p in a.provider.split(",")]
    missing = [p for p in wanted if p not in present]
    if missing:
        print(f"Provider(s) {missing} have no runs. Present: {present}")
        return 1

    # close prices per run_id/leg from the journal (run_id is 'provider:msgid', unique across providers)
    closes: dict[str, dict] = defaultdict(dict)
    for r in conn.execute("SELECT run_id, detail FROM journal WHERE kind='leg_closed'"):
        d = json.loads(r["detail"])
        if r["run_id"] is not None and d.get("price") is not None:
            closes[r["run_id"]][d["leg"]] = d["price"]

    def score(provider: str) -> list[tuple[str, float]]:
        """(received_at_iso, runR) for each closed signal of this provider, in time order."""
        out = []
        for row in conn.execute("SELECT id, data FROM runs WHERE provider=? ORDER BY telegram_msg_id", (provider,)):
            run = json.loads(row["data"])
            rr = run_r(run, closes.get(row["id"], {}))
            if rr is not None:
                out.append((run["signal"]["received_at"], rr))
        return out

    def stats(results: list[tuple[str, float]]):
        """worst single day, worst peak->trough drawdown, longest losing streak, from time-ordered (ts, R)."""
        per_day: dict[str, float] = defaultdict(float)
        for ts, r in results:
            per_day[ts[:10]] += r
        ordered = sorted(results)
        equity = peak = max_dd = 0.0
        streak = worst_streak = 0
        for _, r in ordered:
            equity += r
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
            streak = streak + 1 if r < -0.05 else 0
            worst_streak = max(worst_streak, streak)
        worst_day = min(per_day.values()) if per_day else 0.0
        return per_day, worst_day, max_dd, worst_streak

    by_provider = {p: score(p) for p in wanted}
    print(f"=== risk report ({a.db}) ===")
    print(f"providers present: {present}   analysed: {wanted}\n")
    for p in wanted:
        res = by_provider[p]
        if not res:
            print(f"[{p}] no closed signals yet\n")
            continue
        wins = sum(1 for _, r in res if r > 0.05)
        losses = sum(1 for _, r in res if r < -0.05)
        pd, wd, dd, ws = stats(res)
        print(f"[{p}] {len(res)} closed signals, {wins/len(res)*100:.0f}% win | total {sum(r for _,r in res):+.1f}R "
              f"over {len(pd)} days | worst day {wd:+.1f}R | worst DD {dd:+.1f}R | longest losing streak {ws}")

    combined = [row for p in wanted for row in by_provider[p]]
    if not combined:
        print("\nNo closed signals across the selected providers yet — let it trade, then re-run.")
        return 1

    _, c_day, c_dd, c_streak = stats(combined)
    print(f"\n--- COMBINED across {wanted} (what the prop account's daily/total limit actually sees) ---")
    print(f"worst single day: {c_day:+.1f}R   worst drawdown: {c_dd:+.1f}R   longest losing streak: {c_streak}")

    daily_cap = a.daily / abs(c_day) if c_day < 0 else float("inf")
    total_cap = a.total / abs(c_dd) if c_dd < 0 else float("inf")
    binding = "daily" if daily_cap <= total_cap else "total"
    cap = min(daily_cap, total_cap)
    rec = cap * a.safety
    print()
    print(f"to keep the COMBINED worst day under {a.daily:.0f}% and worst drawdown under {a.total:.0f}%:")
    print(f"  max risk per leg: {cap:.2f}% (binding limit: {binding})")
    print(f"  recommended (x{a.safety:.0%} safety): risk_pct_per_leg: {rec:.2f}  (set this on EACH provider)")
    print(f"  -> worst historical day ~{abs(c_day)*rec:.1f}% of equity, worst drawdown ~{abs(c_dd)*rec:.1f}%")
    print()
    print("ASSUMES every analysed provider runs the same risk_pct_per_leg. NOTE: any OTHER copier on the")
    print("same prop account draws down against the limit too and is NOT counted here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
