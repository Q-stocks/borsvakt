#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – Sektortrend-vakt (modul 14)

sectors.py ROTERAR mellan sektorer månadsvis. Den här modulen VARNAR dagligen
när en sektor VÄNDER – upp eller ner – med erkända tekniska system:

  • 200-dagars glidande medel: pris återtar / faller under MA200 (den mest
    bevakade långsiktiga trendlinjen).
  • Guldkors / Dödskors: 50-dagars MA korsar över / under 200-dagars MA
    (de mest namnkunniga trendvändningssignalerna).
  • Relativ styrka mot index: sektorn börjar över- eller underprestera mot
    marknaden (kärnan i sektorrotation / RRG).

Larmar BARA när status faktiskt skiftar (jämför mot sparat läge), så ingen
spam. Första körningen lär in baslinjen tyst.

Sverige saknar likvida sektor-ETF:er, så sektorindex byggs som likaviktade
korgar av aktier vi redan trackar. Samma metod för USA (robust, slipper
overifierade sektor-ETF:er).

Ärligt: guld-/dödskors är ERKÄNDA men EFTERSLÄPANDE och whippy som
fristående signaler. 200d-korsningen och relativ styrka är mer användbara
för rotation. Behandla som varningar att titta närmare på, inte order.

Körning:  python sectortrend.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import sys

import pandas as pd
import yaml

from scanner import CONFIG_FILE, load_state, save_state, send_telegram

BENCH = {"US": "SXR8.DE", "SE": "^OMX"}


def fetch_daily(symbol: str, cache: dict):
    if symbol in cache:
        return cache[symbol]
    import yfinance as yf
    try:
        h = yf.Ticker(symbol).history(period="18mo", interval="1d", auto_adjust=True)
        s = h["Close"].dropna() if (h is not None and not h.empty) else None
        # Normalisera bort tidszon så US (America/New_York) och EU-bench
        # (Europe/Berlin) aligneras på kalenderdatum vid join (annars 0 rader).
        if s is not None:
            s.index = s.index.tz_localize(None)
    except Exception as exc:
        print(f"  {symbol}: {exc}", file=sys.stderr)
        s = None
    cache[symbol] = s
    return s


def composite(members: list[str], cache: dict) -> pd.Series | None:
    """Likaviktat, normaliserat sektorindex från medlemsaktierna."""
    cols = []
    for t in members:
        s = fetch_daily(t, cache)
        if s is not None and len(s) >= 200:
            cols.append(s)
    if not cols:
        return None
    # Konkatenera RÅA serier och skär till gemensamt fönster FÖRST,
    # rebasa sedan – annars går längre historik in över 1.0 (ej likaviktat).
    df = pd.concat(cols, axis=1).dropna()
    if len(df) < 200:
        return None
    df = df / df.iloc[0]
    return df.mean(axis=1)


def status_of(comp: pd.Series, bench: pd.Series | None) -> dict:
    price = float(comp.iloc[-1])
    sma50 = float(comp.tail(50).mean())
    sma200 = float(comp.tail(200).mean())
    st = {"above200": price > sma200, "gcross": sma50 > sma200, "rs_up": None}
    if bench is not None:
        joined = pd.concat([comp, bench], axis=1, sort=False).dropna()
        if len(joined) >= 60:
            ratio = joined.iloc[:, 0] / joined.iloc[:, 1]
            st["rs_up"] = float(ratio.iloc[-1]) > float(ratio.tail(50).mean())
    return st


def transitions(name, market, old: dict, new: dict) -> list[str]:
    out = []
    if old.get("above200") is not None and new["above200"] != old["above200"]:
        out.append("🟢 Vänder UPP – återtog 200-dagars MA" if new["above200"]
                   else "🔴 Vänder NER – föll under 200-dagars MA")
    if old.get("gcross") is not None and new["gcross"] != old["gcross"]:
        out.append("⭐ GULDKORS – 50-dagars MA korsade över 200-dagars"
                   if new["gcross"] else "💀 DÖDSKORS – 50-dagars MA korsade under 200-dagars")
    if old.get("rs_up") is not None and new.get("rs_up") is not None and new["rs_up"] != old["rs_up"]:
        out.append("📈 Börjar överprestera mot index (relativ styrka vänder upp)"
                   if new["rs_up"] else "📉 Börjar underprestera mot index")
    return out


def build_alert(name, market, new: dict, changes: list[str]) -> str:
    badge = []
    badge.append("över 200d MA ✅" if new["above200"] else "under 200d MA ⛔")
    if new["gcross"] is not None:
        badge.append("50>200" if new["gcross"] else "50<200")
    if new.get("rs_up") is not None:
        badge.append("RS upp" if new["rs_up"] else "RS ner")
    return (
        f"🏭 <b>SEKTORTREND – {html.escape(name)}</b> [{market}]\n"
        + "\n".join("• " + c for c in changes)
        + f"\n\nStatus nu: {', '.join(badge)}.\n"
        f"<i>Erkända trendsignaler (200d MA, guld-/dödskors, relativ styrka). "
        f"De släpar och kan whippa – varning att titta närmare på, ej order. "
        f"Ej rådgivning.</i>"
    )


def process(cfg_t: dict, state: dict, dry: bool) -> None:
    cache: dict = {}
    store = state.setdefault("sector_trend", {})
    benches = {m: fetch_daily(BENCH[m], cache) for m in ("US", "SE")}

    for grp in cfg_t.get("groups", []):
        name, market = grp["name"], grp.get("market", "SE")
        members = grp.get("members", [])
        comp = composite(members, cache)
        if comp is None:
            print(f"  {name}: otillräcklig data, hoppar.", file=sys.stderr)
            continue
        new = status_of(comp, benches.get(market))
        new["updated"] = dt.date.today().isoformat()
        old = store.get(name, {})

        ok = True
        if old:  # inte första gången -> leta vändningar
            changes = transitions(name, market, old, new)
            if changes:
                ok = send_telegram(build_alert(name, market, new, changes), dry)
        # Flytta baslinjen bara om ev. larm levererades – annars står vändningen
        # kvar och re-detekteras nästa körning (tappa aldrig en vändning på 429/5xx).
        if ok:
            store[name] = new


def main() -> int:
    ap = argparse.ArgumentParser(description="Börsvakt – Sektortrend-vakt")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg_t = cfg.get("sector_trend", {})
    if not cfg_t.get("enabled", False):
        print("sector_trend: avstängd i config.yaml.")
        return 0

    state = load_state()
    try:
        process(cfg_t, state, args.dry_run)
    except Exception as exc:
        print(f"sector_trend: fel: {exc}", file=sys.stderr)
    if not args.dry_run:
        save_state(state)
    print("Sektortrend-koll klar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
