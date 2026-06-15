#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – PEAD-motorn (modul 5): post-earnings-announcement drift.

Fenomenet (Bernard & Thomas m.fl.): aktier som SLÅR vinstförväntan –
och/eller reagerar starkt positivt på rapportdagen – fortsätter driva
uppåt i ungefär ett kvartal. En av de mest ihållande anomalierna och den
mest realistiska "snabbare än momentum" för en privatperson: lägre
omsättning än daytrading, men snabbare feedback än månadsmomentum.

Signal per aktie:
  1. Hittade en rapport inom de senaste `lookback_days` dagarna?
  2. Vinstöverraskning ≥ `surprise_pct_min`  ELLER  onormal kursreaktion
     på rapportdagen ≥ `reaction_pct_min` (mot index).
  3. Om ja och aktien inte redan följs → KÖPLARM, lägg i drift-portföljen
     med inträdesdatum.
  4. Dagligen: innehav äldre än driftfönstret (`hold_days` kalenderdagar
     ≈ ett kvartal) → SÄLJLARM, ut ur drift-portföljen.

Long-only som standard (positiv drift). Missar/short-sidan finns men är
svårare för retail och avstängd.

Datakällor:
  • USA: yfinance `earnings_dates` (estimat + utfall + surprise).
  • Sverige: Börsdata-rapportexport (data/earnings_sverige.csv) – yfinance
    har dålig täckning på Stockholmsbörsen. Kursreaktionen räknas ändå.

Körning:  python pead.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import math
import sys

import yaml

from alertlog import log_alert
from scanner import CONFIG_FILE, ROOT, load_state, save_state, send_telegram


# ----------------------------------------------------------------------
# Rapportdata
# ----------------------------------------------------------------------

def _num(x):
    """yfinance lämnar saknade värden som numpy NaN (inte None). Returnera
    None för saknat/NaN, annars float – så 'nan' aldrig läcker till state."""
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return None
        return float(x)
    except (ValueError, TypeError):
        return None


def recent_earnings_yf(symbol: str, lookback_days: int) -> list[dict]:
    """Senaste rapporterna från yfinance inom lookback-fönstret."""
    import yfinance as yf

    df = yf.Ticker(symbol).get_earnings_dates(limit=12)
    if df is None or df.empty:
        return []
    out = []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days + 2)
    for idx, row in df.iterrows():
        date = idx.to_pydatetime()
        if date.tzinfo is None:
            date = date.replace(tzinfo=dt.timezone.utc)
        if date > dt.datetime.now(dt.timezone.utc) or date < cutoff:
            continue  # framtida rapport eller för gammal
        est = _num(row.get("EPS Estimate"))
        act = _num(row.get("Reported EPS"))
        surp = _num(row.get("Surprise(%)"))
        if surp is None and est not in (None, 0) and act is not None:
            try:
                surp = (act - est) / abs(est) * 100.0
            except (ValueError, ZeroDivisionError, TypeError):
                surp = None
        out.append({"date": date.date(),
                    "surprise": surp,
                    "eps_est": est, "eps_act": act})
    return out


def load_earnings_export(path: str) -> dict:
    """Börsdata-rapportexport → ticker -> [{date, surprise}]. Förväntade
    kolumner: Ticker, Rapportdatum, samt antingen 'Surprise' (%) eller
    'EPS' + 'EPS estimat'. Tickers konverteras Börsdata->Yahoo (.ST)."""
    from stocks import borsdata_to_yahoo  # återanvänd konverteraren

    p = ROOT / path
    if not p.exists():
        return {}
    with open(p, encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delim = ";" if sample.count(";") > sample.count(",") else ","
        reader = csv.DictReader(fh, delimiter=delim)
        out: dict[str, list] = {}
        for r in reader:
            low = {k.lower().strip(): (v or "").strip() for k, v in r.items() if k}
            raw_t = low.get("ticker", "")
            date_s = low.get("rapportdatum") or low.get("datum") or low.get("date")
            if not raw_t or not date_s:
                continue
            try:
                d = dt.date.fromisoformat(date_s[:10])
            except ValueError:
                continue
            surp = None
            if low.get("surprise"):
                try:
                    surp = float(low["surprise"].replace("%", "").replace(",", "."))
                except ValueError:
                    pass
            elif low.get("eps") and low.get("eps estimat"):
                try:
                    a = float(low["eps"].replace(",", "."))
                    e = float(low["eps estimat"].replace(",", "."))
                    surp = (a - e) / abs(e) * 100.0 if e else None
                except (ValueError, ZeroDivisionError):
                    pass
            t = borsdata_to_yahoo(raw_t, "SE", {})
            out.setdefault(t, []).append({"date": d, "surprise": surp})
    return out


def report_reaction(symbol: str, report_date: dt.date, index_symbol: str) -> float | None:
    """Onormal avkastning kring rapportdagen (aktie minus index),
    från stängning dagen före till stängning ca 1 dag efter."""
    import yfinance as yf

    start = (report_date - dt.timedelta(days=7)).isoformat()
    end = (report_date + dt.timedelta(days=7)).isoformat()
    s = yf.Ticker(symbol).history(start=start, end=end, interval="1d", auto_adjust=True)
    ix = yf.Ticker(index_symbol).history(start=start, end=end, interval="1d", auto_adjust=True)
    if s is None or len(s) < 2:
        return None
    sd = [d.date() for d in s.index]
    after = [i for i, d in enumerate(sd) if d >= report_date]
    if not after or after[0] == 0:
        return None
    j = after[0]
    k = min(j + 1, len(s) - 1)  # stängning ~1 dag efter rapport
    stock_ret = float(s["Close"].iloc[k] / s["Close"].iloc[j - 1] - 1.0) * 100.0
    idx_ret = 0.0
    if ix is not None and len(ix) >= 2:
        # Lägg index på SAMMA staplar som aktien (j-1 .. k), inte hela
        # ~2-veckorsfönstret – annars jämförs olika tidsspann.
        ixc = ix["Close"].reindex(s.index, method="ffill")
        try:
            a = float(ixc.iloc[j - 1])
            b = float(ixc.iloc[k])
            if not (math.isnan(a) or math.isnan(b)) and a:
                idx_ret = (b / a - 1.0) * 100.0
        except (ValueError, TypeError, ZeroDivisionError, IndexError):
            idx_ret = 0.0
    return stock_ret - idx_ret


# ----------------------------------------------------------------------
# Larm
# ----------------------------------------------------------------------

def build_entry_alert(name, symbol, surprise, reaction, hold_days) -> str:
    bits = []
    if surprise is not None:
        bits.append(f"vinstöverraskning {surprise:+.0f} %")
    if reaction is not None:
        bits.append(f"onormal reaktion {reaction:+.0f} % på rapportdagen")
    drivers = " och ".join(bits) if bits else "stark rapportsignal"
    return (
        f"🟢 <b>PEAD – KÖPKANDIDAT</b>\n"
        f"<b>{html.escape(name)}</b> ({html.escape(symbol)})\n"
        f"Drivkraft: {drivers}.\n\n"
        f"<b>Tesen:</b> aktier som slår förväntan fortsätter historiskt driva "
        f"uppåt i ~{hold_days} dagar – marknaden underreagerar på goda nyheter.\n"
        f"<b>Plan:</b> köp efter rapportdagen (jaga inte själva hoppet), håll "
        f"driftfönstret, sälj vid SÄLJLARM eller om kursen bryter ned under sin "
        f"trend dessförinnan.\n\n"
        f"<i>Eventdriven heuristik, ej rådgivning. Verifiera att rapporten var "
        f"ett verkligt vinstslag och inte en engångseffekt.</i>"
    )


def build_exit_alert(name, symbol, days_held) -> str:
    return (
        f"⚪ <b>PEAD – DRIFTFÖNSTRET STÄNGT</b>\n"
        f"<b>{html.escape(name)}</b> ({html.escape(symbol)}) har hållits "
        f"~{days_held} dagar sedan rapporten.\n\n"
        f"Driften klingar historiskt av efter ett kvartal. Sälj enligt regeln, "
        f"eller rulla över till momentum-/trendmotorn om aktien fortfarande "
        f"rankas högt där.\n\n<i>Larm, ej order.</i>"
    )


# ----------------------------------------------------------------------
# Motorn
# ----------------------------------------------------------------------

def load_symbols(path: str) -> list[tuple[str, str]]:
    from stocks import load_universe
    return load_universe(path)


def process_market(mkt: dict, cfg_p: dict, state: dict, dry: bool) -> None:
    name = mkt["name"]
    idx = mkt["index_signal"]
    lookback = int(cfg_p.get("lookback_days", 5))
    hold = int(cfg_p.get("hold_days", 70))
    smin = float(cfg_p.get("surprise_pct_min", 5.0))
    rmin = float(cfg_p.get("reaction_pct_min", 5.0))

    drift = state.setdefault("drift_portfolio", {}).setdefault(name, {})
    names = dict(load_symbols(mkt["universe_file"]))

    # Förladda Börsdata-export om källan är 'export'
    export = {}
    if mkt.get("earnings_source") == "export":
        export = load_earnings_export(mkt.get("earnings_file", ""))

    today = dt.date.today()

    # 1) Exit: stäng positioner vars driftfönster löpt ut
    for sym in list(drift):
        entry = dt.date.fromisoformat(drift[sym]["report"])
        held = (today - entry).days
        if held >= hold:
            send_telegram(build_exit_alert(names.get(sym, sym), sym, held), dry)
            del drift[sym]

    # 2) Entry: leta färska rapporter som kvalificerar
    for sym, nm in names.items():
        if sym in drift:
            continue  # följs redan
        try:
            # Hämta senaste rapporten
            events = []
            if mkt.get("earnings_source") == "export":
                events = [e for e in export.get(sym, [])
                          if 0 <= (today - e["date"]).days <= lookback]
            else:
                events = recent_earnings_yf(sym, lookback)
            if not events:
                continue
            ev = sorted(events, key=lambda e: e["date"], reverse=True)[0]

            surprise = ev.get("surprise")
            reaction = report_reaction(sym, ev["date"], idx)

            ok_surprise = surprise is not None and surprise >= smin
            ok_reaction = reaction is not None and reaction >= rmin
            if not (ok_surprise or ok_reaction):
                continue

            send_telegram(build_entry_alert(nm, sym, surprise, reaction, hold), dry)
            log_alert("pead", sym, "entry",
                      market=("US" if name == "USA" else "SE"),
                      meta={"surprise": surprise, "reaction": reaction}, dry=dry)
            drift[sym] = {"report": ev["date"].isoformat(),
                          "surprise": surprise, "reaction": reaction,
                          "entry_logged": today.isoformat()}
        except Exception as exc:
            print(f"  {sym}: PEAD-fel: {exc}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="Börsvakt – PEAD-motorn")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg_p = cfg.get("pead", {})
    if not cfg_p.get("enabled", False):
        print("pead: avstängd i config.yaml.")
        return 0

    state = load_state()
    for mkt in cfg_p.get("markets", []):
        try:
            process_market(mkt, cfg_p, state, args.dry_run)
        except Exception as exc:
            print(f"PEAD {mkt.get('name')}: fel: {exc}", file=sys.stderr)
    save_state(state)
    print("PEAD-koll klar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
