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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # fristående – undviker cirkelimport
LOG_DIR = ROOT / "log"
ALERTS = LOG_DIR / "alerts.csv"
EVALS = LOG_DIR / "evaluations.csv"

HORIZONS = (1, 5, 20, 60)                         # handelsdagar framåt
INDEX_BY_MARKET = {"SE": "^OMX", "US": "SXR8.DE"}

ALERT_COLS = ["ts", "date", "module", "ticker", "kind", "market", "price", "meta"]
EVAL_COLS = ["signal_id", "module", "ticker", "kind", "date", "market",
             "horizon", "ret", "bench", "excess"]


# ----------------------------------------------------------------------
# Loggning (anropas av övriga moduler vid skarpa larm)
# ----------------------------------------------------------------------

def _latest_close(symbol: str) -> float | None:
    import yfinance as yf
    h = yf.Ticker(symbol).history(period="5d", interval="1d", auto_adjust=True)
    if h is None or h.empty:
        return None
    return float(h["Close"].iloc[-1])


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

def _hist(symbol: str, start: str, end: str):
    import yfinance as yf
    return yf.Ticker(symbol).history(start=start, end=end, interval="1d", auto_adjust=True)


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
    stock_ret = (p1 / p0 - 1.0) * 100.0

    bench_ret = 0.0
    idx_sym = INDEX_BY_MARKET.get(market, "^OMX")
    # Index över SAMMA breda fönster som aktien, sedan aligna till aktiens
    # handelsdagar och läsa index-Close på SAMMA positionella t0/t0+horizon.
    # Båda benen måste spänna identiska handelsdagar (yfinance end är EXKLUSIV).
    b = _hist(idx_sym, start, end)
    if b is not None and not b.empty:
        bc = b["Close"].reindex(s.index, method="ffill")
        b0 = bc.iloc[t0]
        b1 = bc.iloc[t0 + horizon]
        if b0 == b0 and b1 == b1 and b0:   # NaN-skydd (NaN != NaN) + nollskydd
            bench_ret = (float(b1) / float(b0) - 1.0) * 100.0
    return stock_ret, bench_ret


def _load_done() -> set:
    if not EVALS.exists():
        return set()
    out = set()
    for r in csv.DictReader(open(EVALS, encoding="utf-8")):
        out.add((r["signal_id"], int(r["horizon"])))
    return out


def evaluate() -> int:
    if not ALERTS.exists():
        print("Ingen alerts.csv ännu.")
        return 0
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
            new_rows.append({"signal_id": sid, "module": r["module"], "ticker": r["ticker"],
                             "kind": r["kind"], "date": r["date"], "market": market,
                             "horizon": h, "ret": round(sret, 4),
                             "bench": round(bret, 4), "excess": round(sret - bret, 4)})
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
    n = len(rows)
    if not n:
        return {}
    excess = [r["excess"] for r in rows]
    wins = sum(1 for e in excess if e > 0)
    return {"n": n, "hit": 100.0 * wins / n,
            "avg_excess": sum(excess) / n,
            "avg_ret": sum(r["ret"] for r in rows) / n}


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
    for r in rows:
        r["horizon"] = int(r["horizon"])
        r["excess"] = float(r["excess"])
        r["ret"] = float(r["ret"])

    modules = sorted({r["module"] for r in rows})
    L = ["📊 <b>Larmlogg – scorecard</b>",
         "<i>Framåtblickande avkastning mot index, out-of-sample. "
         "Överavkastning &gt; 0 = signalen tillförde värde.</i>", ""]
    for m in modules:
        L.append(f"<b>{m}</b>")
        for h in HORIZONS:
            sub = [r for r in rows if r["module"] == m and r["horizon"] == h]
            a = _agg(sub)
            if not a:
                continue
            L.append(f"  {h}d: n={a['n']}, träff {a['hit']:.0f}%, "
                     f"snitt-överavk {a['avg_excess']:+.1f}% "
                     f"(rå {a['avg_ret']:+.1f}%)")
        L.append("")
    total = _agg(rows)
    L.append(f"<b>Totalt:</b> {total['n']} mätpunkter, träff {total['hit']:.0f}%, "
             f"snitt-överavk {total['avg_excess']:+.1f}%")
    L.append("<i>Litet n = osäkert. Döm ingen strategi förrän några månaders "
             "signaler hunnit mogna. Detta är facit – inte backtest.</i>")
    if not send_telegram("\n".join(L), dry):
        print("alertlog: scorecard-notisen kunde inte levereras – steget "
              "failar för omkörning.", file=sys.stderr)
        return 1
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
        return evaluate()
    if cmd == "report":
        return report(dry)
    if cmd == "show":
        return show()
    print(f"Okänt kommando: {cmd}. Använd evaluate | report | show.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
