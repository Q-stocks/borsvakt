#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – Filter-backtest: vilken vakt rensar "TOBII-typer" utan att kosta?

Testar Aktiemotorns regel med olika filter på NYA köp:
  • inget (som live nu)
  • 10-mån MA-trendfilter (standard, men långsamt)
  • krav 12-mån momentum > 0 (utesluter bolag som fallit över året – TOBII-fallet)
  • krav 3/6/12 ALLA > 0 (ett studs-kvartal räcker inte)
  • + F-score-kvalitetsfilter (om data/fundamenta_sverige.csv finns)

Trendvarianter går på prisdata. F-score kräver Börsdata-export och visar RIKTNING
(snapshot = look-ahead i kvalitetsdimensionen, samma typ av bias som survivorship).

Körning:  python backtest_filters.py            # universe/sverige.csv, ^OMX
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

from backtest_pead import load_universe, daily_close

TOP_N = 10
BAND = 20
SMA_N = 210
STEP = 21
COST_NET = 0.10
YEARS = 14
QPCT = 50
QFILE = "data/fundamenta_sverige.csv"


def _ok(meta, gate):
    r3, r6, r12, trend = meta
    if gate == "trend":
        return trend
    if gate == "m12":
        return r12 > 0
    if gate == "allpos":
        return r3 > 0 and r6 > 0 and r12 > 0
    return True


def simulate(P, R, B, cols, gate, cost_pct, evict=False):
    cost = cost_pct / 100.0
    N = len(B)
    start = 252
    rebal = set(range(start, N, STEP))
    held, eq, eqs, trades = [], 1.0, [], 0
    excluded = 0  # gånger en topp-rankad aktie filtrerades bort
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
            cand = [c for c in ranked if _ok(meta[c], gate)]
            excluded += sum(1 for c in ranked[:TOP_N] if not _ok(meta[c], gate))
            keep = [c for c in held if rank_of.get(c, 10 ** 9) <= BAND
                    and (not evict or _ok(meta[c], gate))]
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
    n_rebal = len(rebal)
    return np.array(eqs), (trades / years if years else 0.0), excluded / max(1, n_rebal)


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


def fscore_keep(universe, pct):
    p = Path(QFILE)
    if not p.exists():
        return None
    from stocks import load_quality
    vals = load_quality(QFILE, "Ticker", "F-Score")
    if not vals:
        return None
    m = {raw.upper(): s for raw, s in vals.items()}   # filen har redan Yahoo-tickrar
    have = [(t, m[t.upper()]) for t in universe if t.upper() in m]
    have.sort(key=lambda x: x[1], reverse=True)
    keep_n = max(1, int(len(have) * pct / 100.0))
    return set(t for t, _ in have[:keep_n])


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    universe_file = args[0] if args else "universe/sverige.csv"
    bench_sym = args[1] if len(args) > 1 else "^OMX"

    bench = daily_close(bench_sym, YEARS)
    if bench is None:
        print("Ingen indexdata.", file=sys.stderr); return 1
    cal = bench.index
    print(f"Hämtar {universe_file} + {bench_sym} ({YEARS}å dagsdata) …", file=sys.stderr)
    prices = {}
    for sym in load_universe(universe_file):
        s = daily_close(sym, YEARS)
        if s is not None and len(s) > 260:
            prices[sym] = s.reindex(cal).ffill()
    P = {c: prices[c].to_numpy(dtype=float) for c in prices}
    R = {c: prices[c].pct_change().fillna(0).to_numpy(dtype=float) for c in prices}
    B = bench.to_numpy(dtype=float)
    Brel = bench.pct_change().fillna(0).to_numpy(dtype=float)
    allcols = list(prices)

    qkeep = fscore_keep(allcols, QPCT)
    qcols = [c for c in allcols if c in qkeep] if qkeep else None

    configs = [
        ("Som live nu (inget filter)",            allcols, "none",   False),
        ("allpos – blockera bara NYA köp",        allcols, "allpos", False),
        ("allpos + SÄLJ vid negativt (evict)",    allcols, "allpos", True),
    ]
    if qcols:
        configs += [
            (f"F-score topp{QPCT}% (utan vakt)", qcols, "none",   False),
            (f"F-score topp{QPCT}% + allpos",    qcols, "allpos", False),
            (f"F-score topp{QPCT}% + evict",     qcols, "allpos", True),
        ]

    bm = metrics(np.cumprod(1 + Brel[252:]))
    print("\n" + "=" * 86)
    print(f" FILTER-BACKTEST – Aktiemotorn, {universe_file}  ({len(allcols)} aktier, index {bench_sym})")
    print("=" * 86)
    print(f" Månadsvis, banding {BAND}, regimfilter, courtage {COST_NET:.2f}%/affär. Survivorship bias.")
    if not qcols:
        print(f" F-score-rader HOPPAS ÖVER – lägg {QFILE} (Ticker, F-Score) så körs de.")
    else:
        print(f" F-score aktivt: {len(qcols)}/{len(allcols)} aktier passerar (look-ahead-caveat).")
    print(f"\n   {'Variant':<36}{'CAGR netto':>12}{'affärer/år':>12}{'Sharpe':>8}{'maxDD':>8}{'bortfiltr/mån':>14}")
    for label, cols, gate, ev in configs:
        en, tpy, exc = simulate(P, R, B, cols, gate, COST_NET, ev)
        m = metrics(en)
        print(f"   {label:<36}{m['cagr']:>+11.1%}{tpy:>12.0f}{m['sharpe']:>8.2f}{m['mdd']:>+8.0%}{exc:>14.2f}")
    print(f"   {'^OMX köp & behåll (prisindex)':<36}{bm['cagr']:>+11.1%}{0:>12.0f}{bm['sharpe']:>8.2f}{bm['mdd']:>+8.0%}{0:>14.2f}")
    print("\n 'bortfiltr/mån' = hur många av topp-10 filtret stänger ute i snitt per månad.")
    print(" TOLKNING: en bra vakt höjer/behåller netto+Sharpe ELLER sänker maxDD, OCH")
    print(" rensar trendbrutna studsare (TOBII-typer = negativt 12m). F-score testar om")
    print(" fundamental kvalitetsgallring tillför utöver det.")
    print("=" * 86)
    return 0


if __name__ == "__main__":
    sys.exit(main())
