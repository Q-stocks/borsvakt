#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – Regimläge (modul 10): cykel-KONTEXT, inte cykel-prognos.

Här drar jag en hård, ärlig gräns. Att bygga något som säger "vi är i år 3
av en 4-årscykel, gå in NU" är exakt den falska precision jag varnat för:
makrocykler är verkliga i efterhand men opålitliga framåt – de överlappar,
upprepas inte på schema, och även proffsens makrofonder missar vändpunkterna
gång på gång. Sådant byggs INTE.

Det som GÅR att göra ärligt är att BESKRIVA nuläget med observerbara,
regelbaserade mått – och låta systemet reagera mekaniskt (trendfiltret),
inte förutsäga. Den här modulen rapporterar:

  • Risk på/av: globalt aktieindex över/under sitt 200-dagars MA.
  • Räntekurva: 10 år minus 3 mån (^TNX − ^IRX). Invertering har historiskt
    FÖREGÅTT recessioner – men med lång och varierande fördröjning. Gul
    lampa, inte en timer.
  • Bredd: andel bevakade sektorer över sitt 200-dagars MA.

Och den översätter läget till HANDLING: vilka av dina moduler du bör luta dig
mot just nu (offensivt vs defensivt). Det är kompassen ditt dokument
efterfrågade – reaktiv, inte spågumma.

Körning:  python regime.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import yaml

from scanner import CONFIG_FILE, load_state, save_state, send_telegram


def _daily(symbol: str, period: str = "14mo"):
    import yfinance as yf
    h = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=True)
    return h if (h is not None and not h.empty) else None


def _above_200(symbol: str):
    h = _daily(symbol)
    if h is None or len(h) < 200:
        return None
    price = float(h["Close"].iloc[-1])
    sma = float(h["Close"].tail(200).mean())
    return price > sma, price, sma


def _yield_curve():
    """10 år − 3 mån via Yahoo (^TNX, ^IRX). Returnerar (spread, t10, t3)."""
    t10 = _daily("^TNX", "1mo")
    t3 = _daily("^IRX", "1mo")
    if t10 is None or t3 is None:
        return None
    a = float(t10["Close"].iloc[-1])
    b = float(t3["Close"].iloc[-1])
    return a - b, a, b


def main() -> int:
    ap = argparse.ArgumentParser(description="Börsvakt – Regimläge (kontext, ej prognos)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg_r = cfg.get("regime", {})
    if not cfg_r.get("enabled", False):
        print("regime: avstängd i config.yaml.")
        return 0

    eq_symbol = cfg_r.get("equity_signal", "EUNL.DE")
    L = [f"🧭 <b>Regimläge – {dt.date.today():%Y-%m-%d}</b>",
         "<i>Beskrivning av nuläget, inte en prognos. Systemet reagerar "
         "mekaniskt – det spår inte vändpunkter.</i>", ""]

    risk_on = None
    eq = _above_200(eq_symbol)
    if eq is None:
        L.append("• Risk på/av: ⚠️ kunde inte läsa indexdata.")
    else:
        risk_on, price, sma = eq
        state = "🟢 RISK PÅ" if risk_on else "🔴 RISK AV"
        rel = "över" if risk_on else "under"
        L.append(f"• {state} – globala aktier {rel} 200-dagars MA "
                 f"({price:.1f} vs {sma:.1f}).")

    inverted = None
    yc = _yield_curve()
    if yc is None:
        L.append("• Räntekurva: ⚠️ kunde inte läsa (^TNX/^IRX).")
    else:
        spread, t10, t3 = yc
        inverted = spread < 0
        flag = "🟡 INVERTERAD" if inverted else "🟢 normal"
        L.append(f"• Räntekurva (10å−3m): {flag}, {spread:+.2f} pe "
                 f"(10å {t10:.2f} / 3m {t3:.2f}).")
        if inverted:
            L.append("  <i>Invertering har föregått recessioner – men med lång, "
                     "oregelbunden fördröjning. Varning, inte signal.</i>")

    # Bredd över bevakade sektorer (om sectors-modulen är konfigurerad)
    sectors = cfg.get("sectors", {}).get("assets", [])
    breadth_above = breadth_total = 0
    if sectors:
        # Hämta en gång per sektor – återanvänds i både notis och snapshot
        # (annars 3 okachade yfinance-anrop per sektor som kan ge olika svar).
        results = [(a, _above_200(a["signal"])) for a in sectors]
        for a, r in results:
            if r is not None:
                breadth_total += 1
                breadth_above += 1 if r[0] else 0
        if breadth_total:
            pct = 100.0 * breadth_above / breadth_total
            L.append(f"• Bredd: {breadth_above}/{breadth_total} sektorer ({pct:.0f} %) över 200-dagars MA.")

    # Översätt till handling (regelbaserat, inte prognos)
    L.append("")
    L.append("<b>Vad det betyder för modulerna:</b>")
    if risk_on is False or inverted:
        L.append("⚠️ Defensivt läge: luta dig mot <b>trendföljning</b> (kraschskydd) "
                 "och <b>kvalitetsfiltret</b>. Momentum/sektorer kan ge falska "
                 "utbrott när regimen är svag. Mindre positionsstorlek.")
    elif risk_on:
        L.append("✅ Offensivt läge: <b>momentum, sektorrotation och PEAD</b> har "
                 "historiskt bäst medvind. Trendföljningen ligger ändå kvar som bas.")
    else:
        L.append("Blandat/okänt läge – följ modulernas egna trendfilter.")
    L.append("")
    L.append("<i>Allt ovan är observerbara regler, inga förutsägelser. "
             "Ej rådgivning.</i>")

    # Persistera snapshot för dashboarden
    if not args.dry_run:
        st = load_state()
        snap = {"date": dt.date.today().isoformat(), "risk_on": risk_on,
                "inverted": inverted}
        if yc is not None:
            snap["curve_spread"] = round(yc[0], 2)
        if breadth_total:
            # Återanvänd breddmätningen ovan – ingen ny yfinance-hämtning.
            snap["breadth_pct"] = round(100 * breadth_above / breadth_total)
        if risk_on is False or inverted:
            snap["stance"] = ("Defensivt: luta dig mot trendföljning och kvalitetsfiltret. "
                              "Momentum/sektorer kan ge falska utbrott.")
        elif risk_on:
            snap["stance"] = ("Offensivt: momentum, sektorrotation och PEAD har medvind. "
                              "Trendföljningen ligger kvar som bas.")
        st["regime"] = snap
        save_state(st)
    send_telegram("\n".join(L), args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
