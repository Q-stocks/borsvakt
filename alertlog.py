#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – Larmloggen (modul 8): facit för alla strategier.

Detta är systemets sanningsserum. Varje skarp signal loggas med priset VID
larmtillfället. Efter 1/5/20/60 dagar mäter loggen den faktiska
framåtblickande avkastningen mot ett index – OUT-OF-SAMPLE. Det är så man
skiljer en äkta edge från curve-fitting: inte genom snygga backtest, utan
genom att se vad larmen faktiskt gjorde i skarpt läge innan riktiga pengar
riskeras.

Tre kommandon:
  python alertlog.py evaluate   # mät mognade signaler (kör dagligen)
  python alertlog.py report     # skicka scorecard till Telegram
  python alertlog.py show       # skriv ut loggen i terminalen

Filer (committas av workflows):
  log/alerts.csv       – varje signal: ts, modul, ticker, typ, pris, meta
  log/evaluations.csv  – utfall per signal och horisont
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # fristående – undviker cirkelimport
LOG_DIR = ROOT / "log"
ALERTS = LOG_DIR / "alerts.csv"
EVALS = LOG_DIR / "evaluations.csv"

HORIZONS = (1, 5, 20, 60)                         # handelsdagar framåt

# MÄTBENCHMARK – skilj det från `stocks.markets[].index_signal` (regimregeln)
# och från `sectortrend.BENCH` (relativ styrka). Att byta mätbenchmark ändrar
# hur resultatet REDOVISAS; att byta indexsignal ändrar vad systemet GÖR.
#
# SPY delar handelskalender och valuta med US-aktierna och återinvesterar
# utdelningar via auto_adjust → korrekt totalavkastning.
#
# ⚠️ ^OMX är ett PRISINDEX medan aktierna hämtas med auto_adjust=True, alltså
# MED utdelningar. Överavkastningen mot ^OMX är därför systematiskt för
# generös mot strategin. Uppmätt drag (2026-09-09, ^OMX mot XACT OMXS30 över
# 152 gemensamma månader): 3,31 pp/år ≈ 0,013 pp per handelsdag, dvs
#   1 d: +0,01   5 d: +0,07   20 d: +0,26   60 d: +0,79 pp för mycket.
# Varför inte byta ändå: den enda totalavkastningsserie som finns på Yahoo
# (XACT-OMXS30.ST) saknar 23 % av handelsdagarna (803 av 3514) och dess
# rullande 12m-skillnad svänger −11 till +16 pp. Att ffill:a den vore att
# byta en känd, konstant skevhet mot okänt brus. Backtesten har i stället
# fått ett EXAKT totalavkastningsbenchmark byggt av universumet självt.
INDEX_BY_MARKET = {"SE": "^OMX", "US": "SPY"}
SE_DIVIDEND_DRAG_PP_PER_DAY = 0.0131              # dokumenterad, används ej i beräkning

ALERT_COLS = ["ts", "date", "module", "ticker", "kind", "market", "price", "meta"]
# bench_sym tillagd 2026-09-09: varje mätpunkt bär numera sin egen
# benchmarkdefinition, så ett framtida byte inte tyst blandar två mått.
# Rader loggade före det saknar fältet och mättes mot INDEX_BY_MARKET ovan.
EVAL_COLS = ["signal_id", "module", "ticker", "kind", "date", "market",
             "horizon", "ret", "bench", "excess", "bench_sym"]

# NEDÅT-riktade signaler (sälj/varning). `excess` loggas alltid RÅTT i CSV:n –
# facit ska vara ett faktum, inte en tolkning – men i scorecarden vänds tecknet
# så att en varning som följs av fall räknas som TRÄFF. Utan detta hade
# nedsidesvakten sett ut som systemets sämsta modul just när den fungerade.
BEARISH_KINDS = {"ma50_break", "ma200_break", "drawdown"}


def signal_value(kind: str, excess: float) -> float:
    """Överavkastning sedd ur signalens egen riktning (>0 = signalen tillförde)."""
    return -excess if kind in BEARISH_KINDS else excess


# ----------------------------------------------------------------------
# Loggning (anropas av övriga moduler vid skarpa larm)
# ----------------------------------------------------------------------

def _latest_close(symbol: str) -> float | None:
    """Senaste AVSLUTADE dagsstängning. Yahoo lägger in dagens rad med tom
    Close redan före öppning – utan dropna() blev priset `nan` och signalen
    kunde aldrig utvärderas (drabbade alla 9 svenska köp 2026-08-03, när
    schemavakten flyttat monthly till 04:0x UTC = före Stockholmsöppning)."""
    import yfinance as yf
    h = yf.Ticker(symbol).history(period="5d", interval="1d", auto_adjust=True)
    if h is None or h.empty:
        return None
    closes = _drop_live_bar(h)["Close"].dropna()
    if closes.empty:
        return None
    return float(closes.iloc[-1])


def log_alert(module: str, ticker: str, kind: str, market: str = "SE",
              price: float | None = None, meta: dict | None = None,
              dry: bool = False) -> None:
    """Logga en skarp signal. Felsäker: loggfel får aldrig stoppa larmet."""
    try:
        if dry:
            print(f"  [DRY] skulle logga: {module}/{ticker}/{kind}")
            return
        LOG_DIR.mkdir(exist_ok=True)
        new = not ALERTS.exists()
        if price is None:
            try:
                price = _latest_close(ticker)
            except Exception:
                price = None
        # Bältet till hängslena: NaN/inf får aldrig skrivas som pris – raden
        # blir då oanvändbar i facit (och "nan" ser ut som ett riktigt värde).
        if price is not None and not math.isfinite(price):
            price = None
        now = dt.datetime.now(dt.timezone.utc)
        with open(ALERTS, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(ALERT_COLS)
            w.writerow([now.isoformat(timespec="seconds"), now.date().isoformat(),
                        module, ticker, kind, market,
                        f"{price:.4f}" if price is not None else "",
                        json.dumps(meta or {}, ensure_ascii=False)])
    except Exception as exc:
        print(f"alertlog: kunde inte logga {ticker}: {exc}", file=sys.stderr)


# ----------------------------------------------------------------------
# Utvärdering – framåtblickande avkastning mot index
# ----------------------------------------------------------------------

def _drop_live_bar(hist):
    """Släpper dagens ännu pågående bar (före 21:30 UTC = båda marknaderna
    stängda). Utvärderingar FRYSES i evaluations.csv – att mäta mot en levande
    intradagsbar ger permanent fel facit. (Egen kopia: alertlog är ROT-modul
    och får inte importera scanner – cirkelimport.)"""
    if hist is None or len(hist) == 0:
        return hist
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now.replace(hour=21, minute=30, second=0, microsecond=0)
    try:
        last_date = hist.index[-1].date()
    except (AttributeError, TypeError):
        return hist
    if last_date == now.date() and now < cutoff:
        return hist.iloc[:-1]
    return hist


def _hist(symbol: str, start: str, end: str):
    import yfinance as yf
    return _drop_live_bar(
        yf.Ticker(symbol).history(start=start, end=end, interval="1d", auto_adjust=True))


def _forward_return(ticker: str, sig_date: dt.date, horizon: int,
                    market: str) -> tuple[float, float] | None:
    """(aktieavkastning %, indexavkastning %) över `horizon` handelsdagar
    från första handelsdagen >= signaldatum."""
    start = (sig_date - dt.timedelta(days=4)).isoformat()
    end = (sig_date + dt.timedelta(days=horizon * 2 + 12)).isoformat()
    s = _hist(ticker, start, end)
    if s is None or len(s) < 2:
        return None
    sd = [d.date() for d in s.index]
    t0 = next((i for i, d in enumerate(sd) if d >= sig_date), None)
    if t0 is None or t0 + horizon >= len(s):
        return None  # ännu inte mognat
    p0 = float(s["Close"].iloc[t0])
    p1 = float(s["Close"].iloc[t0 + horizon])
    # Aktiebenet måste vara giltigt INNAN det skrivs. En NaN-kurs gav förut en
    # NaN-avkastning som ändå sparades – och _load_done() räknade den då som en
    # färdig mätpunkt, så den gjordes aldrig om (123 av 1509 rader 2026-09-06).
    # Hoppa i stället över: raden mäts på nytt nästa körning.
    if not (math.isfinite(p0) and math.isfinite(p1) and p0 > 0):
        return None
    stock_ret = (p1 / p0 - 1.0) * 100.0

    idx_sym = INDEX_BY_MARKET.get(market, "^OMX")
    # Index över SAMMA breda fönster som aktien, sedan aligna till aktiens
    # handelsdagar och läsa index-Close på SAMMA positionella t0/t0+horizon.
    # Båda benen måste spänna identiska handelsdagar (yfinance end är EXKLUSIV).
    b = _hist(idx_sym, start, end)
    if b is None or b.empty:
        # Benchfel: skriv ALDRIG bench=0 som om index stått stilla – hoppa
        # över så mätpunkten görs om nästa körning (raden fryses för evigt).
        return None
    bc = b["Close"].reindex(s.index, method="ffill")
    b0 = bc.iloc[t0]
    b1 = bc.iloc[t0 + horizon]
    if not (b0 == b0 and b1 == b1 and b0):   # NaN-skydd (NaN != NaN) + nollskydd
        return None
    bench_ret = (float(b1) / float(b0) - 1.0) * 100.0
    return stock_ret, bench_ret


def _ensure_columns() -> None:
    """Migrerar evaluations.csv till aktuell kolumnuppsättning.

    Utan detta skriver DictWriter fler värden än rubrikraden har namn, och
    DictReader lägger överskottet i en namnlös restnyckel – de nya fälten blir
    tysta och oläsbara. Migreringen är additiv: gamla rader får tomt värde,
    inga tal ändras. Originalet sparas en gång."""
    if not EVALS.exists():
        return
    with open(EVALS, encoding="utf-8", newline="") as fh:
        header = next(csv.reader(fh), None)
    if header is None or header == EVAL_COLS:
        return
    missing = [c for c in EVAL_COLS if c not in header]
    if not missing:
        return
    rows = list(csv.DictReader(open(EVALS, encoding="utf-8")))
    backup = LOG_DIR / f"evaluations_pre_migration_{dt.date.today():%Y%m%d}.csv"
    if not backup.exists():
        backup.write_bytes(EVALS.read_bytes())
    with open(EVALS, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=EVAL_COLS)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in EVAL_COLS} for r in rows)
    print(f"alertlog: la till kolumn(er) {', '.join(missing)} i evaluations.csv "
          f"({len(rows)} rader, original i {backup.name})")


def _finite(row: dict) -> bool:
    """En mätpunkt räknas bara som mätt om både aktie- och indexbenet är tal."""
    try:
        return math.isfinite(float(row["ret"])) and math.isfinite(float(row["excess"]))
    except (ValueError, KeyError, TypeError):
        return False


def _load_done() -> set:
    if not EVALS.exists():
        return set()
    out = set()
    for r in csv.DictReader(open(EVALS, encoding="utf-8")):
        # Icke-finita rader räknas INTE som färdiga – annars fryses ett
        # tillfälligt datafel för evigt som om det vore ett uppmätt utfall.
        if _finite(r):
            out.add((r["signal_id"], int(r["horizon"])))
    return out


def evaluate() -> int:
    if not ALERTS.exists():
        print("Ingen alerts.csv ännu.")
        return 0
    _ensure_columns()
    done = _load_done()
    today = dt.date.today()
    new_rows = []
    alerts = list(csv.DictReader(open(ALERTS, encoding="utf-8")))
    for r in alerts:
        try:
            sig_date = dt.date.fromisoformat(r["date"])
        except (ValueError, KeyError):
            continue
        market = r.get("market") or "SE"
        sid = f"{r['module']}:{r['ticker']}:{r['kind']}:{r['date']}"
        for h in HORIZONS:
            if (sid, h) in done:
                continue
            # mognadskrav: kalenderdagar >= ~handelsdagar * 1.5 (+helger)
            if (today - sig_date).days < int(h * 1.5) + 1:
                continue
            try:
                fr = _forward_return(r["ticker"], sig_date, h, market)
            except Exception as exc:
                print(f"  eval {r['ticker']} h{h}: {exc}", file=sys.stderr)
                continue
            if fr is None:
                continue
            sret, bret = fr
            if not (math.isfinite(sret) and math.isfinite(bret)):
                continue      # bältet till hängslena: skriv aldrig icke-finit facit
            new_rows.append({"signal_id": sid, "module": r["module"], "ticker": r["ticker"],
                             "kind": r["kind"], "date": r["date"], "market": market,
                             "horizon": h, "ret": round(sret, 4),
                             "bench": round(bret, 4), "excess": round(sret - bret, 4),
                             "bench_sym": INDEX_BY_MARKET.get(market, "^OMX")})
            done.add((sid, h))

    if new_rows:
        LOG_DIR.mkdir(exist_ok=True)
        new = not EVALS.exists()
        with open(EVALS, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=EVAL_COLS)
            if new:
                w.writeheader()
            w.writerows(new_rows)
    print(f"Utvärdering klar: {len(new_rows)} nya mätpunkter.")
    return 0


# ----------------------------------------------------------------------
# Scorecard
# ----------------------------------------------------------------------

def _agg(rows: list[dict]) -> dict:
    # Samma filtrering som dashboard._agg: icke-finita rader får varken förstöra
    # medelvärdet eller ligga kvar i nämnaren för träffprocenten. Utan detta
    # visade Telegram "nan" medan dashboarden visade ett tal – två kanaler,
    # två svar på samma fråga.
    ok = [r for r in rows if _finite(r)]
    dropped = len(rows) - len(ok)
    n = len(ok)
    if not n:
        return {"n": 0, "dropped": dropped} if dropped else {}
    excess = [signal_value(r["kind"], r["excess"]) for r in ok]
    wins = sum(1 for e in excess if e > 0)
    return {"n": n, "dropped": dropped, "hit": 100.0 * wins / n,
            "avg_excess": sum(excess) / n,
            "avg_ret": sum(r["ret"] for r in ok) / n}


def report(dry: bool = False) -> int:
    from scanner import send_telegram  # funktionslokal => ingen cirkelimport

    if not EVALS.exists():
        send_telegram("📊 <b>Larmlogg</b>: inga utvärderade signaler ännu. "
                      "Loggen mognar – återkom när signaler passerat horisonterna.", dry)
        return 0
    rows = list(csv.DictReader(open(EVALS, encoding="utf-8")))
    if not rows:                              # header-only fil => inga mätpunkter
        send_telegram("📊 <b>Larmlogg</b>: inga utvärderade signaler ännu. "
                      "Loggen mognar – återkom när signaler passerat horisonterna.", dry)
        return 0
    parsed = []
    for r in rows:
        try:
            r["horizon"] = int(r["horizon"])
            r["excess"] = float(r["excess"])
            r["ret"] = float(r["ret"])
        except (ValueError, TypeError, KeyError):
            continue          # trasig rad får aldrig fälla hela scorecarden
        parsed.append(r)
    rows = parsed

    modules = sorted({r["module"] for r in rows})
    L = ["📊 <b>Larmlogg – scorecard</b>",
         "<i>Framåtblickande avkastning mot index, out-of-sample. "
         "Överavkastning &gt; 0 = signalen tillförde värde. För säljsignaler "
         "(nedsidesvakten) är tecknet vänt: ett fall efter varningen = träff. "
         "Rå avkastning visas alltid osminkad.</i>", ""]
    for m in modules:
        L.append(f"<b>{m}</b>")
        for h in HORIZONS:
            sub = [r for r in rows if r["module"] == m and r["horizon"] == h]
            a = _agg(sub)
            if not a.get("n"):
                continue
            L.append(f"  {h}d: n={a['n']}, träff {a['hit']:.0f}%, "
                     f"snitt-överavk {a['avg_excess']:+.1f}% "
                     f"(rå {a['avg_ret']:+.1f}%)")
        L.append("")
    total = _agg(rows)
    L.append(f"<b>Totalt:</b> {total['n']} mätpunkter, träff {total['hit']:.0f}%, "
             f"snitt-överavk {total['avg_excess']:+.1f}%")
    if total.get("dropped"):
        # Bortfallet ska SYNAS. Ett tyst filtrerat datafel ser ut som färre
        # signaler, inte som ett fel att åtgärda.
        L.append(f"⚠️ {total['dropped']} mätpunkter uteslutna (ogiltig kursdata) – "
                 f"körs om automatiskt nästa utvärdering.")
    L.append("<i>Litet n = osäkert. Döm ingen strategi förrän några månaders "
             "signaler hunnit mogna. Detta är facit – inte backtest.</i>")
    if any(r["market"] == "SE" for r in rows):
        # Skevheten ska stå där siffrorna läses, inte bara i en kodkommentar.
        L.append(f"<i>⚠️ Svenska tal mäts mot ^OMX som är ett PRISINDEX medan "
                 f"aktiekurserna innehåller utdelningar. Överavkastningen är "
                 f"därför ca {SE_DIVIDEND_DRAG_PP_PER_DAY:.3f} pp per handelsdag "
                 f"för generös (1d +0,01 · 5d +0,07 · 20d +0,26 · 60d +0,79 pp). "
                 f"Dra bort det innan du drar slutsatser om långa horisonter.</i>")
    if not send_telegram("\n".join(L), dry):
        print("alertlog: scorecard-notisen kunde inte levereras – steget "
              "failar för omkörning.", file=sys.stderr)
        return 1
    return 0


def repair(dry: bool = False) -> int:
    """Städar bort icke-finita mätpunkter ur evaluations.csv så att de kan mätas
    om. Originalet sparas EN gång som evaluations_pre_repair_<datum>.csv – facit
    ska aldrig skrivas om utan spår. Rader som inte går att mäta (avnoterat,
    borttagen ticker) skrivs helt enkelt inte tillbaka: 'ej mätt' är ett ärligare
    svar än en nolla eller ett NaN."""
    if not EVALS.exists():
        print("Ingen evaluations.csv att reparera.")
        return 0
    if not dry:
        _ensure_columns()
    rows = list(csv.DictReader(open(EVALS, encoding="utf-8")))
    ok = [r for r in rows if _finite(r)]
    bad = len(rows) - len(ok)
    if not bad:
        print(f"Inget att reparera: alla {len(rows)} mätpunkter är giltiga.")
        return 0
    per_mod: dict[str, int] = {}
    for r in rows:
        if not _finite(r):
            per_mod[r.get("module", "?")] = per_mod.get(r.get("module", "?"), 0) + 1
    print(f"{bad} av {len(rows)} mätpunkter är ogiltiga: "
          + ", ".join(f"{k}={v}" for k, v in sorted(per_mod.items())))
    if dry:
        print("[DRY] skulle säkerhetskopiera och skriva om filen.")
        return 0
    backup = LOG_DIR / f"evaluations_pre_repair_{dt.date.today():%Y%m%d}.csv"
    if not backup.exists():
        backup.write_bytes(EVALS.read_bytes())
        print(f"Original sparat: {backup.name}")
    with open(EVALS, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=EVAL_COLS)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in EVAL_COLS} for r in ok)
    print(f"Klart: {len(ok)} giltiga rader kvar. De borttagna mäts om vid nästa "
          f"'alertlog.py evaluate' och skrivs bara om de ger giltiga tal.")
    return 0


def show() -> int:
    for f, label in [(ALERTS, "ALERTS"), (EVALS, "EVALUATIONS")]:
        print(f"\n=== {label} ({f}) ===")
        if f.exists():
            print(f.read_text(encoding="utf-8")[:4000])
        else:
            print("(saknas)")
    return 0


def main() -> int:
    # Windows-konsol: tvinga UTF-8 så svenska/emoji inte kraschar utskrift.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    cmd = sys.argv[1] if len(sys.argv) > 1 else "evaluate"
    dry = "--dry-run" in sys.argv
    if cmd == "evaluate":
        if dry:
            # `evaluate` SKREV förut till evaluations.csv även med --dry-run.
            # Flaggan ska betyda samma sak i alla moduler: rör ingenting.
            print("[DRY] evaluate skriver till log/evaluations.csv – hoppar över.")
            return 0
        return evaluate()
    if cmd == "report":
        return report(dry)
    if cmd == "repair":
        return repair(dry)
    if cmd == "show":
        return show()
    print(f"Okänt kommando: {cmd}. Använd evaluate | report | repair | show.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
