#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – Tak-backtest: hjälper ett ÖVRE momentumtak på NYA köp R/R?

Idé (användaren): aktier som redan stigit "för mycket" har dålig risk/reward som
NYA köp – har man dom kan man behålla dom (= banding), men köp inte in sig sent i
en parabol. Det är ett ÖVRE tak ovanpå allpos (som är en NEDRE gräns).

Vakt på nya köp = allpos (3/6/12 alla > 0)  OCH  12-mån-avkastning < TAK.
Banding behåller befintliga innehav (rank ≤ BAND) oavsett tak – ingen tvångssälj.
Taket mäts på 12-månadersavkastningen (r12); "stigit över 500 %" => r12 > 5,0.

Körning:  python backtest_cap.py [universe.csv] [bench]
          (default universe/sverige_broad.csv, ^OMX)
"""

from __future__ import annotations

import math
import sys

import numpy as np

from backtest_pead import load_universe, daily_close

TOP_N = 10
BAND = 20
SMA_N = 210
STEP = 21
COST_NET = 0.10
YEARS = 14

# Tak på r12 för NYA köp. None = nuvarande regel (bara allpos, inget tak).
CAPS = [
    ("allpos (inget tak) – som nu", None),
    ("tak +1000 %  (r12 < 10,0)",   10.0),
    ("tak +750 %   (r12 < 7,5)",     7.5),
    ("tak +500 %   (r12 < 5,0)",     5.0),
    ("tak +300 %   (r12 < 3,0)",     3.0),
    ("tak +200 %   (r12 < 2,0)",     2.0),
]


def _allpos(meta) -> bool:
    r3, r6, r12, _ = meta
    return r3 > 0 and r6 > 0 and r12 > 0


def _new_ok(meta, cap) -> bool:
    """Vakt på NYA köp: allpos + valfritt övre tak på r12."""
    if not _allpos(meta):
        return False
    if cap is not None and meta[2] >= cap:   # r12 redan över taket → hoppa nyköp
        return False
    return True


def simulate(P, R, B, cols, cap, cost_pct):
    cost = cost_pct / 100.0
    N = len(B)
    start = 252
    rebal = set(range(start, N, STEP))
    held, eq, eqs, trades = [], 1.0, [], 0
    capped = 0  # ggr ett topp-10-namn blockerades AV TAKET (men hade klarat allpos)
    for i in range(start, N):
        gross = (sum(R[c][i] for c in held) / len(held)) if held else 0.0
        ct = 0.0
        if i in rebal:
            scores, meta = {}, {}
            for c in cols:
                ci = P[c][i]
                if math.isnan(ci) or ci <= 0:
                    continue
                a, b, d = P[c][i - 63], P[c][i - 126], P[c][i - 252]
                if any(math.isnan(x) or x <= 0 for x in (a, b, d)):
                    continue
                r3, r6, r12 = ci/a-1, ci/b-1, ci/d-1
                scores[c] = (r3 + r6 + r12) / 3.0
                sma = np.nanmean(P[c][i - SMA_N:i])
                meta[c] = (r3, r6, r12, (not math.isnan(sma)) and ci > sma)
            ranked = sorted(scores, key=lambda c: scores[c], reverse=True)
            rank_of = {c: k + 1 for k, c in enumerate(ranked)}
            cand = [c for c in ranked if _new_ok(meta[c], cap)]
            if cap is not None:
                capped += sum(1 for c in ranked[:TOP_N]
                              if _allpos(meta[c]) and meta[c][2] >= cap)
            # Banding behåller befintliga (ingen evict, taket gäller EJ innehav)
            keep = [c for c in held if rank_of.get(c, 10 ** 9) <= BAND]
            fill = [c for c in cand if c not in keep][: max(0, TOP_N - len(keep))]
            new = (keep + fill)[:TOP_N]
            sma_b = np.nanmean(B[i - SMA_N:i])
            if not math.isnan(sma_b) and B[i] < sma_b:
                new = []
            tr = len(set(held) ^ set(new))
            trades += tr
            ct = tr * cost / TOP_N
            held = new
        eq *= (1 + gross - ct)
        eqs.append(eq)
    years = (N - start) / 252.0
    return np.array(eqs), (trades / years if years else 0.0), capped / max(1, len(rebal))


def metrics(eqs):
    if len(eqs) < 2:
        return {}
    r = np.diff(eqs) / eqs[:-1]
    cagr = eqs[-1] ** (252.0 / len(r)) - 1
    sd = r.std()
    sharpe = (r.mean() / sd * math.sqrt(252)) if sd > 0 else 0.0
    peak, mdd = -1e9, 0.0
    for v in eqs:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)
    return {"cagr": cagr, "sharpe": sharpe, "mdd": mdd}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    universe_file = args[0] if args else "universe/sverige_broad.csv"
    bench_sym = args[1] if len(args) > 1 else "^OMX"

    bench = daily_close(bench_sym, YEARS)
    if bench is None:
        print("Ingen indexdata.", file=sys.stderr); return 1
    cal = bench.index
    syms = load_universe(universe_file)
    print(f"Hämtar {universe_file} ({len(syms)} namn) + {bench_sym} "
          f"({YEARS}å dagsdata) …", file=sys.stderr)
    prices = {}
    for sym in syms:
        s = daily_close(sym, YEARS)
        if s is not None and len(s) > 260:
            prices[sym] = s.reindex(cal).ffill()
    P = {c: prices[c].to_numpy(dtype=float) for c in prices}
    R = {c: prices[c].pct_change().fillna(0).to_numpy(dtype=float) for c in prices}
    B = bench.to_numpy(dtype=float)
    Brel = bench.pct_change().fillna(0).to_numpy(dtype=float)
    allcols = list(prices)

    bm = metrics(np.cumprod(1 + Brel[252:]))
    print("\n" + "=" * 92)
    print(f" TAK-BACKTEST – Aktiemotorn, {universe_file}  ({len(allcols)} aktier, index {bench_sym})")
    print("=" * 92)
    print(f" Månadsvis, banding {BAND}, regimfilter, courtage {COST_NET:.2f}%/affär. Survivorship bias.")
    print(" Tak = övre gräns på r12 för NYA köp; banding behåller befintliga oavsett.")
    print(f"\n   {'Variant':<32}{'CAGR netto':>12}{'affärer/år':>12}{'Sharpe':>8}{'maxDD':>8}{'takblock/mån':>14}")
    base = None
    for label, cap in CAPS:
        en, tpy, capm = simulate(P, R, B, allcols, cap, COST_NET)
        m = metrics(en)
        if cap is None:
            base = m
        flag = ""
        if base and cap is not None:
            dc = m['cagr'] - base['cagr']
            ds = m['sharpe'] - base['sharpe']
            flag = f"  Δcagr {dc:+.1%} Δsharpe {ds:+.2f}"
        print(f"   {label:<32}{m['cagr']:>+11.1%}{tpy:>12.0f}{m['sharpe']:>8.2f}"
              f"{m['mdd']:>+8.0%}{capm:>14.2f}{flag}")
    print(f"   {bench_sym + ' köp & behåll':<32}{bm['cagr']:>+11.1%}{0:>12.0f}"
          f"{bm['sharpe']:>8.2f}{bm['mdd']:>+8.0%}{0:>14.2f}")
    print("\n 'takblock/mån' = hur många av topp-10 taket stänger ute i snitt per månad")
    print("  (namn som klarade allpos men låg över taket).")
    print(" TOLKNING: taket är värt det om Sharpe behålls/höjs och maxDD sänks utan att")
    print(" CAGR rasar. Faller CAGR mycket mer än Sharpe stiger = för snävt (klipper äkta")
    print(" vinnare, samma fälla som F-score). Survivorship-biasen drar UPP alla rader lika.")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    sys.exit(main())
