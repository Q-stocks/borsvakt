#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – Backtest (modul 12)

Kör den BESTÄMDA regeluppsättningen (samma som stocks.py: sammansatt
momentum 3/6/12 mån, topp N, banding, trend- och regimfilter) mot historik.
Detta är giltig validering – vi finjusterar INGET för att få snyggare
resultat. Men läs varningarna i utskriften: en backtest på dagens
bevakningslista är survivorship-biased (bara överlevarna finns med), så
höga tal ska tas med en stor nypa salt.

Mäter per kalenderår + totalt: avkastning, volatilitet, Sharpe, max nedgång,
andel månader som slår index, och de värsta månaderna (momentums akilleshäl).

Kräver nätåtkomst till Yahoo (yfinance) – körs lokalt, inte i sandlådan.

Körning:
  python backtest.py                      # USA-universum, 15 år
  python backtest.py universe/sverige.csv ^OMX 20
  python backtest.py --trend              # multi-tillgång trendföljning i stället
"""

from __future__ import annotations

import sys
import datetime as dt

import pandas as pd

# Standardparametrar = exakt det stocks.py kör
DEFAULTS = dict(top_n=10, band_keep=20, sma_months=10,
                trend_filter=True, regime_filter=True, cost_bps=15)


def fetch_monthly(symbols, years: int) -> pd.DataFrame:
    import yfinance as yf
    data = {}
    for s in symbols:
        try:
            h = yf.Ticker(s).history(period=f"{years}y", interval="1mo", auto_adjust=True)
            if h is not None and not h.empty:
                ser = h["Close"].dropna()
                # yfinance tidsstämplar månadsbarer vid månadsSTART i varje börs
                # egen tz -> samma månad blir olika UTC-instanser och align(inner)
                # tappar rader tyst. Normalisera till tz-naiv månadsslut-stämpel.
                ser.index = ser.index.tz_localize(None).to_period("M").to_timestamp("M")
                ser = ser[~ser.index.duplicated(keep="last")]
                data[s] = ser
        except Exception as exc:
            print(f"  {s}: {exc}", file=sys.stderr)
    df = pd.DataFrame(data)
    # Släng innevarande (ofullbordade) månad
    if len(df) and df.index[-1].to_pydatetime().month == dt.date.today().month \
            and df.index[-1].to_pydatetime().year == dt.date.today().year:
        df = df.iloc[:-1]
    return df


def simulate_momentum(df: pd.DataFrame, idx: pd.Series | None, cfg: dict) -> dict:
    """Returnerar dict med månadsavkastning för strategi och index."""
    top_n, band = cfg["top_n"], cfg["band_keep"]
    sma_n = cfg["sma_months"]
    use_trend, use_regime = cfg["trend_filter"], cfg["regime_filter"]
    cost = cfg["cost_bps"] / 10000.0

    dates = list(df.index)
    held: list[str] = []
    strat, bench = [], []

    for i in range(12, len(df) - 1):
        sub = df.iloc[: i + 1]
        scores, trend = {}, {}
        for col in df.columns:
            s = sub[col].dropna()
            if len(s) < 13 or s.iloc[-1] <= 0:
                continue
            r3 = s.iloc[-1] / s.iloc[-4] - 1
            r6 = s.iloc[-1] / s.iloc[-7] - 1
            r12 = s.iloc[-1] / s.iloc[-13] - 1
            scores[col] = (r3 + r6 + r12) / 3.0
            trend[col] = s.iloc[-1] > s.iloc[-sma_n:].mean()
        ranked = sorted(scores, key=lambda c: scores[c], reverse=True)
        rank_of = {c: k + 1 for k, c in enumerate(ranked)}
        cand = [c for c in ranked if (trend.get(c, False) or not use_trend)]

        keep = [c for c in held if rank_of.get(c, 10**9) <= band]
        fill = [c for c in cand if c not in keep][: max(0, top_n - len(keep))]
        port = (keep + fill)[:top_n]

        # Regimfilter: index under sitt 10-mån MA -> kassa
        if use_regime and idx is not None:
            isub = idx.iloc[: i + 1].dropna()
            if len(isub) >= sma_n and isub.iloc[-1] < isub.iloc[-sma_n:].mean():
                port = []

        # Realiserad avkastning nästa månad
        if port:
            rr = []
            for c in port:
                a, b = df[c].iloc[i], df[c].iloc[i + 1]
                if pd.notna(a) and pd.notna(b) and a > 0:
                    rr.append(b / a - 1)
            gross = sum(rr) / len(rr) if rr else 0.0
        else:
            gross = 0.0

        trades = len(set(held) ^ set(port))
        net = gross - trades * cost / max(1, top_n)
        strat.append((dates[i + 1], net))
        if idx is not None:
            a, b = idx.iloc[i], idx.iloc[i + 1]
            bench.append((dates[i + 1], (b / a - 1) if (pd.notna(a) and pd.notna(b) and a > 0) else 0.0))
        held = port

    return {"strat": strat, "bench": bench}


def simulate_trend(df: pd.DataFrame, cfg: dict) -> dict:
    """Multi-tillgång trendföljning: håll likaviktat de tillgångar som ligger
    över sitt 10-mån MA, annars kassa (0 % på den delen)."""
    sma_n = cfg["sma_months"]
    dates = list(df.index)
    strat = []
    for i in range(sma_n, len(df) - 1):
        sub = df.iloc[: i + 1]
        on = [c for c in df.columns
              if sub[c].dropna().shape[0] >= sma_n and sub[c].iloc[-1] > sub[c].iloc[-sma_n:].mean()]
        if on:
            rr = []
            for c in on:
                a, b = df[c].iloc[i], df[c].iloc[i + 1]
                if pd.notna(a) and pd.notna(b) and a > 0:
                    rr.append(b / a - 1)
            # likaviktat över de tillgångar som FINNS (har nog historik); de
            # som är "av" bidrar 0 (kassa). Räkna inte ännu icke-existerande
            # tillgångar som kassa i nämnaren.
            investable = [c for c in df.columns if sub[c].dropna().shape[0] >= sma_n]
            gross = sum(rr) / len(investable) if investable else 0.0
        else:
            gross = 0.0
        strat.append((dates[i + 1], gross))
    return {"strat": strat, "bench": []}


# ----------------------------------------------------------------------
# Mått
# ----------------------------------------------------------------------

def metrics(series: list[tuple]) -> dict:
    if not series:
        return {}
    rets = [r for _, r in series]
    n = len(rets)
    cum = 1.0
    eq = []
    for r in rets:
        cum *= (1 + r)
        eq.append(cum)
    # cum <= 0 (total förlust) ger annars ett komplext tal som kraschar report()
    cagr = (cum ** (12.0 / n) - 1) if cum > 0 else -1.0
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / max(1, n - 1)
    sd = var ** 0.5
    sharpe = (mean / sd * (12 ** 0.5)) if sd > 0 else 0.0
    peak, mdd = -1e9, 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"cagr": cagr, "vol": sd * (12 ** 0.5), "sharpe": sharpe,
            "mdd": mdd, "total": cum - 1, "months": n}


def yearly(series: list[tuple]) -> dict:
    by_year: dict[int, float] = {}
    for d, r in series:
        y = d.year
        by_year[y] = by_year.get(y, 1.0) * (1 + r)
    return {y: v - 1 for y, v in by_year.items()}


def hit_rate(strat, bench) -> float | None:
    if not bench:
        return None
    bd = {d: r for d, r in bench}
    wins = sum(1 for d, r in strat if d in bd and r > bd[d])
    return 100.0 * wins / len(strat)


def worst_months(series, k=5):
    return sorted(series, key=lambda x: x[1])[:k]


def report(res: dict, label: str):
    strat, bench = res["strat"], res["bench"]
    ms, mb = metrics(strat), metrics(bench)
    ys, yb = yearly(strat), yearly(bench)

    print("\n" + "=" * 64)
    print(f" BACKTEST: {label}")
    print("=" * 64)
    print("\n VARNINGAR (läs dessa):")
    print(" • Universumet = dagens bolag → SURVIVORSHIP BIAS. Konkursade och")
    print("   avnoterade bolag saknas, vilket gör resultaten för OPTIMISTISKA.")
    print("   För en ren test: använd point-in-time-listor eller Börsdata (har")
    print("   avnoterade bolag).")
    print(" • Historik är inte framtid. Detta validerar regeln, garanterar inget.")
    print(" • Momentum kraschar vid vändpunkter (se värsta månaderna nedan).")

    print(f"\n {'År':<6}{'Strategi':>12}{'Index':>12}{'Diff':>12}")
    for y in sorted(ys):
        s = ys[y]
        b = yb.get(y)
        bs = f"{b:+.1%}" if b is not None else "  –"
        ds = f"{(s-b):+.1%}" if b is not None else "  –"
        print(f" {y:<6}{s:>+12.1%}{bs:>12}{ds:>12}")

    def line(name, m):
        if not m:
            return
        print(f" {name:<22}{m['cagr']:>+8.1%}/år  vol {m['vol']:.0%}  "
              f"Sharpe {m['sharpe']:.2f}  maxDD {m['mdd']:.0%}  ({m['months']} mån)")
    print("\n SAMMANFATTNING")
    line("Strategi", ms)
    line("Index (buy & hold)", mb)
    hr = hit_rate(strat, bench)
    if hr is not None:
        print(f" Andel månader som slår index: {hr:.0f} %")
    print("\n Värsta månaderna (momentums akilleshäl):")
    for d, r in worst_months(strat):
        print(f"   {d.date()}  {r:+.1%}")
    print("=" * 64)


def main() -> int:
    # Utskrifterna använder Unicode (→, •) -> krascha inte på Windows cp1252
    import io  # noqa: F401
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    cfg = dict(DEFAULTS)

    if "--trend" in flags:
        # Standardkorg från trend-modulens idé (verifiera tickers)
        assets = ["EUNL.DE", "SXR8.DE", "IS3N.DE", "4GLD.DE"]
        years = int(args[0]) if args else 15
        print(f"Hämtar {len(assets)} tillgångar, {years} år …")
        df = fetch_monthly(assets, years)
        if df.shape[1] == 0:
            print("Ingen data hämtad (kör lokalt med nätåtkomst).", file=sys.stderr)
            return 1
        report(simulate_trend(df, cfg), f"Trendföljning, {list(df.columns)}")
        return 0

    universe = args[0] if args else "universe/usa.csv"
    index_sym = args[1] if len(args) > 1 else "SXR8.DE"
    years = int(args[2]) if len(args) > 2 else 15

    from pathlib import Path
    import csv
    rows = []
    p = Path(universe)
    if not p.exists():
        print(f"Hittar inte {universe}", file=sys.stderr)
        return 1
    with open(p, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            low = {k.lower().strip(): (v or "").strip() for k, v in r.items() if k}
            if low.get("ticker"):
                rows.append(low["ticker"])
    print(f"Hämtar {len(rows)} aktier + index {index_sym}, {years} år … (kan ta en stund)")
    df = fetch_monthly(rows, years)
    idx = fetch_monthly([index_sym], years)
    if df.shape[1] == 0:
        print("Ingen kursdata (kör lokalt med Yahoo-åtkomst).", file=sys.stderr)
        return 1
    idx_series = idx[index_sym] if index_sym in idx.columns else None
    if idx_series is not None:
        df, idx_series = df.align(idx_series, axis=0, join="inner")
    report(simulate_momentum(df, idx_series, cfg),
           f"Momentum {universe} (topp {cfg['top_n']}, banding {cfg['band_keep']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
