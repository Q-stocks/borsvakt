#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – månadsmotor (modul 1)

Dual momentum med trendfilter på ett litet, likvitt ETF-universum.

Regeln (hela regeln – det finns inget mer):
  1. Vid varje månadsskifte: beräkna 3-, 6- och 12-månadersavkastning
     per tillgång på månadsstängningar. Poäng = snittet av de tre.
  2. Trendfilter: tillgången måste stänga över sitt 10-månaders
     glidande medel (SMA10) för att få ägas.
  3. Äg de `top_n` högst rankade som klarar filtret.
     Klarar ingen filtret: gå till kassa-/räntealternativet.
  4. Gör ingenting förrän nästa månadsskifte.

Varför så enkelt? För att enkelheten ÄR egenskapen: en regel per månad
går att följa i tio år. Signalerna räknas på senast AVSLUTADE månaden,
så livekurser är per definition onödiga här.

Körning:
  python momentum.py --dry-run   # visa signalen utan att skicka
  python momentum.py             # skicka månadssignal till Telegram
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import sys

import pandas as pd
import yaml

from scanner import CONFIG_FILE, send_telegram


def month_end_closes(symbol: str):
    """Månadsstängningar (totalavkastningsjusterade), exkl. innevarande månad."""
    import yfinance as yf

    hist = yf.Ticker(symbol).history(period="2y", interval="1mo", auto_adjust=True)
    if hist is None or hist.empty:
        return None
    closes = hist["Close"].dropna()
    # Släng innevarande (ofullbordad) månad – och allt nyare, så att en
    # tz-/locale-skiftad delårsbar inte slinker med (exakt år/månad-match
    # kan missa den och bara trimma en enda rad).
    cur = pd.Period(dt.date.today(), freq="M")
    closes = closes[closes.index.tz_localize(None).to_period("M") < cur]
    return closes if len(closes) >= 13 else None


def _ret(closes, months: int) -> float:
    return float(closes.iloc[-1] / closes.iloc[-1 - months] - 1.0)


def evaluate(cfg_m: dict) -> list[dict]:
    rows = []
    for asset in cfg_m["universe"]:
        closes = month_end_closes(asset["signal"])
        if closes is None:
            rows.append({**asset, "error": True})
            continue
        r3, r6, r12 = _ret(closes, 3), _ret(closes, 6), _ret(closes, 12)
        sma_n = int(cfg_m.get("sma_months", 10))
        sma = float(closes.iloc[-sma_n:].mean())
        rows.append(
            {
                **asset,
                "error": False,
                "r3": r3,
                "r6": r6,
                "r12": r12,
                "score": (r3 + r6 + r12) / 3.0,
                "above": float(closes.iloc[-1]) > sma,
            }
        )
    return rows


def build_message(rows: list[dict], cfg_m: dict) -> str:
    ok = [r for r in rows if not r.get("error")]
    ok.sort(key=lambda r: r["score"], reverse=True)
    top = [r for r in ok if r["above"]][: int(cfg_m.get("top_n", 1))]

    lines = [f"📅 <b>Månadssignal – {dt.date.today():%Y-%m}</b>", ""]
    for i, r in enumerate(ok, 1):
        flag = "✅" if r["above"] else "⛔"
        pick = "  ◀️ ÄGS" if r in top else ""
        lines.append(
            f"{i}. {flag} <b>{html.escape(r['name'])}</b>  "
            f"3m {r['r3']:+.1%} · 6m {r['r6']:+.1%} · 12m {r['r12']:+.1%}{pick}"
        )
    for r in rows:
        if r.get("error"):
            lines.append(f"⚠️ {html.escape(r['name'])}: kunde inte hämta data ({r['signal']})")

    lines.append("")
    if top:
        names = " + ".join(f"{html.escape(r['name'])} ({html.escape(r['trade'])})" for r in top)
        lines.append(f"📌 <b>Regel denna månad: äg {names}</b>")
    else:
        cash = cfg_m.get("cash", {})
        lines.append(
            f"📌 <b>Regel denna månad: ingen tillgång över trendfiltret → "
            f"{html.escape(cash.get('name', 'kassa'))} ({html.escape(cash.get('trade', 'räntefond'))})</b>"
        )
    lines.append("")
    lines.append(
        "<i>✅/⛔ = över/under 10-mån glidande medel (månadsstängning). "
        "Agera bara vid månadsskifte – däremellan är tystnad en del av regeln. "
        "Premien har historiskt tillfallit den som följer regeln mekaniskt, "
        "även när det känns fel. Ej rådgivning.</i>"
    )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Börsvakt – månadssignal (dual momentum)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg_m = cfg.get("momentum", {})
    if not cfg_m.get("enabled", False):
        print("momentum: avstängd i config.yaml.")
        return 0

    rows = evaluate(cfg_m)
    send_telegram(build_message(rows, cfg_m), args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
