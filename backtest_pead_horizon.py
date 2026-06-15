#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – PEAD hålltids-sweep: hur länge driver drifteffekten?

För kvalificerade event (överraskning >= 5% ELLER reaktion >= 5%) mäts
KUMULATIV överavkastning mot SPY vid många horisonter. Då ser man:
  • var totala driften TOPPAR (naturlig hålltid),
  • MARGINALEN (vad varje extra vecka tillför) – när den ~0 är driften slut,
  • överavk PER DAG (kapitalet är ju låst – snabb återinvestering kan löna sig),
  • och edgen ÖVER BASLINJEN (alla rapporter) på samma horisont.

Körning:  python backtest_pead_horizon.py
"""

from __future__ import annotations

import sys

from backtest_pead import (load_universe, daily_close, earnings_events,
                           reaction, forward, SURPRISE_MIN, REACTION_MIN)

HS = [5, 10, 20, 30, 40, 50, 60, 75, 90, 120]   # handelsdagar


def _stats(vals):
    if not vals:
        return None
    n = len(vals)
    mean = sum(vals) / n
    wins = sum(1 for v in vals if v > 0)
    sv = sorted(vals)
    med = sv[n // 2]
    return {"n": n, "mean": mean, "med": med, "hit": 100.0 * wins / n}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    universe_file = args[0] if args else "universe/usa.csv"
    bench_sym = args[1] if len(args) > 1 else "SPY"

    bench = daily_close(bench_sym)
    if bench is None:
        print("Ingen indexdata.", file=sys.stderr)
        return 1
    universe = load_universe(universe_file)
    print(f"Hämtar {len(universe)} aktier + {bench_sym} …", file=sys.stderr)

    qual = {h: [] for h in HS}
    base = {h: [] for h in HS}
    for sym in universe:
        prices = daily_close(sym)
        if prices is None or len(prices) < 130:
            continue
        for ev_date, surprise in earnings_events(sym):
            react = reaction(prices, bench, ev_date)
            ok = (surprise is not None and surprise >= SURPRISE_MIN) or \
                 (react is not None and react >= REACTION_MIN)
            for h in HS:
                fr = forward(prices, bench, ev_date, h)
                if fr is None:
                    continue
                ex = fr[0] - fr[1]
                base[h].append(ex)
                if ok:
                    qual[h].append(ex)

    print("\n" + "=" * 78)
    print(" PEAD HÅLLTIDS-SWEEP – USA (kvalificerade event, överavk mot SPY)")
    print("=" * 78)
    print(" Kumulativ överavkastning från inträde (dagen efter rapport) till exit.\n")
    print(f"   {'horisont':>10} {'n':>6} {'överavk':>9} {'median':>8} "
          f"{'Δ/period':>9} {'/dag':>7} {'träff':>6} {'vs baslinje':>12}")
    prev = None
    rows = []
    for h in HS:
        q = _stats(qual[h])
        b = _stats(base[h])
        if not q:
            continue
        marg = (q["mean"] - prev) if prev is not None else q["mean"]
        per_day = q["mean"] / h
        vs_base = q["mean"] - (b["mean"] if b else 0.0)
        cal = round(h * 1.4)
        rows.append((h, q, marg, per_day, vs_base))
        print(f"   {h:>3}d (~{cal:>3}k) {q['n']:>6} {q['mean']:>+8.2f}% "
              f"{q['med']:>+7.2f}% {marg:>+8.2f}% {per_day:>+6.3f}% "
              f"{q['hit']:>5.0f}% {vs_base:>+11.2f}%")
        prev = q["mean"]

    if rows:
        peak = max(rows, key=lambda r: r[1]["mean"])
        best_day = max(rows, key=lambda r: r[3])
        print("\n SLUTSATS:")
        print(f"  • Total överavkastning toppar vid ~{peak[0]} handelsdagar "
              f"(~{round(peak[0]*1.4)} kalenderdagar): {peak[1]['mean']:+.2f}% mot SPY.")
        print(f"  • Bäst överavkastning PER DAG vid ~{best_day[0]} handelsdagar "
              f"({best_day[3]:+.3f}%/dag) – snabbast återinvestering av kapitalet.")
        print("  • När Δ/period planar ut/blir negativ är driften slut → sälj då,")
        print("    eller rulla över till momentum om aktien fortfarande rankas högt.")
        print(f"  • Systemets nuvarande hold_days=70 kalenderdagar ≈ {round(70/1.4)} handelsdagar.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
