#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – Nedsidesvakt (modul 4)

Asymmetrin du efterfrågade: KÖP långsamt (månadsvis momentum), men ÖVERVÄG
att SÄLJA snabbare när trenden viker. "Trendlinjer" är subjektiva och svåra
att mekanisera – den objektiva, backtestbara motsvarigheten är glidande
medel. Den här modulen bevakar dina INNEHAV (från Aktiemotorns
state-portfölj) en gång per handelsdag och larmar när:

  • kursen stänger under 50-dagars glidande medel  (tidig varning)
  • kursen stänger under 200-dagars glidande medel (trend bruten – starkast)
  • kursen fallit ≥ `drawdown_pct` från sin 60-dagars topp

VIKTIGT, ärligt: forskningen om per-aktie-stop är BLANDAD. Trendexit minskar
typiskt nedgångar (drawdown) men kan kosta avkastning genom whipsaw – den
höjer alltså sällan avkastningen, den jämnar ut resan. Den välbelagda
kraschskyddet är i stället regimfiltret på portföljnivå (redan i stocks.py).
Därför är detta LARM, aldrig autoförsäljning, och månadskärnan står kvar.

Körning:  python exits.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import sys

import yaml

from scanner import CONFIG_FILE, load_holdings, load_state, save_state, send_telegram


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
    return {"price": price, "sma50": sma50, "sma200": sma200, "high60": high60, "dd": dd}


def build_exit_alert(name, symbol, a, level, dd_pct) -> str:
    if level == "sma200":
        head = "🛑 <b>TREND BRUTEN</b> – under 200-dagars MA"
        body = (f"Stänger {a['price']:.2f}, under MA200 ≈ {a['sma200']:.2f}. "
                f"Det starkaste trendexit-larmet.")
    elif level == "sma50":
        head = "⚠️ <b>Tidig varning</b> – under 50-dagars MA"
        body = (f"Stänger {a['price']:.2f}, under MA50 ≈ {a['sma50']:.2f}. "
                f"Ofta en första svaghetssignal; trenden är inte nödvändigtvis bruten.")
    else:
        head = f"↓ <b>{abs(a['dd']):.0f} % från toppen</b>"
        body = (f"Stänger {a['price']:.2f}, ned {a['dd']:.0f} % från 60-dagars "
                f"topp ({a['high60']:.2f}).")
    return (
        f"{head}\n<b>{html.escape(name)}</b> ({html.escape(symbol)})\n{body}\n\n"
        f"<b>Att tänka på:</b>\n"
        f"1. Detta är en frivillig nedsidesvakt – momentumkärnan är månadsvis.\n"
        f"2. Följer du trendexit: agera mekaniskt, inte på känsla. Halvera eller "
        f"sälj enligt din förutbestämda regel.\n"
        f"3. Whipsaw är priset för skyddet – ibland vänder den upp igen direkt.\n\n"
        f"<i>Larm, ej order. Kursdata ~15 min fördröjd; MA-brott bedöms på dagsstängning.</i>"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Börsvakt – nedsidesvakt (MA-exit på innehav)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg_e = cfg.get("exit_watch", {})
    if not cfg_e.get("enabled", False):
        print("exit_watch: avstängd i config.yaml.")
        return 0

    dd_trigger = float(cfg_e.get("drawdown_pct", 20))
    state = load_state()
    held_by_market = state.get("stock_portfolio", {})
    if not held_by_market and not load_holdings():
        print("Inga innehav (varken i state eller holdings.csv).")
        return 0

    state.setdefault("exit_alerts", {})
    week = dt.date.today().isocalendar()
    wk = f"{week[0]}-W{week[1]:02d}"

    # Plocka in: motorernas innehav + config-extra + dina egna i holdings.csv.
    # OBS: listorna SLÅS IHOP per marknadsnyckel – extra_holdings får aldrig
    # skugga (ersätta) en hel motorportfölj med samma namn.
    extra = cfg_e.get("extra_holdings", {}) or {}
    universe: dict[str, str] = {}
    for source in (held_by_market, extra):
        for mkt, tickers in source.items():
            for t in tickers:
                universe[t] = t  # namn = ticker som fallback
    for h in load_holdings():
        universe[h["ticker"]] = h["ticker"]

    for symbol in sorted(universe):
        try:
            a = analyse(symbol)
            if not a:
                # Transparens: ett innehav utan data står UTAN nedsidesbevakning
                # just nu – det ska synas i Actions-loggen, inte försvinna tyst.
                print(f"  {symbol}: ingen kursdata – nedsidesvakt ej utvärderad.",
                      file=sys.stderr)
                continue
            # Välj starkaste utlösta nivån (200 > 50 > drawdown)
            level = None
            if a["sma200"] is not None and a["price"] < a["sma200"]:
                level = "sma200"
            elif a["price"] < a["sma50"]:
                level = "sma50"
            elif a["dd"] <= -dd_trigger:
                level = "drawdown"
            if not level:
                continue
            key = f"{symbol}:{level}:{wk}"
            if state["exit_alerts"].get(key):
                continue
            if send_telegram(build_exit_alert(universe[symbol], symbol, a, level, dd_trigger),
                             args.dry_run):
                state["exit_alerts"][key] = True   # markera skickat först vid bekräftad leverans
        except Exception as exc:
            print(f"  {symbol}: exit-fel: {exc}", file=sys.stderr)

    # Trimma gamla larmnycklar (behåll innevarande + förra veckan)
    prev = (dt.date.today() - dt.timedelta(weeks=1)).isocalendar()
    keep = {wk, f"{prev[0]}-W{prev[1]:02d}"}
    state["exit_alerts"] = {k: v for k, v in state["exit_alerts"].items()
                            if k.rsplit(":", 1)[-1] in keep}
    if not args.dry_run:
        save_state(state)
    print("Nedsidesvakt klar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
