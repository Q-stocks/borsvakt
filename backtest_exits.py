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
MOM_CAP = 10.0       # = config.yaml stocks.momentum_cap (+1000 %) på NYA köp.
                     # Fanns bara i drift, inte här – därför kunde backtesten
                     # köpa namn som live hade blockerat (se sane_series).
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
            # allpos OCH momentumtaket – samma vakt som config.yaml kör skarpt.
            passes = ((lambda c: all(x > 0 for x in meta[c]) and meta[c][2] < MOM_CAP)
                      if gate == "allpos" else (lambda c: meta[c][2] < MOM_CAP))
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


MIN_START_PRICE = 0.01      # under detta är kursen avrundad mot noll -> ratio exploderar
MAX_DAY_MOVE = 3.0          # +300 % på en dag = trasig justering, inte en aktie


def sane_series(P, c, start: int, N: int) -> bool:
    """Sant om kursserien går att räkna på – falskt vid trasig justering.

    Bakgrund (2026-09-09): en första version av jämförelsekorgen gav +62 %/år
    och två serier stod för 99 % av slutvärdet. ORRON.ST redovisades som
    0,0001 -> 6,83 kr (70 966x — startkursen nertryckt mot noll av Yahoos
    bakåtjustering efter Lundin Energy-utskiftningen) och SBB-B.ST hade en
    endagsrörelse på +27 700 %. Medianaktien gav +13,5 %/år.

    Tröskeln är medvetet låg: äkta öresaktier (Episurf ~0,09 kr) ska INTE
    falla bort. Bara serier vars startkurs ligger under ett öre, eller som
    fyrdubblas på en dag, räknas som trasiga.

    Mäts från seriens första giltiga bar, inte från `start` – ett bolag som
    noterades mitt i perioden är inte ett datafel.
    """
    px = P[c][start:N]
    valid = np.flatnonzero(np.isfinite(px) & (px > 0))
    if len(valid) < 2:
        return False
    px = px[valid[0]:]
    px = px[np.isfinite(px)]
    if len(px) < 2 or px[0] < MIN_START_PRICE:
        return False
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.diff(px) / px[:-1]
    return not (np.nanmax(np.abs(r)) > MAX_DAY_MOVE)


def universe_benchmark(P, R, start: int, N: int):
    """'Äg hela universumet' som EXAKT totalavkastningsbenchmark.

    Varför inte bara indexet: ^OMX är ett PRISINDEX medan aktiekurserna här
    hämtas med auto_adjust=True, alltså MED utdelningar. Att jämföra dem är
    att ge strategin utdelningarna gratis – uppmätt 3,31 pp/år (^OMX mot XACT
    OMXS30, 152 gemensamma månader). Den enda svenska totalavkastningsserien
    på Yahoo saknar 23 % av handelsdagarna och duger inte som daglig serie.

    Korgen använder exakt samma prisdata som strategin: totalavkastande per
    konstruktion, samma handelskalender och SAMMA survivorship bias. Den är
    därför en ärlig spegel av strategins egna förutsättningar – men den är
    INTE ett marknadsindex och ska aldrig presenteras som ett.

    Tre kurvor, med olika syfte:
      • monthly – likavikt, omviktad var 21:a dag som strategin. Robust
                  centralmått och den rimligaste "äg allt"-jämförelsen.
      • daily   – omviktad varje dag, precis som strategisimuleringen. Den
                  enda raden som är apples-to-apples med CAGR-talen ovan.
      • buyhold – aktieantalen stilla från dag ett. Ärligast i teorin men
                  extremt utfallskänslig: några få namn dominerar helt.
    Skillnaden daily minus buyhold ÄR omviktningspremien i modellen.
    """
    # Korgen kräver kurs redan vid `start` – ett bolag som noterades senare kan
    # inte ingå i en köp-och-behåll-korg från dag ett. Det snävar in urvalet
    # ytterligare (en survivorship-effekt till) och ska redovisas som sådant.
    cols = [c for c in P if sane_series(P, c, start, N)
            and not math.isnan(P[c][start]) and P[c][start] > 0]
    dropped = [c for c in P if c not in set(cols)
               and not math.isnan(P[c][start]) and P[c][start] > 0]
    if not cols:
        return None
    base = np.array([P[c][start] for c in cols])
    px = np.array([P[c][start:N] for c in cols])          # (namn, dagar)
    with np.errstate(invalid="ignore"):
        rel = px / base[:, None]
    buyhold = np.nanmean(rel, axis=0)
    rr = np.array([R[c][start:N] for c in cols])
    daily = np.cumprod(1 + np.nanmean(rr, axis=0))

    # Månadsvis omviktning: håll vikterna stilla inom varje 21-dagarsperiod.
    monthly = np.ones(N - start)
    eq = 1.0
    for i0 in range(0, N - start, STEP):
        i1 = min(i0 + STEP, N - start)
        seg = rel[:, i0:i1] / rel[:, i0][:, None]
        monthly[i0:i1] = eq * np.nanmean(seg, axis=0)
        eq = monthly[i1 - 1]
    return {"buyhold": buyhold / buyhold[0], "daily": daily / daily[0],
            "monthly": monthly / monthly[0], "n": len(cols), "dropped": dropped,
            "median_mult": float(np.nanmedian(rel[:, -1]))}


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
    # Trasiga kursserier ut ur STRATEGINS universum, inte bara ur jämförelse-
    # korgen: mätt 2026-09-09 kom +8,5 pp av midlarge-CAGR:n och +7,3 pp av
    # broad-CAGR:n från ORRON.ST/SBB-B.ST/CARA.ST, som simuleringen faktiskt
    # köpte. Drift skyddades av momentum_cap; backtesten saknade det.
    alla = list(prices)
    cols = [c for c in alla if sane_series(P, c, 252, len(B))]
    skippade = [c for c in alla if c not in set(cols)]
    if skippade:
        print(f"  Uteslutna (trasig kursjustering): {', '.join(sorted(skippade))}",
              file=sys.stderr)

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
    # ^-symboler är rena index (inga utdelningar). En ETF som SPY eller
    # XACT-OMXS30.ST hämtas med auto_adjust och ÄR totalavkastande.
    ar_prisindex = bench_sym.startswith("^")
    bench_etikett = (f"{bench_sym} (PRISindex, u. utdeln.)" if ar_prisindex
                     else f"{bench_sym} (ETF, totalavk.)")
    print(f"\n   {'JÄMFÖRELSER':<30}")
    print(f"   {bench_etikett:<30}{bm['cagr']:>+8.1%}"
          f"{bm['sharpe']:>8.2f}{bm['mdd']:>+8.0%}{bm['ulcer']:>8.1f}"
          f"{0:>12.0f}{0:>10.0f}{0:>8.0f}")
    ub = universe_benchmark(P, R, 252, len(B))
    if ub:
        rader = [("Äg allt, månadsvis omvikt", "monthly"),
                 ("Äg allt, daglig omvikt", "daily"),
                 ("Äg allt, köp & behåll", "buyhold")]
        mm = {}
        for label, key in rader:
            m = metrics(ub[key])
            mm[key] = m
            print(f"   {label:<30}{m['cagr']:>+8.1%}{m['sharpe']:>8.2f}"
                  f"{m['mdd']:>+8.0%}{m['ulcer']:>8.1f}{0:>12.0f}{0:>10.0f}{0:>8.0f}")
        print(f"\n LÄS SÅ HÄR")
        if ar_prisindex:
            print(f" • {bench_sym} är ett PRISindex medan aktierna hämtas MED utdelningar.")
            print(f"   Den raden ger strategin utdelningarna gratis (~3,3 pp/år i Sverige)")
            print(f"   och jämför dessutom mot 30 storbolag i stället för detta universum.")
        else:
            print(f" • {bench_sym} är totalavkastande, men speglar ett annat universum")
            print(f"   än det som handlas här – jämför främst mot 'Äg allt'-raderna.")
        print(f" • 'Äg allt' är byggt av SAMMA prisdata: totalavkastande, samma kalender,")
        print(f"   samma survivorship bias – en spegel av strategins förutsättningar,")
        print(f"   INTE ett marknadsindex. {ub['n']} av {len(cols)} namn ingår; "
              f"medianaktien gav {ub['median_mult']:.1f}x.")
        if ub["dropped"]:
            print(f" • {len(ub['dropped'])} namn uteslutna ur korgen (trasig justering: "
                  f"startkurs < {MIN_START_PRICE} kr\n   eller endagsrörelse > "
                  f"{MAX_DAY_MOVE:.0%}): {', '.join(sorted(ub['dropped'])[:8])}"
                  + (" …" if len(ub["dropped"]) > 8 else ""))
            print(f"   ⚠️ De ligger KVAR i strategins universum – kontrollera om de "
                  f"påverkar rankningen.")
        print(f" • Strategin viktar om dagligen, så den apples-to-apples-jämförelsen är")
        print(f"   'daglig omvikt' ({mm['daily']['cagr']:+.1%}). Skillnaden mot "
              f"'köp & behåll' ({mm['buyhold']['cagr']:+.1%}) är hur\n   mycket av "
              f"ALLA CAGR-tal ovan som kommer ur viktningsmodellen, inte ur strategin.")
    print("\n TOLKNING: ett stopp ska sänka maxDD/Ulcer utan att äta upp CAGR. Höjer det")
    print(" BÅDE avkastning och Sharpe är det för bra för att vara sant – kolla courtage")
    print(" och antal stopp/år innan du tror på det. Whipsaw syns som många stopp/år")
    print(" kombinerat med lägre CAGR än 'inget stopp'.")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
