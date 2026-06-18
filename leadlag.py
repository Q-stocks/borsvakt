#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – Lead-Lag (modul 13): värdekedje-spridning ovanpå PEAD.

EXPERIMENTELL. Bygger på Cohen-Frazzini-effekten (kund-leverantör lead-lag),
men ärligt: det är PEAD i ett extra led, effekten har försvagats efter
publicering och fungerar inte för stora bolag. Därför körs den som ett
LOGGAT EXPERIMENT — varje signal hamnar i larmloggen och utvärderas
out-of-sample innan den får styra riktiga pengar.

Arbetsdelningen (det enda hederliga givet datan):
  • DU (med AI-hjälp) bygger länkkartan i config: vilka nedströmsbolag som
    gynnas när en "kanonfågel" (leader) levererar. Datorn kan inte gissa
    detta utan att hamna i data-mining-fällan.
  • SYSTEMET automatiserar resten: när en leader får ett PEAD-larm (som vi
    redan fångar) skickar modulen ett experimentellt bevakningslarm på de
    mappade nedströmsbolagen som ännu inte rört sig — och loggar dem.
  • Saknar en leader mappade följare skickas i stället en RESEARCH-NUDGE:
    "researcha vilka som gynnas och lägg till dem." Det är din loop.

Fungerar för både SE och US (marknad anges per länk).

Körning:  python leadlag.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import sys

import yaml

from alertlog import log_alert
from scanner import CONFIG_FILE, load_state, save_state, send_telegram


def fresh_leaders(state: dict, window_days: int) -> dict:
    """Leaders som fått ett PEAD-larm (finns i drift_portfolio) de senaste
    `window_days` dagarna. Kräver att leadern ligger i PEAD-universumet."""
    today = dt.date.today()
    out = {}
    for market, holds in (state.get("drift_portfolio") or {}).items():
        for sym, info in holds.items():
            stamp = info.get("entry_logged") or info.get("report")
            try:
                entered = dt.date.fromisoformat(str(stamp)[:10])
            except (ValueError, TypeError):
                continue
            if 0 <= (today - entered).days <= window_days:
                out[sym] = {"market": market, "entry": entered.isoformat(),
                            "surprise": info.get("surprise"), "reaction": info.get("reaction")}
    return out


def recent_move_pct(symbol: str, days: int = 20) -> float | None:
    """Följarens avkastning de senaste `days` handelsdagarna (för att hoppa
    över bolag som redan rört sig — vi vill in FÖRE breakouten)."""
    import yfinance as yf
    h = yf.Ticker(symbol).history(period="2mo", interval="1d", auto_adjust=True)
    if h is None or len(h) < days + 1:
        return None
    c = h["Close"].dropna()
    return float(c.iloc[-1] / c.iloc[-days - 1] - 1.0) * 100.0


def build_follower_alert(leader, follower, link, ev, moved) -> str:
    drivers = []
    if ev.get("surprise") is not None:
        drivers.append(f"vinstöverr. {ev['surprise']:+.0f}%")
    if ev.get("reaction") is not None:
        drivers.append(f"reaktion {ev['reaction']:+.0f}%")
    dl = (" (" + ", ".join(drivers) + ")") if drivers else ""
    mv = ""
    if moved is not None:
        mv = f"\n{html.escape(follower)} har rört sig {moved:+.0f}% på 20 dagar — {'fortfarande lugnt' if abs(moved) < 6 else 'börjar röra på sig'}."
    return (
        f"🔗 <b>LEAD-LAG (experimentell)</b>\n"
        f"<b>{html.escape(leader)}</b> fick ett PEAD-larm{dl}.\n"
        f"Nedströmskandidat: <b>{html.escape(follower)}</b> "
        f"<span>[{html.escape(link.get('market','SE'))}]</span>\n"
        f"Tes: {html.escape(link.get('thesis','värdekedjekoppling'))}.{mv}\n\n"
        f"<i>Hypotes under test — loggas och utvärderas out-of-sample. "
        f"Detta är PEAD i ett extra led och en försvagad effekt; behandla som "
        f"experiment, inte signal. Snäva stoppar. Ej rådgivning.</i>"
    )


def build_research_nudge(leader, link, ev) -> str:
    return (
        f"🔗🔍 <b>LEAD-LAG: research behövs</b>\n"
        f"<b>{html.escape(leader)}</b> [{html.escape(link.get('market','SE'))}] fick ett "
        f"PEAD-larm, men du har inga nedströmsbolag mappade.\n"
        f"Tes: {html.escape(link.get('thesis','värdekedjekoppling'))}.\n\n"
        f"Researcha (gärna med AI) vilka bolag som gynnas 1–2 kvartal senare och "
        f"lägg dem under <code>leadlag.links → followers</code> för {html.escape(leader)}. "
        f"Då börjar systemet bevaka och logga dem automatiskt nästa gång."
    )


def process(cfg_l: dict, state: dict, dry: bool) -> None:
    window = int(cfg_l.get("trigger_window_days", 7))
    skip_pct = float(cfg_l.get("skip_if_follower_moved_pct", 12))
    links = {l["leader"]: l for l in cfg_l.get("links", []) if l.get("leader")}
    fired = state.setdefault("leadlag_fired", {})

    leaders = fresh_leaders(state, window)
    for lead_sym, ev in leaders.items():
        link = links.get(lead_sym)
        if not link:
            continue  # leadern är inte en mappad kanonfågel
        base = f"{lead_sym}:{ev['entry']}"
        followers = [f for f in (link.get("followers") or []) if f]

        if not followers:
            k = f"nudge:{base}"
            if not fired.get(k) and send_telegram(build_research_nudge(lead_sym, link, ev), dry):
                fired[k] = True   # markera skickat först vid bekräftad leverans
            continue

        for fol in followers:
            k = f"{lead_sym}->{fol}:{ev['entry']}"
            if fired.get(k):
                continue
            moved = None
            try:
                moved = recent_move_pct(fol)
            except Exception:
                moved = None
            # Hoppa över om följaren redan rusat (vi vill in före breakouten)
            if moved is not None and abs(moved) >= skip_pct:
                fired[k] = True
                continue
            if send_telegram(build_follower_alert(lead_sym, fol, link, ev, moved), dry):
                log_alert("leadlag", fol, "follower_watch",
                          market=link.get("market", "SE"),
                          meta={"leader": lead_sym, "thesis": link.get("thesis", "")}, dry=dry)
                fired[k] = True   # logga + markera först vid bekräftad leverans


def main() -> int:
    ap = argparse.ArgumentParser(description="Börsvakt – Lead-Lag (experimentell)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg_l = cfg.get("leadlag", {})
    if not cfg_l.get("enabled", False):
        print("leadlag: avstängd i config.yaml.")
        return 0

    state = load_state()
    try:
        process(cfg_l, state, args.dry_run)
    except Exception as exc:
        print(f"leadlag: fel: {exc}", file=sys.stderr)
    # Trimma gammal fired-historik (behåll ~200 senaste nycklar)
    if len(state.get("leadlag_fired", {})) > 200:
        state["leadlag_fired"] = dict(list(state["leadlag_fired"].items())[-200:])
    if not args.dry_run:
        save_state(state)
    print("Lead-lag klar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
