#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – Stopp-backtest: lönar det sig att AGERA på nedsidesvaktens larm?

exits.py larmar dagligen när ett innehav bryter MA50/MA200 eller fallit ≥20 %
från 60-dagars topp – men larmen har hittills bara varit information. Frågan
det här skriptet svarar på: hade en MEKANISK regel ("bryter MA50 → sälj,
återinvestera vid nästa månadsrebalans") gjort Aktiemotorn bättre eller sämre?

Bakgrund (2026-08): juli var brutal för momentumkorgen – SIVE bröt MA50 den
30 juni och föll 47 % DÄREFTER, NOKIA −28 %, QCOM −17 %, medan månadskärnan
höll kvar positionerna i upp till fyra veckor. Fem observationer är dock
ingen evidens; det här är 14 år.

MODELL (medvetet konservativ, speglar hur systemet faktiskt körs):
  • Larmet bedöms på en AVSLUTAD dagsstängning (samma regel som drop_live_bar).
  • Affären sker dagen EFTER larmet, till stängning – daily.yml kör på natten,
    du hinner inte handla på larmdagens stängning.
  • Sålt innehav blir KASSA (0 % ränta) till nästa månadsrebalans. Motorn köper
    aldrig mellan rebalanser, så positionen står tom – det är hela kostnaden
    för skyddet och den ska inte trollas bort.
  • Aktien får normalt köpas tillbaka vid nästa rebalans om den fortfarande
    rankar. "Karantän"-varianten testar spärr en månad.
  • Courtage tas ut både på stoppförsäljningen och på återköpet.

Survivorship bias: universumfilerna är dagens listor (samma caveat som alla
andra backtester i repot). Kassan förräntas inte, vilket underskattar stoppet
något i högränteperioder – och överskattar inget.

Körning:
  python backtest_exits.py                                  # sverige.csv, ^OMX
  python backtest_exits.py universe/sverige_broad.csv ^OMX
  python backtest_exits.py universe/usa.csv SPY
"""

from __future__ import annotations

import math
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_pead import load_universe

ROOT = Path(__file__).resolve().parent
TOP_N = 10
BAND = 20
SMA_N = 210          # regimfilter: ~10 månader i handelsdagar
STEP = 21            # rebalans ~1 gång/månad
COST_NET = 0.10      # courtage per affär, %
YEARS = 14
CACHE = ROOT / ".cache_backtest_exits"


# ----------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------

def fetch(symbols: list[str], bench: str, years: int) -> dict:
    """Batchad hämtning med lokal cache (yfinance en-symbol-i-taget tar 15 min
    på 326 namn; batchen tar ~1 min och cachen gör omkörningar gratis)."""
    import yfinance as yf

    CACHE.mkdir(exist_ok=True)
    # hashlib, INTE hash(): strängars hash saltas per process (PYTHONHASHSEED)
    # så en hash()-baserad nyckel hade missat cachen vid varje ny körning.
    import hashlib
    sig = hashlib.sha1(("|".join(sorted(symbols)) + f"|{bench}|{years}")
                       .encode("utf-8")).hexdigest()[:16]
    key = CACHE / f"{sig}.pkl"
    if key.exists():
        print(f"  (cache: {key.name})", file=sys.stderr)
        return pickle.loads(key.read_bytes())

    want = sorted(set(symbols) | {bench})
    out = {}
    for i in range(0, len(want), 60):
        chunk = want[i:i + 60]
        print(f"  hämtar {i + 1}–{i + len(chunk)} av {len(want)} …", file=sys.stderr)
        d = yf.download(chunk, period=f"{years}y", interval="1d", auto_adjust=True,
                        progress=False, threads=True)
        if d is None or d.empty:
            continue
        close = d["Close"] if isinstance(d.columns, pd.MultiIndex) else d[["Close"]]
        if not isinstance(d.columns, pd.MultiIndex):
            close.columns = chunk
        for c in close.columns:
            s = close[c].dropna()
            if len(s) > 260:
                s.index = s.index.tz_localize(None).normalize()
                out[c] = s[~s.index.duplicated(keep="last")]
    key.write_bytes(pickle.dumps(out))
    return out


def rolling_mean(a: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(a).rolling(n, min_periods=n).mean().to_numpy()


def rolling_max(a: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(a).rolling(n, min_periods=n).max().to_numpy()


# ----------------------------------------------------------------------
# Stoppregler – exakt de tre nivåer exits.py redan larmar på
# ----------------------------------------------------------------------

class Stop:
    """triggered(c, i) = True om innehavet c larmar på dagsstängning i."""

    def __init__(self, label, kind, dd_pct=20.0, confirm=1, quarantine=False):
        self.label, self.kind = label, kind
        self.dd_pct, self.confirm, self.quarantine = dd_pct, confirm, quarantine
        self.ma50 = self.ma200 = self.hi60 = None

    def prepare(self, P):
        if self.kind in ("ma50", "ma200"):
            n = 50 if self.kind == "ma50" else 200
            tgt = "ma50" if self.kind == "ma50" else "ma200"
            setattr(self, tgt, {c: rolling_mean(P[c], n) for c in P})
        elif self.kind == "dd":
            self.hi60 = {c: rolling_max(P[c], 60) for c in P}

    def _below(self, P, c, i) -> bool:
        if self.kind == "ma50":
            m = self.ma50[c][i]
            return (not math.isnan(m)) and P[c][i] < m
        if self.kind == "ma200":
            m = self.ma200[c][i]
            return (not math.isnan(m)) and P[c][i] < m
        h = self.hi60[c][i]
        return (not math.isnan(h)) and h > 0 and (P[c][i] / h - 1.0) * 100.0 <= -self.dd_pct

    def triggered(self, P, c, i) -> bool:
        # `confirm` = antal stängningar i rad som krävs (2 = filtrera bort
        # endagsdippen under linjen, priset är att man agerar en dag senare).
        return all(self._below(P, c, i - k) for k in range(self.confirm))


# ----------------------------------------------------------------------
# Simulering
# ----------------------------------------------------------------------

def simulate(P, R, B, cols, stop: Stop | None, cost_pct=COST_NET, gate="allpos"):
    cost = cost_pct / 100.0
    N = len(B)
    start = 252
    rebal = set(range(start, N, STEP))
    held: list[str] = []
    pending: set[str] = set()          # larmade igår -> säljs på dagens stängning
    banned: dict[str, int] = {}        # karantän: ticker -> rebalansnummer
    eq, eqs = 1.0, []
    trades = stops = 0
    cash_days = 0.0
    n_rebal = 0

    for i in range(start, N):
        # 1) Dagens avkastning: likaviktade slots, tomma slots = kassa (0 %).
        gross = sum(R[c][i] for c in held) / TOP_N if held else 0.0
        ct = 0.0

        # 2) Gårdagens larm verkställs på dagens stängning.
        if pending:
            sold = [c for c in held if c in pending]
            if sold:
                held = [c for c in held if c not in pending]
                trades += len(sold)
                stops += len(sold)
                ct += len(sold) * cost / TOP_N
                if stop is not None and stop.quarantine:
                    for c in sold:
                        banned[c] = n_rebal + 1
            pending = set()

        # 3) Månadsrebalans (samma regler som stocks.py: score, banding, regim).
        if i in rebal:
            n_rebal += 1
            scores, meta = {}, {}
            for c in cols:
                ci = P[c][i]
                if math.isnan(ci) or ci <= 0:
                    continue
                a, b, d = P[c][i - 63], P[c][i - 126], P[c][i - 252]
                if any(math.isnan(x) or x <= 0 for x in (a, b, d)):
                    continue
                r3, r6, r12 = ci / a - 1, ci / b - 1, ci / d - 1
                scores[c] = (r3 + r6 + r12) / 3.0
                meta[c] = (r3, r6, r12)
            ranked = sorted(scores, key=lambda c: scores[c], reverse=True)
            rank_of = {c: k + 1 for k, c in enumerate(ranked)}
            passes = (lambda c: all(x > 0 for x in meta[c])) if gate == "allpos" else (lambda c: True)
            keep = [c for c in held if rank_of.get(c, 10 ** 9) <= BAND]
            cand = [c for c in ranked
                    if c not in keep and passes(c) and banned.get(c, -1) < n_rebal]
            new = (keep + cand[: max(0, TOP_N - len(keep))])[:TOP_N]
            sma_b = np.nanmean(B[i - SMA_N:i])
            if not math.isnan(sma_b) and B[i] < sma_b:
                new = []
            tr = len(set(held) ^ set(new))
            trades += tr
            ct += tr * cost / TOP_N
            held = new

        # 4) Stoppkoll på dagens AVSLUTADE stängning -> säljs imorgon.
        if stop is not None and held:
            pending = {c for c in held if stop.triggered(P, c, i)}

        cash_days += (TOP_N - len(held)) / TOP_N
        eq *= (1 + gross - ct)
        eqs.append(eq)

    years = (N - start) / 252.0
    return (np.array(eqs), trades / years, stops / years,
            100.0 * cash_days / (N - start))


def metrics(eqs):
    if len(eqs) < 2:
        return {}
    r = np.diff(eqs) / eqs[:-1]
    cagr = eqs[-1] ** (252.0 / len(r)) - 1
    sd = r.std()
    sharpe = (r.mean() / sd * math.sqrt(252)) if sd > 0 else 0.0
    peak, mdd = -1e9, 0.0
    for v in eqs:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    # Ulcer: straffar DJUPA och LÅNGA sättningar – det stoppet påstås fixa.
    peak, sq = -1e9, []
    for v in eqs:
        peak = max(peak, v)
        sq.append((100.0 * (v / peak - 1)) ** 2)
    return {"cagr": cagr, "sharpe": sharpe, "mdd": mdd,
            "ulcer": math.sqrt(sum(sq) / len(sq))}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    universe_file = args[0] if args else "universe/sverige.csv"
    bench_sym = args[1] if len(args) > 1 else "^OMX"

    print(f"Hämtar {universe_file} + {bench_sym} ({YEARS}å dagsdata) …", file=sys.stderr)
    syms = load_universe(universe_file)
    data = fetch(syms, bench_sym, YEARS)
    if bench_sym not in data:
        print("Ingen indexdata.", file=sys.stderr)
        return 1
    bench = data[bench_sym]
    cal = bench.index
    prices = {s: data[s].reindex(cal).ffill() for s in syms if s in data}
    P = {c: prices[c].to_numpy(dtype=float) for c in prices}
    R = {c: prices[c].pct_change().fillna(0).to_numpy(dtype=float) for c in prices}
    B = bench.to_numpy(dtype=float)
    Brel = bench.pct_change().fillna(0).to_numpy(dtype=float)
    cols = list(prices)

    variants = [
        (None, "Inget stopp (som live nu)"),
        (Stop("ma50", "ma50"), "MA50-brott -> sälj"),
        (Stop("ma50c", "ma50", confirm=2), "MA50-brott, 2 stängningar"),
        (Stop("ma50q", "ma50", quarantine=True), "MA50-brott + karantän 1 mån"),
        (Stop("ma200", "ma200"), "MA200-brott -> sälj"),
        (Stop("dd", "dd", dd_pct=20.0), "-20 % från 60d-topp"),
    ]
    for s, _ in variants:
        if s is not None:
            s.prepare(P)

    bm = metrics(np.cumprod(1 + Brel[252:]))
    print("\n" + "=" * 100)
    print(f" STOPP-BACKTEST – Aktiemotorn, {universe_file} ({len(cols)} aktier, index {bench_sym})")
    print("=" * 100)
    print(f" Månadsvis, topp {TOP_N}, banding {BAND}, allpos-vakt, regimfilter, "
          f"courtage {COST_NET:.2f}%/affär.")
    print(" Stoppat innehav = KASSA (0 % ränta) till nästa rebalans. "
          "Affär dagen efter larmet. Survivorship bias.")
    print(f"\n   {'Variant':<30}{'CAGR':>9}{'Sharpe':>8}{'maxDD':>8}{'Ulcer':>8}"
          f"{'affärer/år':>12}{'stopp/år':>10}{'kassa%':>8}")
    base = None
    for stop, label in variants:
        eqs, tpy, spy, cashpct = simulate(P, R, B, cols, stop)
        m = metrics(eqs)
        if base is None:
            base = m
        print(f"   {label:<30}{m['cagr']:>+8.1%}{m['sharpe']:>8.2f}{m['mdd']:>+8.0%}"
              f"{m['ulcer']:>8.1f}{tpy:>12.0f}{spy:>10.0f}{cashpct:>8.0f}")
    print(f"   {'Index köp & behåll':<30}{bm['cagr']:>+8.1%}{bm['sharpe']:>8.2f}"
          f"{bm['mdd']:>+8.0%}{bm['ulcer']:>8.1f}{0:>12.0f}{0:>10.0f}{0:>8.0f}")
    print("\n TOLKNING: ett stopp ska sänka maxDD/Ulcer utan att äta upp CAGR. Höjer det")
    print(" BÅDE avkastning och Sharpe är det för bra för att vara sant – kolla courtage")
    print(" och antal stopp/år innan du tror på det. Whipsaw syns som många stopp/år")
    print(" kombinerat med lägre CAGR än 'inget stopp'.")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
