#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – Sektorrotation (modul 9, rekonstruerad)

Momentum applicerat på sektorer: ranka en korg sektorer på Sammansatt momentum
(snitt av 3/6/12-månadersavkastning) och håll topp N som ligger i uppåttrend
(över 10-mån glidande medel). Ett mekaniskt sätt att ligga i de ledande
sektorerna utan att gissa varför.

Sektorindex byggs antingen från:
  • config sectors.groups  – likaviktade korgar av aktier (rekommenderat;
    Sverige saknar likvida sektor-ETF:er), ELLER
  • config sectors.assets  – sektor-ETF:er (ett ticker per sektor).

Rankningen persisteras i state['sectors'] för dashboardens cockpit. Saknad data
hoppas tyst över.

Körning:  python sectors.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import sys

import pandas as pd
import yaml

from scanner import CONFIG_FILE, load_state, save_state, send_telegram


def _monthly(symbol: str, cache: dict):
    """Månadsstängningar, tz-naiva, snappade till månadsslut, exkl. innevarande."""
    if symbol in cache:
        return cache[symbol]
    import yfinance as yf
    try:
        h = yf.Ticker(symbol).history(period="3y", interval="1mo", auto_adjust=True)
        s = h["Close"].dropna() if (h is not None and not h.empty) else None
        if s is not None and len(s):
            s.index = s.index.tz_localize(None).to_period("M").to_timestamp("M")
            s = s[~s.index.duplicated(keep="last")]
            cur = pd.Period(dt.date.today(), freq="M")
            s = s[s.index.to_period("M") < cur]
    except Exception as exc:
        print(f"  {symbol}: {exc}", file=sys.stderr)
        s = None
    cache[symbol] = s
    return s


def composite(members: list[str], cache: dict):
    """Likaviktat månadsindex (rebasat vid gemensam start) från medlemmarna."""
    cols = []
    for t in members:
        s = _monthly(t, cache)
        if s is not None and len(s) >= 13:
            cols.append(s)
    if not cols:
        return None
    df = pd.concat(cols, axis=1, sort=False).dropna()
    if len(df) < 13:
        return None
    df = df / df.iloc[0]
    return df.mean(axis=1)


def _ret(closes, months: int) -> float:
    return float(closes.iloc[-1] / closes.iloc[-1 - months] - 1.0)


def score_unit(name: str, closes) -> dict | None:
    if closes is None or len(closes) < 13:
        return None
    r3, r6, r12 = _ret(closes, 3), _ret(closes, 6), _ret(closes, 12)
    sma10 = float(closes.iloc[-10:].mean())
    return {"name": name, "r3": r3, "r6": r6, "r12": r12,
            "score": (r3 + r6 + r12) / 3.0,
            "above": float(closes.iloc[-1]) > sma10}


def evaluate(cfg_s: dict) -> list[dict]:
    cache: dict = {}
    units = []
    for g in cfg_s.get("groups", []):
        closes = composite(g.get("members", []), cache)
        u = score_unit(g["name"], closes)
        if u:
            units.append(u)
    for a in cfg_s.get("assets", []):
        closes = _monthly(a["signal"], cache)
        u = score_unit(a.get("name", a["signal"]), closes)
        if u:
            units.append(u)
    units.sort(key=lambda r: r["score"], reverse=True)
    return units


def build_message(units: list[dict], top_n: int, held: list[str]) -> str:
    L = [f"🧭 <b>Sektorrotation – {dt.date.today():%Y-%m}</b>", ""]
    for i, u in enumerate(units, 1):
        flag = "✅" if u["above"] else "⛔"
        pick = "  ◀️ HÅLLS" if u["name"] in held else ""
        L.append(f"{i}. {flag} <b>{html.escape(u['name'])}</b>  "
                 f"3m {u['r3']:+.0%} · 6m {u['r6']:+.0%} · 12m {u['r12']:+.0%}{pick}")
    L.append("")
    if held:
        L.append(f"📌 <b>Håll topp {top_n} i uppåttrend: "
                 f"{', '.join(html.escape(h) for h in held)}</b>")
    else:
        L.append(f"📌 <b>Ingen sektor över 10-mån MA → avvakta.</b>")
    L.append("")
    L.append("<i>Sammansatt momentum (3/6/12 mån). Håll de ledande sektorerna i "
             "uppåttrend. Sektorindex = likaviktade korgar. Ej rådgivning.</i>")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Börsvakt – Sektorrotation")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg_s = cfg.get("sectors", {})
    if not cfg_s.get("enabled", False):
        print("sectors: avstängd i config.yaml.")
        return 0

    top_n = int(cfg_s.get("top_n", 3))
    units = evaluate(cfg_s)
    if not units:
        print("sectors: otillräcklig data för alla sektorer.", file=sys.stderr)
        return 0

    held = [u["name"] for u in units if u["above"]][:top_n]
    delivered = send_telegram(build_message(units, top_n, held), args.dry_run)

    # Dashboard-snapshoten persisteras oavsett leverans (kontextdata) …
    if not args.dry_run:
        state = load_state()
        state["sectors"] = {
            "updated": dt.date.today().isoformat(),
            "held": held,
            "leaders": [{"name": u["name"], "r12": round(u["r12"] * 100, 1),
                         "above": u["above"]} for u in units],
        }
        save_state(state)
    # … men en olevererad månadsnotis failar steget så schemavakten kör om.
    if not delivered:
        print("sectors: månadsnotisen kunde inte levereras – steget failar "
              "för omkörning.", file=sys.stderr)
        return 1
    print("Sektorrotation klar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
