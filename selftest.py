#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – röktest av de skydd som hittats den hårda vägen.

Varje test här motsvarar en bugg som faktiskt nått drift. Inget nätverk, ingen
state, inga larm – kör det före varje push:

    python selftest.py

Bakgrund per block:
  • liquidity/gate  – 2026-08-06: GRANGX.ST köptes trots noll omsatt volym i
    12 månader och fastfrusen kursserie.
  • kvalitetsfilter – 2026-09-06: `quality_file` lästes även med
    `universe_from_quality: false`, så en rutinimport för multifaktorn kunde
    slå på ett förkastat filter – och med Börsdata-format på tickrarna matcha
    noll rader = tomt universum = hela portföljen såld.
  • föräldralösa innehav – 2026-09-06: ett ägt namn som saknades i
    universumfilen fick rank 10^9 och såldes utan att kursdata prövats.
  • facit – 2026-09-06: NaN-mätpunkter frystes som färdiga (123 av 1509 rader)
    och Telegrams scorecard räknade med dem medan dashboarden filtrerade bort
    dem, så kanalerna gav olika svar.
"""

from __future__ import annotations

import csv
import math
import pathlib
import subprocess
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import alertlog
import dashboard
import stocks

OK = FAIL = 0


def check(name: str, cond: bool) -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FEL  {name}")


def bars(closes, vols):
    """Månadsbarer som slutar på senaste avslutade månad."""
    idx = pd.date_range(end=pd.Timestamp.today().normalize() - pd.offsets.MonthEnd(1),
                        periods=len(closes), freq="ME", tz="Europe/Stockholm")
    return pd.DataFrame({"Close": list(closes), "Volume": list(vols)}, index=idx)


def test_liquidity():
    print("\n--- Likviditet och frusna serier (2026-08-06) ---")
    frozen = bars([11.46] * 24, [0] * 24)
    liq = stocks.liquidity(frozen)
    check("frusen serie utan handel flaggas frozen+dead", liq["frozen"] and liq["dead"])
    dead = bars(np.linspace(10, 12, 24), [0] * 18 + [1000] * 6)
    check("6+ nollmånader = dead", stocks.liquidity(dead)["dead"])
    thin = bars(np.linspace(10, 12, 24), [500] * 24)
    check("tunn men handlad serie är inte dead", not stocks.liquidity(thin)["dead"])
    frisk = bars(np.linspace(10, 22, 24), [1_000_000] * 24)
    check("frisk serie är varken frozen eller dead",
          not stocks.liquidity(frisk)["frozen"] and not stocks.liquidity(frisk)["dead"])
    check("frisk omsättning över grinden", stocks.liquidity(frisk)["turnover"] > 250_000)

    base = {"r3": 0.1, "r6": 0.2, "r12": 0.3, "liquid": True}
    check("illikvid kandidat blockeras som nytt köp",
          stocks._passes_gate({**base, "liquid": False}, "allpos") is False)
    check("nollmomentum blockeras av allpos (GRANGX-fallet)",
          stocks._passes_gate({"r3": 0.0, "r6": 0.0, "r12": 0.0, "liquid": True},
                              "allpos") is False)
    ranked = [{"ticker": "A", "r3": 1, "r6": 1, "r12": 1, "liquid": True, "dead": False},
              {"ticker": "D", "r3": 1, "r6": 1, "r12": 1, "liquid": False, "dead": True},
              {"ticker": "T", "r3": 1, "r6": 1, "r12": 1, "liquid": False, "dead": False}]
    port, _ = stocks.apply_banding(ranked, ["D", "T"], 3, 20, "allpos")
    check("ohandelsbart innehav plockas ur portföljen", "D" not in port)
    check("tunt men handlat innehav behålls av banding", "T" in port)


def _market_runner(tmp, qfile, universe):
    """Kör process_market isolerat: inga nätanrop, ingen state, dry-run."""
    frisk = {t: bars(np.linspace(10, 20 + i, 24), [1_000_000] * 24)
             for i, (t, _) in enumerate(universe)}
    frisk["AGD.ST"] = bars(np.linspace(10, 30, 24), [1_000_000] * 24)
    stocks.load_universe = lambda p: list(universe)
    stocks.month_end_bars = lambda t: frisk.get(t)
    stocks.risk_on = lambda *a, **k: (True, True)
    mkt = {"name": "Test", "market": "SE", "universe_file": "x.csv",
           "index_signal": "^OMX", "quality_file": str(qfile)}

    def run(cfg_extra, prev=None, missing=()):
        stocks.month_end_bars = lambda t: None if t in missing else frisk.get(t)
        cfg = {"top_n": 10, "band_keep": 20, "momentum_gate": "allpos",
               "min_daily_turnover": 0, "ticker_column": "Ticker",
               "quality_column": "F-Score", "quality_top_pct": 50, **cfg_extra}
        state = {"stock_portfolio": {"Test": list(prev or [])}}
        text, port, _ = stocks.process_market(mkt, cfg, state, dry=True)
        return text, port

    return run


def test_quality_filter(tmp):
    print("\n--- Kvalitetsfiltret kan inte smyga på (2026-09-06) ---")
    univ = [(f"T{i}.ST", f"Bolag {i}") for i in range(10)]
    qfile = tmp / "fundamenta.csv"
    run = _market_runner(tmp, qfile, univ)

    qfile.write_text("Ticker,F-Score\n" + "\n".join(f"T{i}.ST,{9-i}" for i in range(10)),
                     encoding="utf-8")
    text, port = run({"quality_filter_enabled": False})
    check("fil finns men flaggan är av: filtret körs inte", len(port) == 10)
    check("notisen säger att filtret är av", "quality_filter_enabled: false" in text)

    text, port = run({"quality_filter_enabled": True})
    check("flaggan på: topp 50 % väljs", len(port) == 5)

    qfile.write_text("Ticker,F-Score\n" + "\n".join(f"T{i},{9-i}" for i in range(10)),
                     encoding="utf-8")
    text, port = run({"quality_filter_enabled": True})
    check("ticker-formatfel tömmer INTE universumet", len(port) == 10)
    check("formatfelet syns i notisen", "HOPPAT ÖVER" in text)


def test_orphans_and_datafail(tmp):
    print("\n--- Föräldralösa innehav och totalt datafel (2026-09-06) ---")
    univ = [(f"T{i}.ST", f"Bolag {i}") for i in range(10)]
    qfile = tmp / "tom.csv"
    run = _market_runner(tmp, qfile, univ)
    cfg = {"quality_filter_enabled": False}

    text, port = run(cfg, prev=["AGD.ST", "T1.ST"])
    check("ägt namn utanför universumet rankas i stället för att säljas blint",
          "AGD.ST" in port)
    check("det syns i notisen", "saknas i universumfilen" in text)

    text, port = run(cfg, prev=["AGD.ST", "T1.ST"], missing={"AGD.ST"})
    check("ägt namn utanför universumet utan kursdata behålls", "AGD.ST" in port)

    text, port = run(cfg, prev=["T1.ST", "T2.ST"],
                     missing={f"T{i}.ST" for i in range(10)} | {"AGD.ST"})
    check("inget rankbart = portföljen står oförändrad", port == ["T1.ST", "T2.ST"])
    check("DATAFEL syns i notisen", "DATAFEL" in text)


def test_facit(tmp):
    print("\n--- Facit: NaN, teckenvändning och reparation (2026-09-06) ---")
    alertlog.LOG_DIR, alertlog.EVALS = tmp, tmp / "e.csv"
    alertlog.ALERTS = tmp / "a.csv"
    with open(alertlog.EVALS, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=alertlog.EVAL_COLS)
        w.writeheader()
        w.writerow({"signal_id": "m:T:buy:2026-01-01", "module": "m", "ticker": "T",
                    "kind": "buy", "date": "2026-01-01", "market": "SE", "horizon": 1,
                    "ret": "nan", "bench": "1.0", "excess": "nan"})
        w.writerow({"signal_id": "m:U:buy:2026-01-01", "module": "m", "ticker": "U",
                    "kind": "buy", "date": "2026-01-01", "market": "SE", "horizon": 1,
                    "ret": "2.0", "bench": "1.0", "excess": "1.0"})
    done = alertlog._load_done()
    check("NaN-mätpunkt räknas inte som färdig", ("m:T:buy:2026-01-01", 1) not in done)
    check("giltig mätpunkt räknas som färdig", ("m:U:buy:2026-01-01", 1) in done)

    tg = alertlog._agg([{"kind": "buy", "excess": float("nan"), "ret": float("nan")},
                        {"kind": "buy", "excess": 2.0, "ret": 2.0}])
    db = dashboard._agg([{"kind": "buy", "excess": "nan", "ret": "nan"},
                         {"kind": "buy", "excess": "2.0", "ret": "2.0"}])
    check("Telegram filtrerar NaN", tg["n"] == 1 and tg["avg_excess"] == 2.0)
    check("Telegram och dashboard ger samma svar",
          (tg["n"], tg["avg_excess"]) == (db["n"], db["excess"]))
    check("bortfallet räknas", tg["dropped"] == 1)

    check("köpsignal behåller tecken", alertlog.signal_value("buy", -3.0) == -3.0)
    check("säljsignal vänder tecken", alertlog.signal_value("ma50_break", -3.0) == 3.0)
    agg = alertlog._agg([{"kind": "ma50_break", "excess": -5.0, "ret": -6.0},
                         {"kind": "ma50_break", "excess": -3.0, "ret": -4.0}])
    check("fall efter säljvarning = träff", agg["hit"] == 100.0)
    check("rå avkastning visas osminkad", agg["avg_ret"] == -5.0)

    alertlog.repair(dry=False)
    rows = list(csv.DictReader(open(alertlog.EVALS, encoding="utf-8")))
    backups = list(tmp.glob("evaluations_pre_repair_*.csv"))
    check("repair tar bort ogiltiga rader", len(rows) == 1)
    check("repair säkerhetskopierar originalet",
          len(backups) == 1 and
          len(list(csv.DictReader(open(backups[0], encoding="utf-8")))) == 2)

    root = pathlib.Path(__file__).resolve().parent
    r = subprocess.run([sys.executable, str(root / "alertlog.py"), "evaluate", "--dry-run"],
                       capture_output=True, text=True, encoding="utf-8", cwd=str(root))
    check("evaluate --dry-run skriver inte", "[DRY]" in (r.stdout or ""))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    tmp = pathlib.Path(tempfile.mkdtemp())
    test_liquidity()
    test_quality_filter(tmp)
    test_orphans_and_datafail(tmp)
    test_facit(tmp)
    print(f"\n===== {OK} gröna, {FAIL} röda =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
