#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – PEAD portfölj-backtest (equity-kurva vs index, MED courtage)

Event-studyn (backtest_pead.py) mäter snittdriften per rapport. Den här
simulerar i stället en HANDLINGSBAR PEAD-portfölj över tid och jämför mot
index (SPY, total return):

  • Regel: när ett event kvalificerar (vinstöverraskning >= 5% ELLER onormal
    rapportreaktion >= 5%) köps aktien vid stängning DAGEN EFTER rapporten och
    hålls driftfönstret (~70 kalenderdagar), precis som pead.py:s drift-portfölj.
  • Storlek: likaviktat över MAX_POS (10) platser. Fler signaler än platser →
    de med störst överraskning prioriteras; resten missas (kapitaltak). Ledig
    kassa ligger i 0 % (ingen ränta antagen) – PEAD är inte alltid fullinvesterad.
  • Courtage: dras vid VARJE köp och VARJE sälj (round-trip = 2× en-vägs).
    Körs på flera nivåer så man ser känsligheten. För USA-aktier från ett
    SEK-konto motsvarar ~0,25 % ungefär courtage + valutaväxling (Avanza/Nordnet).

Daglig mark-to-market → CAGR, volatilitet, Sharpe, max nedgång, andel kapital
som faktiskt är deployat, och årlig avkastning mot SPY.

Ärligt: survivorship bias (dagens bolag), surprise% är konsensus-snapshot (mild
look-ahead), ingen slippage/spread utöver courtage, long-only.

Körning:  python backtest_pead_portfolio.py            # universe/usa.csv, SPY
          python backtest_pead_portfolio.py universe/usa.csv SPY
"""

from __future__ import annotations

import sys
from collections import defaultdict

import pandas as pd

from backtest_pead import (load_universe, daily_close, earnings_events,
                           reaction, SURPRISE_MIN, REACTION_MIN)

HOLD_DAYS = 70
MAX_POS = 10
COST_LEVELS = [0.0, 0.05, 0.10, 0.25]   # courtage % per affär (en väg)


def build_trades(universe: list[str], bench: pd.Series):
    """prices[sym] = dagsserie; trades = lista kvalificerade event."""
    prices, trades = {}, []
    for sym in universe:
        s = daily_close(sym)
        if s is None or len(s) < 90:
            continue
        prices[sym] = s
        for ev_date, surprise in earnings_events(sym):
            react = reaction(s, bench, ev_date)
            ok = (surprise is not None and surprise >= SURPRISE_MIN) or \
                 (react is not None and react >= REACTION_MIN)
            if not ok:
                continue
            strength = surprise if surprise is not None else (react or 0.0)
            trades.append({"sym": sym, "report": ev_date, "strength": strength})
    return prices, trades


def simulate(prices, trades, bench, cost_pct, max_pos):
    """Daglig portföljsimulering på SPY:s handelskalender."""
    cost = cost_pct / 100.0
    cal = bench.index                                   # SPY = portföljkalender
    caldates = [d.date() for d in cal]
    di = {d: i for i, d in enumerate(caldates)}
    N = len(cal)

    # Dagsavkastning per aktie, omindexerad till kalendern
    rets = {}
    for sym, s in prices.items():
        r = s.reindex(cal).ffill()
        rets[sym] = r.pct_change().fillna(0.0).to_numpy()

    # Mappa varje kvalificerat event -> (entry_i, exit_i) på kalendern
    entries_by_i = defaultdict(list)
    first_i = N
    for t in trades:
        if t["sym"] not in rets:
            continue
        ev = t["report"]
        entry_i = next((i for i, d in enumerate(caldates) if d > ev), None)
        if entry_i is None:
            continue
        exit_i = next((i for i in range(entry_i + 1, N)
                       if (caldates[i] - ev).days >= HOLD_DAYS), None)
        if exit_i is None:
            continue
        entries_by_i[entry_i].append({"sym": t["sym"], "exit_i": exit_i,
                                      "strength": t["strength"]})
        first_i = min(first_i, entry_i)
    if first_i >= N:
        return None

    cash = 1.0
    positions = []          # {sym, value, exit_i}
    eq = [0.0] * N
    deployed = []
    n_trades = 0
    for i in range(first_i, N):
        # 1) markera till marknad
        for p in positions:
            p["value"] *= (1.0 + rets[p["sym"]][i])
        # 2) sälj utgångna (courtage på säljet)
        still = []
        for p in positions:
            if p["exit_i"] <= i:
                cash += p["value"] * (1.0 - cost)
                n_trades += 1
            else:
                still.append(p)
        positions = still
        # 3) köp nya (starkast överraskning först), courtage på köpet
        for ev in sorted(entries_by_i.get(i, []), key=lambda e: e["strength"], reverse=True):
            if len(positions) >= max_pos or cash <= 1e-9:
                break
            equity = cash + sum(p["value"] for p in positions)
            size = min(equity / max_pos, cash)
            if size <= 1e-9:
                break
            cash -= size
            positions.append({"sym": ev["sym"], "value": size * (1.0 - cost),
                              "exit_i": ev["exit_i"]})
            n_trades += 1
        invested = sum(p["value"] for p in positions)
        eq[i] = cash + invested
        deployed.append(invested / eq[i] if eq[i] > 0 else 0.0)

    span = range(first_i, N)
    equity = pd.Series([eq[i] for i in span], index=[cal[i] for i in span])
    return {"equity": equity, "first_i": first_i,
            "deployed": sum(deployed) / len(deployed) if deployed else 0.0,
            "n_trades": n_trades}


def metrics(equity: pd.Series) -> dict:
    r = equity.pct_change().dropna()
    n = len(r)
    if n < 2:
        return {}
    total = float(equity.iloc[-1] / equity.iloc[0])
    cagr = total ** (252.0 / n) - 1
    vol = float(r.std()) * (252 ** 0.5)
    sharpe = (float(r.mean()) / float(r.std()) * (252 ** 0.5)) if r.std() > 0 else 0.0
    peak, mdd = -1e9, 0.0
    for v in equity.values:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"cagr": cagr, "vol": vol, "sharpe": sharpe, "mdd": mdd, "total": total - 1}


def yearly(equity: pd.Series) -> dict:
    r = equity.pct_change().dropna()
    by = {}
    for d, x in r.items():
        y = d.year
        by[y] = by.get(y, 1.0) * (1 + x)
    return {y: v - 1 for y, v in by.items()}


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
        print("Ingen indexdata (kör lokalt med Yahoo-åtkomst).", file=sys.stderr)
        return 1
    universe = load_universe(universe_file)
    print(f"Hämtar {len(universe)} aktier + {bench_sym} (dagsdata + rapporter) …",
          file=sys.stderr)
    prices, trades = build_trades(universe, bench)

    runs = {}
    for c in COST_LEVELS:
        runs[c] = simulate(prices, trades, bench, c, MAX_POS)
    base = runs[COST_LEVELS[0]]
    if base is None:
        print("Inga affärer att simulera.", file=sys.stderr)
        return 1

    # SPY buy & hold över samma period
    span_idx = base["equity"].index
    spy = bench.reindex(span_idx).ffill()
    spy = spy / spy.iloc[0]
    ms = metrics(spy)

    print("\n" + "=" * 72)
    print(f" PEAD PORTFÖLJ-BACKTEST – USA  (likaviktat {MAX_POS} platser, håll {HOLD_DAYS}d)")
    print("=" * 72)
    print("\n VARNINGAR: survivorship bias (dagens bolag); surprise% = konsensus-")
    print(" snapshot (mild look-ahead); ingen slippage utöver courtage; long-only.")
    print(f" Period: {span_idx[0].date()} .. {span_idx[-1].date()}  ·  index = {bench_sym} (total return)")
    print(f" Kvalificerade affärer: {base['n_trades']} köp+sälj  ·  snittandel kapital deployat: {base['deployed']*100:.0f} %")

    print("\n PEAD-PORTFÖLJ per courtagenivå (per affär, en väg):")
    print(f"   {'courtage':>9} {'CAGR':>8} {'vol':>6} {'Sharpe':>7} {'maxDD':>7} {'totalt':>9}")
    for c in COST_LEVELS:
        m = metrics(runs[c]["equity"]) if runs[c] else {}
        if not m:
            continue
        print(f"   {c:>7.2f} % {m['cagr']:>+7.1%} {m['vol']:>6.0%} "
              f"{m['sharpe']:>7.2f} {m['mdd']:>+7.0%} {m['total']:>+9.0%}")
    print(f"   {'SPY B&H':>9} {ms['cagr']:>+7.1%} {ms['vol']:>6.0%} "
          f"{ms['sharpe']:>7.2f} {ms['mdd']:>+7.0%} {ms['total']:>+9.0%}")

    # Årlig jämförelse vid en realistisk nivå (0,10 %)
    real = runs[0.10] if runs.get(0.10) else base
    ys, yb = yearly(real["equity"]), yearly(spy)
    print(f"\n Årlig avkastning (courtage 0,10 %)  vs  {bench_sym}:")
    print(f"   {'År':<6}{'PEAD':>10}{'SPY':>10}{'Diff':>10}")
    for y in sorted(ys):
        b = yb.get(y)
        bs = f"{b:+.1%}" if b is not None else "  –"
        ds = f"{(ys[y]-b):+.1%}" if b is not None else "  –"
        print(f"   {y:<6}{ys[y]:>+10.1%}{bs:>10}{ds:>10}")

    print("\n TOLKNING: PEAD ligger ofta delvis i kassa (se 'deployat'), så absolut")
    print(" CAGR jämförs mot en FULLINVESTERAD SPY. Edge syns bäst i Sharpe/maxDD")
    print(" och i hur snabbt courtaget äter upp överavkastningen.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
