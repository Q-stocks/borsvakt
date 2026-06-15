#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – Breakout-backtest (Qullamaggie-mekaniserad) MED courtage

Testar om "tidigt insteg" via utbrott faktiskt slår det sena momentum vi redan
validerat – innan vi bygger något live-larm. Spec (CLAUDE.md TODO #8):

  SETUP (per aktie, dagsdata):
    • Föregående ben: +30 % under ~60 dagar in i basen.
    • Bas/konsolidering: senaste 10 dagars range < 15 % OCH < halva benet.
    • UTBROTT: stängning över basens högsta, på RVOL >= 2 (volym >= 2x 20d-snitt),
      och en uppdag.
  ENTRY: stängning utbrottsdagen.  STOP: utbrottsdagens lägsta.
  EXIT:  stängning under trailing-stop = max(initialstop, 20-dagars MA)
         → "låt vinnaren löpa så länge trenden håller, kapa snabbt om den failar".

Körs som likaviktad slot-portfölj (max 10 samtidiga), daglig mark-to-market mot
SPY, courtage per affär på flera nivåer.

ÄRLIGA BEGRÄNSNINGAR (skrivs ut):
  • Survivorship bias slår EXTRA hårt på breakouts – dagens bolag hade många rena
    utbrott; de failade utbrotten i avnoterade bolag saknas → klart optimistiskt.
  • Dagsdata, inte intraday: riktig metod agerar på intradagsutbrottet; här entry
    på stängning. Grövre approximation.
  • Ingen slippage/spread utöver courtage; long-only.

Körning:  python backtest_breakout.py            # universe/usa.csv, SPY
"""

from __future__ import annotations

import sys
from collections import defaultdict

import pandas as pd

from backtest_pead import load_universe   # återanvänd CSV-läsaren

RUN_UP_MIN = 0.30        # +30 % ben in i basen
BASE_DAYS = 10           # tight bas-fönster
BASE_RANGE_MAX = 0.15    # < 15 % range i basen
RVOL_MIN = 2.0           # volym >= 2x 20d-snitt
MAX_POS = 10
HOLD_CAP = 250           # säkerhetstak (handelsdagar)
COST_LEVELS = [0.0, 0.05, 0.10, 0.25]   # courtage % per affär (en väg)


def fetch_ohlcv(symbol: str, years: int = 13):
    import yfinance as yf
    h = yf.Ticker(symbol).history(period=f"{years}y", interval="1d", auto_adjust=True)
    if h is None or h.empty:
        return None
    df = h[["Close", "High", "Low", "Volume"]].dropna()
    df.index = df.index.tz_localize(None).normalize()
    return df[~df.index.duplicated(keep="last")]


def detect(df: pd.DataFrame) -> list[dict]:
    c = df["Close"].to_numpy(float)
    hi = df["High"].to_numpy(float)
    lo = df["Low"].to_numpy(float)
    vol = df["Volume"].to_numpy(float)
    sma20 = df["Close"].rolling(20).mean().to_numpy(float)
    dates = [d.date() for d in df.index]
    n = len(c)
    trades, t = [], 75
    while t < n:
        avgvol = vol[t - 20:t].mean()
        base_hi = hi[t - BASE_DAYS:t].max()
        base_lo = lo[t - BASE_DAYS:t].min()
        rng = (base_hi - base_lo) / base_lo if base_lo > 0 else 9.9
        prior_lo = lo[t - 70:t - BASE_DAYS].min()
        prior_move = c[t - BASE_DAYS] / prior_lo - 1 if prior_lo > 0 else 0.0
        ok = (avgvol > 0 and vol[t] >= RVOL_MIN * avgvol and c[t] > c[t - 1]
              and c[t] > base_hi and rng < BASE_RANGE_MAX
              and prior_move >= RUN_UP_MIN and rng < 0.5 * prior_move)
        if not ok:
            t += 1
            continue
        entry, stop0 = c[t], lo[t]
        j = t + 1
        while j < n and (j - t) < HOLD_CAP:
            stop = stop0 if sma20[j] != sma20[j] else max(stop0, sma20[j])
            if c[j] < stop:
                break
            j += 1
        xi = min(j, n - 1)
        trades.append({"entry_date": dates[t], "exit_date": dates[xi],
                       "entry": entry, "exit": c[xi], "ret": c[xi] / entry - 1.0,
                       "rvol": vol[t] / avgvol, "hold": xi - t})
        t = xi + 1          # inga överlappande positioner i samma aktie
    return trades


def trade_stats(trades: list[dict]) -> dict:
    if not trades:
        return {}
    rets = [x["ret"] for x in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    return {"n": len(trades), "win": 100.0 * len(wins) / len(trades),
            "avg_win": sum(wins) / len(wins) if wins else 0.0,
            "avg_loss": sum(losses) / len(losses) if losses else 0.0,
            "exp": sum(rets) / len(rets),
            "hold": sum(x["hold"] for x in trades) / len(trades)}


def simulate(all_trades, prices, bench, cost_pct, max_pos):
    cost = cost_pct / 100.0
    cal = bench.index
    caldates = [d.date() for d in cal]
    di = {d: i for i, d in enumerate(caldates)}
    N = len(cal)
    rets = {s: prices[s].reindex(cal).ffill().pct_change().fillna(0.0).to_numpy()
            for s in prices}

    entries_by_i = defaultdict(list)
    first_i = N
    for sym, tr in all_trades.items():
        for x in tr:
            ei = di.get(x["entry_date"]) or next((di[d] for d in caldates if d >= x["entry_date"]), None)
            xi = di.get(x["exit_date"]) or next((di[d] for d in caldates if d >= x["exit_date"]), None)
            if ei is None or xi is None or xi <= ei:
                continue
            entries_by_i[ei].append({"sym": sym, "exit_i": xi, "strength": x["rvol"]})
            first_i = min(first_i, ei)
    if first_i >= N:
        return None

    cash, positions, eq, deployed, n_tr = 1.0, [], [0.0] * N, [], 0
    for i in range(first_i, N):
        for p in positions:
            p["value"] *= (1.0 + rets[p["sym"]][i])
        keep = []
        for p in positions:
            if p["exit_i"] <= i:
                cash += p["value"] * (1.0 - cost); n_tr += 1
            else:
                keep.append(p)
        positions = keep
        for ev in sorted(entries_by_i.get(i, []), key=lambda e: e["strength"], reverse=True):
            if len(positions) >= max_pos or cash <= 1e-9:
                break
            equity = cash + sum(p["value"] for p in positions)
            size = min(equity / max_pos, cash)
            if size <= 1e-9:
                break
            cash -= size
            positions.append({"sym": ev["sym"], "value": size * (1.0 - cost), "exit_i": ev["exit_i"]})
            n_tr += 1
        inv = sum(p["value"] for p in positions)
        eq[i] = cash + inv
        deployed.append(inv / eq[i] if eq[i] > 0 else 0.0)

    span = range(first_i, N)
    equity = pd.Series([eq[i] for i in span], index=[cal[i] for i in span])
    return {"equity": equity, "deployed": sum(deployed) / len(deployed) if deployed else 0.0,
            "n_trades": n_tr}


def metrics(equity: pd.Series) -> dict:
    r = equity.pct_change().dropna()
    n = len(r)
    if n < 2:
        return {}
    total = float(equity.iloc[-1] / equity.iloc[0])
    cagr = total ** (252.0 / n) - 1
    sd = float(r.std())
    sharpe = (float(r.mean()) / sd * (252 ** 0.5)) if sd > 0 else 0.0
    peak, mdd = -1e9, 0.0
    for v in equity.values:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)
    return {"cagr": cagr, "vol": sd * (252 ** 0.5), "sharpe": sharpe, "mdd": mdd, "total": total - 1}


def yearly(equity: pd.Series) -> dict:
    r = equity.pct_change().dropna()
    by = {}
    for d, x in r.items():
        by[d.year] = by.get(d.year, 1.0) * (1 + x)
    return {y: v - 1 for y, v in by.items()}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    universe_file = args[0] if args else "universe/usa.csv"
    bench_sym = args[1] if len(args) > 1 else "SPY"

    bench_df = fetch_ohlcv(bench_sym)
    if bench_df is None:
        print("Ingen indexdata.", file=sys.stderr); return 1
    bench = bench_df["Close"]
    universe = load_universe(universe_file)
    print(f"Hämtar {len(universe)} aktier + {bench_sym} (dagsdata) …", file=sys.stderr)

    prices, all_trades, flat = {}, {}, []
    for sym in universe:
        df = fetch_ohlcv(sym)
        if df is None or len(df) < 120:
            continue
        prices[sym] = df["Close"]
        tr = detect(df)
        if tr:
            all_trades[sym] = tr
            flat.extend(tr)

    ts = trade_stats(flat)
    runs = {c: simulate(all_trades, prices, bench, c, MAX_POS) for c in COST_LEVELS}
    base = runs[COST_LEVELS[0]]
    if base is None:
        print("Inga utbrott hittade att simulera.", file=sys.stderr); return 1

    span = base["equity"].index
    spy = bench.reindex(span).ffill(); spy = spy / spy.iloc[0]
    ms = metrics(spy)

    print("\n" + "=" * 74)
    print(" BREAKOUT-BACKTEST (Qullamaggie-mekaniserad) – USA")
    print("=" * 74)
    print(" VARNINGAR: survivorship bias slår EXTRA på breakouts (failade utbrott i")
    print(" döda bolag saknas → optimistiskt); dagsdata ej intraday; entry på")
    print(" utbrottsstängning; ingen slippage utöver courtage; long-only.")
    print(f" Period: {span[0].date()} .. {span[-1].date()}  ·  index = {bench_sym} (TR)")

    print(f"\n SETUP-STATISTIK (per affär): {ts['n']} utbrott  ·  träff {ts['win']:.0f}%")
    print(f"   snittvinst {ts['avg_win']:+.1%}  ·  snittförlust {ts['avg_loss']:+.1%}  ·  "
          f"förväntat utfall {ts['exp']:+.1%}/affär  ·  snitt-håll {ts['hold']:.0f} dagar")

    print("\n PORTFÖLJ per courtagenivå (likaviktat 10 platser):")
    print(f"   {'courtage':>9} {'CAGR':>8} {'vol':>6} {'Sharpe':>7} {'maxDD':>7} {'totalt':>9}")
    for c in COST_LEVELS:
        m = metrics(runs[c]["equity"]) if runs[c] else {}
        if m:
            print(f"   {c:>7.2f} % {m['cagr']:>+7.1%} {m['vol']:>6.0%} "
                  f"{m['sharpe']:>7.2f} {m['mdd']:>+7.0%} {m['total']:>+9.0%}")
    print(f"   {'SPY B&H':>9} {ms['cagr']:>+7.1%} {ms['vol']:>6.0%} "
          f"{ms['sharpe']:>7.2f} {ms['mdd']:>+7.0%} {ms['total']:>+9.0%}")
    print(f"   (jmf: sent månadsmomentum gav ~+21–23 %/år i våra tidigare körningar)")
    print(f"\n Snittandel kapital deployat: {base['deployed']*100:.0f} %  ·  affärer: {base['n_trades']}")

    real = runs[0.10] or base
    ys, yb = yearly(real["equity"]), yearly(spy)
    print(f"\n Årlig avkastning (courtage 0,10 %)  vs  {bench_sym}:")
    print(f"   {'År':<6}{'Breakout':>11}{'SPY':>10}{'Diff':>10}")
    for y in sorted(ys):
        b = yb.get(y)
        bs = f"{b:+.1%}" if b is not None else "  –"
        ds = f"{(ys[y]-b):+.1%}" if b is not None else "  –"
        print(f"   {y:<6}{ys[y]:>+11.1%}{bs:>10}{ds:>10}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
