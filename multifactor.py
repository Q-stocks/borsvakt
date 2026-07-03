#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – Multifaktor-motorn (modul 6, rekonstruerad)

Kombinerar tre sleeves till en likaviktad portfölj:
  • Momentum        – Sammansatt momentum (snitt 3/6/12 mån), topp N i uppåttrend.
  • Trendande värde – billigast (t.ex. lägst EV/EBIT) BLAND bolag i uppåttrend.
  • Trendande kvalitet – högst kvalitet (t.ex. F-Score) bland bolag i uppåttrend.

På riskjusterad basis slår kombon momentum ensamt eftersom faktorerna toppar vid
olika tidpunkter. Värde/kvalitet kräver en Börsdata-export (data/fundamenta_*.csv);
saknas den körs bara momentum-sleeven (degraderar mjukt). Den kombinerade
portföljen sparas i state['stock_portfolio'] så nedsidesvakten och scannern
bevakar innehaven.

Körning:  python multifactor.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import sys

import yaml

from alertlog import log_alert
from momentum import _ret, month_end_closes
from stocks import load_borsdata, load_universe
from scanner import CONFIG_FILE, load_state, save_state, send_telegram


def score_universe(universe: list[tuple[str, str]]) -> tuple[list[dict], list[str]]:
    scored, errors = [], []
    for ticker, name in universe:
        closes = month_end_closes(ticker)
        if closes is None:
            errors.append(ticker)
            continue
        r3, r6, r12 = _ret(closes, 3), _ret(closes, 6), _ret(closes, 12)
        sma10 = float(closes.iloc[-10:].mean())
        scored.append({"ticker": ticker, "name": name,
                       "r3": r3, "r6": r6, "r12": r12,
                       "score": (r3 + r6 + r12) / 3.0,
                       "above": float(closes.iloc[-1]) > sma10})
    return scored, errors


def fundamenta_map(mkt: dict, cfg_m: dict) -> dict:
    """ticker(Yahoo) -> {'quality': float|None, 'value': float|None} från Börsdata."""
    path = mkt.get("fundamenta_file", "")
    qcol = cfg_m.get("quality_column", "F-Score")
    vcol = cfg_m.get("value_column", "EV/EBIT")
    bd = load_borsdata(path, {"ticker_column": "Ticker", "quality_column": qcol,
                              "ticker_overrides": {}}, mkt.get("market", "SE"))
    if not bd:
        return {}
    # load_borsdata mappar bara en kvalitetskolumn; läs värdekolumnen separat.
    out = {r["ticker"].upper(): {"quality": r.get("quality"), "value": None} for r in bd}
    try:
        from stocks import load_quality
        vals = load_quality(path, "Ticker", vcol)
        if vals:
            for t, v in vals.items():
                out.setdefault(t.upper(), {"quality": None, "value": None})["value"] = v
    except Exception:
        pass
    return out


def sleeve_momentum(scored: list[dict], top_n: int) -> list[str]:
    ranked = sorted(scored, key=lambda r: r["score"], reverse=True)
    return [r["ticker"] for r in ranked if r["above"]][:top_n]


def sleeve_value(scored: list[dict], fmap: dict, top_n: int, lower_better: bool) -> list[str]:
    cand = [r for r in scored if r["above"]
            and fmap.get(r["ticker"].upper(), {}).get("value") is not None]
    cand.sort(key=lambda r: fmap[r["ticker"].upper()]["value"], reverse=not lower_better)
    return [r["ticker"] for r in cand[:top_n]]


def sleeve_quality(scored: list[dict], fmap: dict, top_n: int) -> list[str]:
    cand = [r for r in scored if r["above"]
            and fmap.get(r["ticker"].upper(), {}).get("quality") is not None]
    cand.sort(key=lambda r: fmap[r["ticker"].upper()]["quality"], reverse=True)
    return [r["ticker"] for r in cand[:top_n]]


def process_market(mkt: dict, cfg_m: dict, state: dict, dry: bool) -> str:
    name = mkt["name"]
    top_n = int(cfg_m.get("top_n", 10))
    universe = load_universe(mkt["universe_file"])
    scored, errors = score_universe(universe)
    fmap = fundamenta_map(mkt, cfg_m)
    by_ticker = {r["ticker"]: r for r in scored}

    mom = sleeve_momentum(scored, top_n)
    val = sleeve_value(scored, fmap, top_n, bool(cfg_m.get("value_lower_better", True))) if fmap else []
    qua = sleeve_quality(scored, fmap, top_n) if fmap else []

    combined = sorted(set(mom) | set(val) | set(qua),
                      key=lambda t: by_ticker[t]["score"] if t in by_ticker else -1e9,
                      reverse=True)

    # Höll-vid-datafel (samma princip som Aktiemotorn): ett ÄGT innehav vars
    # kursdata felar kan inte bedömas och BEHÅLLS – ett tillfälligt Yahoo-fel
    # får aldrig bli en tyst implicit försäljning ur sleeven (som exits/
    # scannern bevakar via state).
    prev = list(state.get("stock_portfolio", {}).get(f"Multifaktor-{name}", []))
    err_set = set(errors)
    held_errors = [t for t in prev if t in err_set and t not in combined]
    combined = combined + held_errors
    sells = [t for t in prev if t not in combined]
    buys = [t for t in combined if t not in prev]

    L = [f"🧬 <b>Multifaktor – {html.escape(name)} – {dt.date.today():%Y-%m}</b>"]
    if not fmap:
        L.append("<i>Ingen fundamenta-fil – kör bara momentum-sleeven. "
                 "Lägg data/fundamenta_*.csv för värde/kvalitet (se BORSDATA-EXPORT.md).</i>")
    L.append("")

    def fmt(tickers):
        return ", ".join(html.escape(t) for t in tickers) if tickers else "–"

    L.append(f"<b>Momentum:</b> {fmt(mom)}")
    L.append(f"<b>Trendande värde:</b> {fmt(val)}")
    L.append(f"<b>Trendande kvalitet:</b> {fmt(qua)}")
    L.append("")
    L.append(f"<b>Kombinerad portfölj ({len(combined)} st, likaviktad):</b>")
    for t in combined:
        r = by_ticker.get(t)
        if r:
            L.append(f"• <b>{html.escape(t)}</b> {html.escape(r['name'])}  12m {r['r12']:+.0%}")
        elif t in held_errors:
            L.append(f"• <b>{html.escape(t)}</b> kursdata saknas – behålls utan "
                     f"omprövning (kontrollera manuellt)")
    L.append("")
    L.append("<b>Byten denna månad:</b>")
    L.append("• Sälj: " + (", ".join(html.escape(t) for t in sells) if sells else "–"))
    L.append("• Köp: " + (", ".join(html.escape(t) for t in buys) if buys else "–"))
    if errors:
        L.append(f"⚠️ Saknar data ({len(errors)} av {len(universe)}): "
                 f"{', '.join(html.escape(e) for e in errors[:8])}"
                 + (" …" if len(errors) > 8 else ""))
    L.append("")
    L.append("<i>Momentum byts månads-/kvartalsvis, värde/kvalitet årsvis (låg "
             "omsättning). Ej rådgivning.</i>")

    # OBS: state uppdateras INTE här – anroparen roterar portföljen först vid
    # bekräftad Telegram-leverans (annars tappas månadens lista permanent).
    return "\n".join(L), f"Multifaktor-{name}", combined


def main() -> int:
    ap = argparse.ArgumentParser(description="Börsvakt – Multifaktor-motorn")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg_m = cfg.get("multifactor", {})
    if not cfg_m.get("enabled", False):
        print("multifactor: avstängd i config.yaml.")
        return 0

    state = load_state()
    undelivered = []
    for mkt in cfg_m.get("markets", []):
        try:
            text, sleeve, combined = process_market(mkt, cfg_m, state, args.dry_run)
            # Leveransvillkorat: rotera portföljen först vid bekräftad leverans;
            # vid miss failar steget så schemavakten kör om månadssignalerna.
            if send_telegram(text, args.dry_run):
                prev = set(state.setdefault("stock_portfolio", {}).get(sleeve, []))
                for t in combined:
                    if t not in prev:
                        log_alert("multifactor", t, "buy",
                                  market=mkt.get("market", "SE"), dry=args.dry_run)
                state["stock_portfolio"][sleeve] = combined
            else:
                undelivered.append(mkt.get("name"))
        except Exception as exc:
            # Ett kraschat marknadsvarv = olevererad månadsnotis: räkna det
            # som miss så steget failar och schemavakten kör om (transienta
            # datafel självläker; bestående fel blir SYNLIGA i stället för
            # en tyst utebliven rebalansering).
            print(f"Multifaktor {mkt.get('name')}: fel: {exc}", file=sys.stderr)
            undelivered.append(mkt.get("name"))
    if not args.dry_run:
        save_state(state)
    if undelivered:
        print(f"Multifaktor: notisen kunde inte levereras för "
              f"{', '.join(map(str, undelivered))} – steget failar för omkörning.",
              file=sys.stderr)
        return 1
    print("Multifaktor klar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
