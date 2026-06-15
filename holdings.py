#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – Innehav (modul, rekonstruerad)

Läser holdings.csv (dina egna aktier via scanner.load_holdings) och beräknar
status per innehav: pris, vinst/förlust mot inköp, trend mot 50/200-dagars MA,
nedgång från 60-dagars topp och avstånd till MA50/MA200. Resultatet sparas i
state['holdings_status'] för dashboardens Innehav-flik.

LARMAR EJ. Nedsidesvakten (exits.py), scannern och sektortrenden gör det – de
läser samma holdings.csv. Den här modulen är ren statusberäkning, körs dagligen.

Körning:  python holdings.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import yaml

from scanner import CONFIG_FILE, load_holdings, load_state, save_state


def analyse(symbol: str) -> dict | None:
    import yfinance as yf
    hist = yf.Ticker(symbol).history(period="14mo", interval="1d", auto_adjust=True)
    if hist is None or len(hist) < 60:
        return None
    close = hist["Close"].dropna()
    price = float(close.iloc[-1])
    sma50 = float(close.tail(50).mean())
    sma200 = float(close.tail(200).mean()) if len(close) >= 200 else None
    high60 = float(close.tail(60).max())
    dd = (price / high60 - 1.0) * 100.0 if high60 else 0.0
    return {
        "price": price,
        "above50": price > sma50,
        "above200": (price > sma200) if sma200 is not None else None,
        "dd60": round(dd, 1),
        "dist50": round((price / sma50 - 1.0) * 100.0, 1) if sma50 else None,
        "dist200": round((price / sma200 - 1.0) * 100.0, 1) if sma200 else None,
    }


def build_status(h: dict) -> dict:
    sym = h["ticker"]
    row = {"ticker": sym, "market": h.get("market"), "shares": h.get("shares"),
           "entry_price": h.get("entry_price"), "entry_date": h.get("entry_date"),
           "note": h.get("note", ""), "error": False}
    try:
        a = analyse(sym)
    except Exception as exc:
        print(f"  {sym}: holdings-fel: {exc}", file=sys.stderr)
        a = None
    if a is None:
        row["error"] = True
        return row
    row.update(a)
    ep = h.get("entry_price")
    if ep:
        row["pl_pct"] = round((a["price"] / ep - 1.0) * 100.0, 1)
        if h.get("shares"):
            row["value"] = round(a["price"] * h["shares"], 0)
            row["pl_abs"] = round((a["price"] - ep) * h["shares"], 0)
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="Börsvakt – Innehav (status, inga larm)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not cfg.get("holdings", {}).get("enabled", True):
        print("holdings: avstängd i config.yaml.")
        return 0

    holdings = load_holdings()
    if not holdings:
        print("Inga innehav i holdings.csv.")
        return 0

    rows = [build_status(h) for h in holdings]
    ok = sum(1 for r in rows if not r["error"])
    print(f"Innehav uppdaterade: {ok}/{len(rows)} med data.")

    if not args.dry_run:
        state = load_state()
        state["holdings_status"] = {"updated": dt.date.today().isoformat(), "rows": rows}
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
