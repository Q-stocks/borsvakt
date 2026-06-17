#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – Frekvens-backtest: lönar sig oftare rebalansering på momentum?

Samma momentumregel (sammansatt 3/6/12-mån, trendfilter, regimfilter, banding)
men VARIERAD rebalanseringsfrekvens — och olika mycket "tröghet" (banding) —
allt efter courtage. Svarar på: fångar man mer avkastning genom att kolla
oftare, eller bara mer kostnad? Och räddar banding (tröghet) vecko-varianten?

Identiska regler i alla varianter; ENDA skillnaden är hur ofta vi får agera och
hur tröga vi är att byta. Daglig mark-to-market, equal-weight topp 10.

Körning:  python backtest_frequency.py            # universe/usa.csv, SPY
"""

from __future__ import annotations

import math
import sys

import numpy as np
import pandas as pd

from backtest_pead import load_universe, daily_close

TOP_N = 10
SMA_N = 210          # ~10 mån trendfilter (handelsdagar)
COST_NET = 0.10      # courtage % per affär (en väg) för nettokolumnen
YEARS = 14

# (etikett, step i handelsdagar, banding-rank)
CONFIGS = [
    ("Månadsvis (band 20)",        21, 20),
    ("Varannan vecka (band 20)",   10, 20),
    ("Veckovis (band 20)",          5, 20),
    ("Veckovis UTAN tröghet (band 10)", 5, 10),
    ("Veckovis STARK tröghet (band 30)", 5, 30),
]


def simulate(P, R, B, Brel, step, band, cost_pct):
    cost = cost_pct / 100.0
    cols = list(P)
    N = len(B)
    start = 252
    rebal = set(range(start, N, step))
    held, eq, eqs, trades_total = [], 1.0, [], 0
    for i in range(start, N):
        gross = (sum(R[c][i] for c in held) / len(held)) if held else 0.0
        cost_today = 0.0
        if i in rebal:
            scores, trend = {}, {}
            for c in cols:
                ci = P[c][i]
                if math.isnan(ci) or ci <= 0:
                    continue
                a, b, d = P[c][i - 63], P[c][i - 126], P[c][i - 252]
                if any(math.isnan(x) or x <= 0 for x in (a, b, d)):
                    continue
                scores[c] = ((ci / a - 1) + (ci / b - 1) + (ci / d - 1)) / 3.0
                sma = np.nanmean(P[c][i - SMA_N:i])
                trend[c] = (not math.isnan(sma)) and ci > sma
            ranked = sorted(scores, key=lambda c: scores[c], reverse=True)
            rank_of = {c: k + 1 for k, c in enumerate(ranked)}
            cand = [c for c in ranked if trend.get(c)]
            keep = [c for c in held if rank_of.get(c, 10 ** 9) <= band]
            fill = [c for c in cand if c not in keep][: max(0, TOP_N - len(keep))]
            new = (keep + fill)[:TOP_N]
            sma_b = np.nanmean(B[i - SMA_N:i])
            if not math.isnan(sma_b) and B[i] < sma_b:
                new = []                      # regimfilter → kassa
            tr = len(set(held) ^ set(new))
            trades_total += tr
            cost_today = tr * cost / TOP_N
            held = new
        net = gross - cost_today
        eq *= (1 + net)
        eqs.append(eq)
    years = (N - start) / 252.0
    return np.array(eqs), trades_total / years if years else 0.0


def metrics(eqs):
    if len(eqs) < 2:
        return {}
    r = np.diff(eqs) / eqs[:-1]
    n = len(r)
    cagr = eqs[-1] ** (252.0 / n) - 1
    sd = r.std()
    sharpe = (r.mean() / sd * math.sqrt(252)) if sd > 0 else 0.0
    peak = -1e9; mdd = 0.0
    for v in eqs:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)
    return {"cagr": cagr, "vol": sd * math.sqrt(252), "sharpe": sharpe, "mdd": mdd}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    universe_file = args[0] if args else "universe/usa.csv"
    bench_sym = args[1] if len(args) > 1 else "SPY"

    bench = daily_close(bench_sym, YEARS)
    if bench is None:
        print("Ingen indexdata.", file=sys.stderr); return 1
    cal = bench.index
    print(f"Hämtar universum + {bench_sym} ({YEARS}å dagsdata) …", file=sys.stderr)
    prices = {}
    for sym in load_universe(universe_file):
        s = daily_close(sym, YEARS)
        if s is not None and len(s) > 260:
            prices[sym] = s.reindex(cal).ffill()
    P = {c: prices[c].to_numpy(dtype=float) for c in prices}
    R = {c: prices[c].pct_change().fillna(0).to_numpy(dtype=float) for c in prices}
    B = bench.to_numpy(dtype=float)
    Brel = bench.pct_change().fillna(0).to_numpy(dtype=float)

    bm = metrics(np.cumprod(1 + Brel[252:]))

    print("\n" + "=" * 84)
    print(f" FREKVENS-BACKTEST – momentum, {universe_file}  ({len(prices)} aktier, index {bench_sym})")
    print("=" * 84)
    print(" Identisk regel; enda skillnaden = hur ofta vi rebalanserar + hur trög banding är.")
    print(" Survivorship bias (dagens bolag). Courtage 'netto' = 0,10 %/affär en väg.\n")
    print(f"   {'Variant':<34}{'CAGR brutto':>12}{'CAGR netto':>12}{'affärer/år':>12}{'Sharpe':>8}{'maxDD':>8}")
    for label, step, band in CONFIGS:
        eg, _ = simulate(P, R, B, Brel, step, band, 0.0)
        en, tpy = simulate(P, R, B, Brel, step, band, COST_NET)
        mg, mn = metrics(eg), metrics(en)
        print(f"   {label:<34}{mg['cagr']:>+11.1%}{mn['cagr']:>+12.1%}"
              f"{tpy:>12.0f}{mn['sharpe']:>8.2f}{mn['mdd']:>+8.0%}")
    print(f"   {'SPY köp & behåll':<34}{bm['cagr']:>+11.1%}{bm['cagr']:>+12.1%}"
          f"{0:>12.0f}{bm['sharpe']:>8.2f}{bm['mdd']:>+8.0%}")
    print("\n TOLKNING: jämför 'CAGR netto' (efter courtage) och affärer/år. Om oftare ger")
    print(" högre brutto men lägre netto → courtaget åt upp vinsten. Banding (tröghet)")
    print(" syns som färre affärer/år vid samma frekvens.")
    print("=" * 84)
    return 0


if __name__ == "__main__":
    sys.exit(main())
