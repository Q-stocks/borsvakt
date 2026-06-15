#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – Trendföljning (modul 7): multi-tillgång, defensiv overlay.

Klassisk trendföljning (Faber GTAA / "managed futures lite"): håll bara de
tillgångsslag som stänger ÖVER sitt 10-månaders glidande medel, likaviktat;
resten till kassa. Eftersom korgen spänner över aktier, obligationer, guld
och råvaror är detta ett genuint diversifierat KRASCHSKYDD på portföljnivå –
det som har bäst stöd mot momentums värsta drag (kraschar i björnmarknad).

Skillnad mot övriga moduler:
  • momentum.py väljer den BÄSTA regionen (offensivt, en tillgång).
  • trend.py håller ALLA tillgångar i uppåttrend (defensivt, bred korg).
  • exits.py vaktar enskilda aktieinnehav.
De kompletterar varandra – kör trend.py som stabiliserande bas och
momentum/aktier som offensiv satellit, eller trend.py som hela
"sov-gott"-delen av portföljen.

EU-kund: alla förslag är UCITS (PRIIPs). Tillgångar vars data inte går att
hämta (overifierad ticker) hoppas tyst över med varning – så funkar
modulen även innan alla tickers är bekräftade.

Körning:  python trend.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import sys

import yaml

from momentum import month_end_closes
from scanner import CONFIG_FILE, send_telegram


def build_message(rows: list[dict], cash: dict) -> str:
    on = [r for r in rows if r.get("on")]
    off = [r for r in rows if r.get("on") is False]
    err = [r for r in rows if r.get("on") is None]

    n_slots = len(rows) - len(err)
    weight = (100.0 / n_slots) if n_slots else 0.0
    cash_weight = weight * len(off)

    L = [f"🌍 <b>Trendföljning – {dt.date.today():%Y-%m}</b>", ""]
    L.append("<b>Allokering denna månad (likaviktat):</b>")
    for r in on:
        L.append(f"✅ {html.escape(r['name'])} – {weight:.0f} %  "
                 f"<i>({html.escape(r['trade'])})</i>")
    for r in off:
        L.append(f"⛔ {html.escape(r['name'])} – 0 % (under 10-mån MA)")
    if cash_weight > 0:
        L.append(f"💰 {html.escape(cash.get('name', 'Kassa'))} – {cash_weight:.0f} %  "
                 f"<i>({html.escape(cash.get('trade', 'räntefond'))})</i>")
    for r in err:
        L.append(f"⚠️ {html.escape(r['name'])}: ingen data ({html.escape(r['signal'])}) – verifiera ticker")
    L.append("")
    L.append("<i>Behåll tillgångar över sitt 10-mån glidande medel, resten i kassa. "
             "Uppdatera vid månadsskifte. Bred trendkorg = kraschskydd, inte "
             "maximal avkastning. Ej rådgivning.</i>")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Börsvakt – Trendföljning (multi-tillgång)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg_t = cfg.get("trend", {})
    if not cfg_t.get("enabled", False):
        print("trend: avstängd i config.yaml.")
        return 0

    sma_n = int(cfg_t.get("sma_months", 10))
    rows = []
    for a in cfg_t.get("assets", []):
        r = dict(a)
        closes = month_end_closes(a["signal"])
        if closes is None:
            r["on"] = None
        else:
            r["on"] = float(closes.iloc[-1]) > float(closes.iloc[-sma_n:].mean())
        rows.append(r)

    send_telegram(build_message(rows, cfg_t.get("cash", {})), args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
