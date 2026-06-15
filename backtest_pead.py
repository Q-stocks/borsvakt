#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – PEAD-backtest (event-study)

Den ordinarie backtest.py simulerar bara momentum/trend. PEAD är eventdriven,
så den valideras bäst som en EVENT-STUDY, inte en månadsvis equity-kurva:

  1. Hämta alla historiska rapporttillfällen (yfinance get_earnings_dates ger
     ~50 kvartal bakåt till ~2014 med Surprise%) för USA-universumet.
  2. Kvalificera ett event om vinstöverraskning >= surprise_min ELLER onormal
     rapportdagsreaktion mot index >= reaction_min  (samma OR-grind som pead.py).
  3. Gå in vid stängning DAGEN EFTER rapporten (jaga inte själva hoppet),
     mät framåtblickande avkastning mot index (SPY, total return) över
     20 / 60 handelsdagar (~kvartalet/driftfönstret).
  4. Aggregera: antal event, snittavkastning, snitt-överavkastning mot index,
     träffprocent. Jämför mot en BASLINJE (alla rapporter, ofiltrerat) och mot
     NEGATIVA överraskningar – så man ser om edgen är äkta eller bara marknadsdrift.

Ärliga begränsningar (skrivs ut):
  • Surprise% från yfinance är en NULÄGES-snapshot av konsensus, inte strikt
    point-in-time → mild look-ahead i estimaten.
  • Universumet = dagens bolag → survivorship bias (optimistiskt).
  • Inga handelskostnader/slippage; long-only; överlappande positioner.

Körning:  python backtest_pead.py            # universe/usa.csv, SPY
          python backtest_pead.py universe/usa.csv SPY
"""

from __future__ import annotations

import csv
import math
import sys
import datetime as dt
from pathlib import Path

import pandas as pd

HORIZONS = (20, 60)          # handelsdagar framåt
SURPRISE_MIN = 5.0           # % vinstöverraskning
REACTION_MIN = 5.0           # % onormal rapportdagsreaktion mot index
ROOT = Path(__file__).resolve().parent


def _num(x):
    try:
        if x is None:
            return None
        x = float(x)
        return None if math.isnan(x) else x
    except (ValueError, TypeError):
        return None


def load_universe(path: str) -> list[str]:
    p = Path(path) if Path(path).is_absolute() else ROOT / path
    out = []
    with open(p, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            low = {k.lower().strip(): (v or "").strip() for k, v in r.items() if k}
            if low.get("ticker"):
                out.append(low["ticker"])
    return out


def daily_close(symbol: str, years: int = 13):
    import yfinance as yf
    h = yf.Ticker(symbol).history(period=f"{years}y", interval="1d", auto_adjust=True)
    if h is None or h.empty:
        return None
    s = h["Close"].dropna()
    s.index = s.index.tz_localize(None).normalize()   # tz-naiv, dagsupplöst (midnatt)
    return s[~s.index.duplicated(keep="last")]


def earnings_events(symbol: str) -> list[tuple[dt.date, float | None]]:
    import yfinance as yf
    try:
        ed = yf.Ticker(symbol).get_earnings_dates(limit=50)
    except Exception as exc:
        print(f"  {symbol}: earnings-fel: {exc}", file=sys.stderr)
        return []
    if ed is None or ed.empty:
        return []
    out = []
    for idx, row in ed.iterrows():
        d = idx.date() if hasattr(idx, "date") else None
        if d is None:
            continue
        out.append((d, _num(row.get("Surprise(%)"))))
    return out


def forward(prices: pd.Series, bench: pd.Series, ev_date: dt.date,
            horizon: int) -> tuple[float, float] | None:
    """(aktieavkastning %, indexavkastning %) från stängning DAGEN EFTER
    rapport (t0) till t0+horizon handelsdagar. None om ej moget."""
    dates = [d.date() for d in prices.index]
    after = [i for i, d in enumerate(dates) if d > ev_date]   # strikt efter rapportdagen
    if not after:
        return None
    t0 = after[0]
    if t0 + horizon >= len(prices):
        return None
    p0, p1 = float(prices.iloc[t0]), float(prices.iloc[t0 + horizon])
    if p0 <= 0:
        return None
    sret = (p1 / p0 - 1.0) * 100.0
    d0, d1 = dates[t0], dates[t0 + horizon]
    bret = 0.0
    bsub = bench.reindex(prices.index, method="ffill")
    b0, b1 = bsub.iloc[t0], bsub.iloc[t0 + horizon]
    if pd.notna(b0) and pd.notna(b1) and float(b0) > 0:
        bret = (float(b1) / float(b0) - 1.0) * 100.0
    return sret, bret


def reaction(prices: pd.Series, bench: pd.Series, ev_date: dt.date) -> float | None:
    """Onormal rapportdagsreaktion (aktie minus index), stängning dagen före
    rapport till stängning dagen efter."""
    dates = [d.date() for d in prices.index]
    after = [i for i, d in enumerate(dates) if d >= ev_date]
    if not after or after[0] == 0:
        return None
    j = after[0]
    k = min(j + 1, len(prices) - 1)
    sret = (float(prices.iloc[k]) / float(prices.iloc[j - 1]) - 1.0) * 100.0
    bsub = bench.reindex(prices.index, method="ffill")
    bret = 0.0
    if pd.notna(bsub.iloc[k]) and pd.notna(bsub.iloc[j - 1]) and float(bsub.iloc[j - 1]) > 0:
        bret = (float(bsub.iloc[k]) / float(bsub.iloc[j - 1]) - 1.0) * 100.0
    return sret - bret


def agg(rows: list[dict], horizon: int) -> dict | None:
    sub = [r for r in rows if r["horizon"] == horizon]
    if not sub:
        return None
    ex = [r["excess"] for r in sub]
    rt = [r["ret"] for r in sub]
    wins = sum(1 for e in ex if e > 0)
    ex_sorted = sorted(ex)
    med = ex_sorted[len(ex_sorted) // 2]
    return {"n": len(sub), "hit": 100.0 * wins / len(sub),
            "avg_ex": sum(ex) / len(ex), "med_ex": med,
            "avg_ret": sum(rt) / len(rt)}


def collect(universe: list[str], bench_sym: str) -> dict:
    print(f"Hämtar {len(universe)} aktier + index {bench_sym} (dagsdata + rapporter) …",
          file=sys.stderr)
    bench = daily_close(bench_sym)
    if bench is None:
        print("Ingen indexdata.", file=sys.stderr)
        return {}
    qualified, baseline, negative = [], [], []
    for sym in universe:
        prices = daily_close(sym)
        if prices is None or len(prices) < 80:
            continue
        for ev_date, surprise in earnings_events(sym):
            react = reaction(prices, bench, ev_date)
            ok_surprise = surprise is not None and surprise >= SURPRISE_MIN
            ok_reaction = react is not None and react >= REACTION_MIN
            neg = surprise is not None and surprise <= -SURPRISE_MIN
            for h in HORIZONS:
                fr = forward(prices, bench, ev_date, h)
                if fr is None:
                    continue
                sret, bret = fr
                rec = {"sym": sym, "date": ev_date, "horizon": h,
                       "ret": sret, "bench": bret, "excess": sret - bret,
                       "surprise": surprise, "reaction": react}
                baseline.append(rec)
                if ok_surprise or ok_reaction:
                    qualified.append(rec)
                if neg:
                    negative.append(rec)
    return {"qualified": qualified, "baseline": baseline, "negative": negative}


def report(data: dict, bench_sym: str):
    print("\n" + "=" * 70)
    print(" PEAD BACKTEST (event-study) – USA")
    print("=" * 70)
    print("\n VARNINGAR (läs dessa):")
    print(" • Surprise% från yfinance är en nuläges-snapshot av konsensus →")
    print("   mild look-ahead i estimaten (ej strikt point-in-time).")
    print(" • Universum = dagens bolag → SURVIVORSHIP BIAS (optimistiskt).")
    print(" • Inga handelskostnader/slippage; long-only; överlappande positioner.")
    print(f" • Index = {bench_sym} (total return). Inträde: stängning DAGEN EFTER rapport.")

    def block(title, rows, ref=None):
        print(f"\n {title}")
        if not rows:
            print("   (inga event)")
            return
        for h in HORIZONS:
            a = agg(rows, h)
            if not a:
                continue
            extra = ""
            if ref is not None:
                ra = agg(ref, h)
                if ra:
                    extra = f"   [baslinje överavk {ra['avg_ex']:+.2f}%]"
            print(f"   {h:>2}d: n={a['n']:<5} snittavk {a['avg_ret']:+.2f}%  "
                  f"överavk {a['avg_ex']:+.2f}% (median {a['med_ex']:+.2f}%)  "
                  f"träff {a['hit']:.0f}%{extra}")

    base = data.get("baseline", [])
    block(f"KVALIFICERADE event (överraskning >= {SURPRISE_MIN:.0f}% ELLER reaktion >= {REACTION_MIN:.0f}%):",
          data.get("qualified", []), ref=base)
    block("BASLINJE (ALLA rapporter, ofiltrerat) – marknadens egen drift:", base)
    block(f"NEGATIVA överraskningar (<= -{SURPRISE_MIN:.0f}%) – ska driva NER om PEAD är äkta:",
          data.get("negative", []), ref=base)

    print("\n TOLKNING: PEAD har en edge om KVALIFICERADE event har högre")
    print(" överavkastning än BASLINJEN, och NEGATIVA event underpresterar.")
    print("=" * 70)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    universe_file = args[0] if args else "universe/usa.csv"
    bench_sym = args[1] if len(args) > 1 else "SPY"
    universe = load_universe(universe_file)
    data = collect(universe, bench_sym)
    if not data:
        print("Ingen data (kör lokalt med Yahoo-åtkomst).", file=sys.stderr)
        return 1
    report(data, bench_sym)
    return 0


if __name__ == "__main__":
    sys.exit(main())
