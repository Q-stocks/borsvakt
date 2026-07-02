#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – bevakar din aktielista och skickar Telegram-notiser vid:

  1. Volymspikar (RVOL, tidsjusterad mot 20-dagars snitt) + prisriktning
  2. Stora dagsrörelser
  3. Egna prisnivåer (över/under)
  4. Nya pressmeddelanden (RSS) – med valfri Claude-sammanfattning

Designprincip: systemet hittar LÄGEN värda att titta på och förklarar
varför. Det ger en regelbaserad checklista – inte låtsas-köpsignaler.

Körning:
  python scanner.py              # skarp körning (skickar Telegram)
  python scanner.py --dry-run    # skriver notiser till terminalen i stället
  python scanner.py --force      # kör även utanför börstid
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sys
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

from alertlog import log_alert

# Windows: säkerställ UTF-8 på stdout så att emojis/svenska tecken inte kraschar
# print (alla moduler som importerar scanner ärver detta).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

STOCKHOLM = ZoneInfo("Europe/Stockholm")
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
CONFIG_FILE = ROOT / "config.yaml"

# Börstider (lokal tid för respektive marknad)
MARKET_HOURS = {
    "SE": {"tz": ZoneInfo("Europe/Stockholm"), "open": (9, 0), "close": (17, 30)},
    "US": {"tz": ZoneInfo("America/New_York"), "open": (9, 30), "close": (16, 0)},
}


# ----------------------------------------------------------------------
# Hjälpfunktioner
# ----------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_holdings() -> list[dict]:
    """Läser holdings.csv (dina innehav) = sanningskällan för 'aktier jag äger'.
    market härleds från .ST om tom. shares/entry_price/entry_date är valfria."""
    import csv as _csv
    p = ROOT / "holdings.csv"
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8-sig") as fh:
        for r in _csv.DictReader(fh):
            low = {k.lower().strip(): (v or "").strip() for k, v in r.items() if k}
            t = low.get("ticker", "")
            if not t:
                continue
            mkt = low.get("market") or ("SE" if t.upper().endswith(".ST") else "US")
            def num(k):
                try:
                    return float(low[k].replace(",", "."))
                except (ValueError, KeyError, AttributeError):
                    return None
            out.append({"ticker": t, "market": mkt.upper(),
                        "shares": num("shares"), "entry_price": num("entry_price"),
                        "entry_date": low.get("entry_date") or None,
                        "note": low.get("note") or ""})
    return out


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"alerts": {}, "seen_news": {}}


def save_state(state: dict) -> None:
    # Härdning: alla moduler delar state.json – säkerställ baskälarna finns
    # så att en modul som sparar state aldrig kraschar på saknad nyckel.
    state.setdefault("alerts", {})
    state.setdefault("seen_news", {})
    # Rensa larmnycklar äldre än 7 dagar så filen inte växer i evighet.
    cutoff = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    state["alerts"] = {k: v for k, v in state["alerts"].items()
                       if isinstance(v, str) and v >= cutoff}
    # Behåll de 50 senaste per bolag; släng tomma nycklar (setdefault återskapar
    # dem vid behov – annars växer dicten med varje symbol som någonsin skannats).
    state["seen_news"] = {sym: ids[-50:] for sym, ids in state["seen_news"].items()
                          if isinstance(ids, list) and ids}
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def market_is_open(market: str, now_utc: dt.datetime) -> bool:
    spec = MARKET_HOURS.get(market, MARKET_HOURS["SE"])
    local = now_utc.astimezone(spec["tz"])
    if local.weekday() >= 5:  # lör/sön
        return False
    o, c = spec["open"], spec["close"]
    t = (local.hour, local.minute)
    return o <= t <= c


def elapsed_fraction(market: str, now_utc: dt.datetime) -> float:
    """Hur stor andel av handelsdagen som passerat (0.05–1.0)."""
    spec = MARKET_HOURS.get(market, MARKET_HOURS["SE"])
    local = now_utc.astimezone(spec["tz"])
    open_min = spec["open"][0] * 60 + spec["open"][1]
    close_min = spec["close"][0] * 60 + spec["close"][1]
    now_min = local.hour * 60 + local.minute
    frac = (now_min - open_min) / (close_min - open_min)
    return max(0.05, min(1.0, frac))


def already_alerted(state: dict, key: str) -> bool:
    return state["alerts"].get(key) == dt.date.today().isoformat()


def mark_alerted(state: dict, key: str) -> None:
    state["alerts"][key] = dt.date.today().isoformat()


# ----------------------------------------------------------------------
# Kursdata (Yahoo Finance, ~15 min fördröjd för Stockholmsbörsen)
# ----------------------------------------------------------------------

def fetch_metrics(symbol: str, market: str, now_utc: dt.datetime) -> dict | None:
    """Hämtar pris, dagsförändring och tidsjusterad relativ volym (RVOL)."""
    import yfinance as yf

    hist = yf.Ticker(symbol).history(period="2mo", interval="1d", auto_adjust=False)
    if hist is None or len(hist) < 22:
        return None

    today = hist.index[-1].date()
    is_today = today == now_utc.astimezone(MARKET_HOURS.get(market, MARKET_HOURS["SE"])["tz"]).date()

    last = hist.iloc[-1]
    prev = hist.iloc[-2]
    prior = hist.iloc[:-1] if is_today else hist  # exkludera dagens (partiella) rad

    avg_vol_20 = float(prior["Volume"].tail(20).mean())
    high_20 = float(prior["High"].tail(20).max())

    price = float(last["Close"])
    prev_close = float(prev["Close"])
    pct = (price / prev_close - 1.0) * 100.0 if prev_close else 0.0

    vol_today = float(last["Volume"]) if is_today else 0.0
    frac = elapsed_fraction(market, now_utc) if is_today else 1.0
    expected = avg_vol_20 * frac
    rvol = (vol_today / expected) if expected > 0 else 0.0

    return {
        "price": price,
        "prev_close": prev_close,
        "pct": pct,
        "vol_today": vol_today,
        "avg_vol_20": avg_vol_20,
        "rvol": rvol,
        "high_20": high_20,
        "is_today": is_today,
    }


# ----------------------------------------------------------------------
# Nyheter (RSS) + valfri Claude-sammanfattning
# ----------------------------------------------------------------------

def fetch_news(rss_url: str, seen_ids: list[str]) -> list[dict]:
    import feedparser

    feed = feedparser.parse(rss_url)
    fresh = []
    for entry in feed.entries[:10]:
        eid = entry.get("id") or entry.get("link") or entry.get("title", "")
        if not eid or eid in seen_ids:
            continue
        fresh.append(
            {
                "id": eid,
                "title": entry.get("title", "(utan rubrik)"),
                "link": entry.get("link", ""),
                "summary": entry.get("summary", "")[:1500],
                "published": entry.get("published", ""),
            }
        )
    return fresh


def claude_summarize(item: dict, model: str) -> str | None:
    """Ber Claude om en 2-meningarssummering + bedömd kurspåverkan."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=300,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Här är ett pressmeddelande från ett börsbolag.\n\n"
                        f"Rubrik: {item['title']}\n\nText: {item['summary']}\n\n"
                        "Sammanfatta på svenska i max 2 meningar. Avsluta med en rad: "
                        "'Sannolik kurspåverkan: låg/medel/hög – <kort motivering>'. "
                        "Var nykter och spekulera inte bortom texten."
                    ),
                }
            ],
        )
        return "".join(b.text for b in msg.content if b.type == "text").strip()
    except Exception as exc:  # API-fel ska aldrig stoppa larmet
        print(f"  (Claude-sammanfattning misslyckades: {exc})", file=sys.stderr)
        return None


# ----------------------------------------------------------------------
# Larmtexter – med transparent "varför" och checklista
# ----------------------------------------------------------------------

def fmt_price(p: float) -> str:
    return f"{p:,.2f}".replace(",", " ")


def checklist_volume(pct: float, has_fresh_news: bool) -> str:
    rows = []
    if has_fresh_news:
        rows.append("1. Läs PM:et nedan – det är sannolikt drivkraften.")
        rows.append("2. Order/avtal med belopp &gt; vaga avsiktsförklaringar.")
        rows.append("3. Stark nyhet + stigande kurs på hög volym = mest hållbara mönstret.")
    else:
        rows.append("1. Inget PM hittat → kolla mfn.se och bolagets sajt manuellt.")
        rows.append("2. Spik utan nyhet är ofta en blockaffär eller flöde som klingar av.")
        rows.append("3. Jaga inte – sätt i så fall nivå i config och invänta bekräftelse.")
    if pct < 0:
        rows.append("4. OBS: volymen kommer på NEDSIDAN – spikar utlöser fall lika ofta som uppgångar.")
    return "\n".join(rows)


def build_volume_alert(name, symbol, m, news_items, summary) -> str:
    arrow = "📈" if m["pct"] >= 0 else "📉"
    breakout = ""
    if m["price"] > m["high_20"]:
        breakout = f"\n• Bryter 20-dagarshögsta ({fmt_price(m['high_20'])})"
    news_block = ""
    if news_items:
        n = news_items[0]
        news_block = f"\n\n📰 <b>{html.escape(n['title'])}</b>\n{html.escape(n['link'])}"
        if summary:
            news_block += f"\n<i>{html.escape(summary)}</i>"
    return (
        f"🔔 <b>{html.escape(name)}</b> ({html.escape(symbol)}) – VOLYMSPIK {arrow}\n"
        f"• Kurs: {fmt_price(m['price'])} ({m['pct']:+.1f}%)\n"
        f"• Volym: {m['rvol']:.1f}× normalt för tidpunkten"
        f"{breakout}"
        f"{news_block}\n\n"
        f"<b>Checklista:</b>\n{checklist_volume(m['pct'], bool(news_items))}\n\n"
        f"<i>Heuristik, ej köpråd. Kursdata ~15 min fördröjd.</i>"
    )


def build_move_alert(name, symbol, m) -> str:
    arrow = "📈" if m["pct"] >= 0 else "📉"
    return (
        f"🔔 <b>{html.escape(name)}</b> ({html.escape(symbol)}) – STOR DAGSRÖRELSE {arrow}\n"
        f"• Kurs: {fmt_price(m['price'])} ({m['pct']:+.1f}%)\n"
        f"• Volym: {m['rvol']:.1f}× normalt\n\n"
        f"<b>Checklista:</b>\n"
        f"1. Rörelse UTAN volym (&lt;1,5×) är skör – låg informationshalt.\n"
        f"2. Kolla PM/nyheter innan du agerar.\n\n"
        f"<i>Heuristik, ej köpråd.</i>"
    )


def build_level_alert(name, symbol, m, level) -> str:
    riktning = "över" if level["dir"] == "over" else "under"
    return (
        f"🎯 <b>{html.escape(name)}</b> ({html.escape(symbol)}) – NIVÅ TRÄFFAD\n"
        f"• Kurs {fmt_price(m['price'])} är {riktning} din nivå {fmt_price(level['price'])}\n"
        f"• Din anteckning: {html.escape(str(level.get('note', '–')))}\n\n"
        f"<i>Du satte nivån i lugnt läge – lita på den planen, inte på pulsen nu.</i>"
    )


def build_news_alert(name, symbol, item, summary) -> str:
    out = (
        f"📰 <b>{html.escape(name)}</b> ({html.escape(symbol)}) – NYTT PM\n"
        f"<b>{html.escape(item['title'])}</b>\n{html.escape(item['link'])}"
    )
    if summary:
        out += f"\n\n<i>{html.escape(summary)}</i>"
    return out


# ----------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------

def send_telegram(text: str, dry_run: bool) -> bool:
    # Returnerar True när notisen är levererad (eller dry_run), annars False –
    # anroparen markerar PM som "sett" först EFTER bekräftad leverans.
    text = text[:4096]  # Telegram tillåter max 4096 tecken per meddelande
    if dry_run:
        print("\n" + "=" * 60 + "\n[DRY RUN] Skulle skicka:\n" + text + "\n")
        return True
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("VARNING: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID saknas – hoppar över.", file=sys.stderr)
        print(text)
        return False
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": False},
        timeout=20,
    )
    if not resp.ok:
        print(f"Telegram-fel {resp.status_code}: {resp.text}", file=sys.stderr)
        return False
    return True


# ----------------------------------------------------------------------
# Huvudloop
# ----------------------------------------------------------------------

def process_ticker(tk: dict, cfg: dict, state: dict, now_utc: dt.datetime,
                   dry_run: bool, force: bool) -> None:
    symbol = tk["symbol"]
    name = tk.get("name", symbol)
    market = tk.get("market", "SE")
    th = cfg.get("thresholds", {})
    today = dt.date.today().isoformat()

    # --- Nyheter kollas alltid (PM kommer ofta före börsöppning) ---
    news_items: list[dict] = []
    seen: list[str] = state["seen_news"].setdefault(symbol, [])
    if tk.get("rss"):
        try:
            news_items = fetch_news(tk["rss"], seen)
            # OBS: markera INTE som sett här – ett PM räknas som "sett" först
            # efter att dess notis bekräftats skickad (annars tappas PM vid
            # misslyckad Telegram-leverans).
            if not seen and news_items:
                # Första körningen mot en nyaktiverad feed: lär in historiken
                # tyst i stället för att larma gamla PM (samma mönster som
                # insiders first_run).
                seen.extend(item["id"] for item in news_items)
                print(f"  {symbol}: ny RSS-feed – lärde in {len(news_items)} "
                      f"historiska PM utan larm.")
                news_items = []
        except Exception as exc:
            print(f"  {symbol}: RSS-fel: {exc}", file=sys.stderr)

    # --- Kursdata bara under börstid (annars är RVOL meningslös) ---
    m = None
    if force or market_is_open(market, now_utc):
        try:
            m = fetch_metrics(symbol, market, now_utc)
        except Exception as exc:
            print(f"  {symbol}: kursdata-fel: {exc}", file=sys.stderr)

    summarize_cfg = cfg.get("claude_summary", {})
    do_summary = summarize_cfg.get("enabled", False)
    model = summarize_cfg.get("model", "claude-haiku-4-5-20251001")

    sent_volume_alert = False
    if m and m["is_today"]:
        # 1) Volymspik + prisrörelse
        key = f"{symbol}:rvol:{today}"
        if (m["avg_vol_20"] >= th.get("min_avg_volume", 0)
                and m["rvol"] >= th.get("rvol_trigger", 3)
                and abs(m["pct"]) >= th.get("rvol_min_price_move", 1.5)
                and not already_alerted(state, key)):
            summary = claude_summarize(news_items[0], model) if (news_items and do_summary) else None
            if send_telegram(build_volume_alert(name, symbol, m, news_items, summary), dry_run):
                # Det inbakade PM:et (news_items[0]) räknas som sett först nu.
                if news_items:
                    seen.append(news_items[0]["id"])
                log_alert("scanner", symbol, "volume_spike", market=market,
                          price=m["price"], meta={"rvol": round(m["rvol"], 1),
                          "pct": round(m["pct"], 1)}, dry=dry_run)
                mark_alerted(state, key)
                sent_volume_alert = True
                news_items = news_items[1:]  # PM:et är redan med i volymlarmet; resten flödar vidare

        # 2) Stor dagsrörelse (om inte volymlarmet redan täckt dagen,
        #    i den här körningen ELLER en tidigare körning samma dag)
        key = f"{symbol}:move:{today}"
        if (not sent_volume_alert
                and not already_alerted(state, f"{symbol}:rvol:{today}")
                and abs(m["pct"]) >= th.get("big_move_pct", 6)
                and not already_alerted(state, key)):
            # Markera skickat först vid bekräftad leverans (annars tappas larmet).
            if send_telegram(build_move_alert(name, symbol, m), dry_run):
                log_alert("scanner", symbol, "big_move", market=market,
                          price=m["price"], meta={"pct": round(m["pct"], 1)}, dry=dry_run)
                mark_alerted(state, key)

        # 3) Egna prisnivåer
        for level in tk.get("levels", []) or []:
            key = f"{symbol}:level:{level['price']}:{level['dir']}:{today}"
            hit = (m["price"] >= level["price"] if level["dir"] == "over"
                   else m["price"] <= level["price"])
            if hit and not already_alerted(state, key):
                if send_telegram(build_level_alert(name, symbol, m, level), dry_run):
                    log_alert("scanner", symbol, "level_hit", market=market,
                              price=m["price"],
                              meta={"level": level["price"], "dir": level["dir"]},
                              dry=dry_run)
                    mark_alerted(state, key)

    # 4) Rena PM-larm (de som inte bakats in i ett volymlarm)
    for item in news_items:
        summary = claude_summarize(item, model) if do_summary else None
        if send_telegram(build_news_alert(name, symbol, item, summary), dry_run):
            # Markera som sett först efter bekräftad leverans.
            seen.append(item["id"])
            log_alert("scanner", symbol, "news", market=market, dry=dry_run)


def main() -> int:
    ap = argparse.ArgumentParser(description="Börsvakt – aktielarm till Telegram")
    ap.add_argument("--dry-run", action="store_true", help="skriv ut i stället för att skicka")
    ap.add_argument("--force", action="store_true", help="kör även utanför börstid")
    args = ap.parse_args()

    cfg = load_config()
    state = load_state()
    now_utc = dt.datetime.now(dt.timezone.utc)

    any_market_open = any(
        market_is_open(t.get("market", "SE"), now_utc) for t in cfg.get("tickers", [])
    )
    if not any_market_open and not args.force:
        print("Alla marknader stängda – kollar bara nyhetsflöden.")

    for tk in cfg.get("tickers", []):
        try:
            process_ticker(tk, cfg, state, now_utc, args.dry_run, args.force)
        except Exception:
            print(f"FEL för {tk.get('symbol')}:", file=sys.stderr)
            traceback.print_exc()

    # Portföljmedvetenhet: skanna även Aktiemotorns innehav för volym/pris
    # (utan RSS – dessa larmar på rörelse, inte nyheter, om de saknar flöde).
    if cfg.get("scan_holdings", True):
        config_syms = {t["symbol"] for t in cfg.get("tickers", [])}
        seen_extra = set()
        # (a) motorernas innehav + (b) dina egna i holdings.csv
        owned = {(h["ticker"], h["market"]) for h in load_holdings()}
        from_engine = {(sym, "US" if mkt.upper() == "USA" else "SE")
                       for mkt, tickers in state.get("stock_portfolio", {}).items()
                       for sym in tickers}
        for sym, market in (owned | from_engine):
            if sym in config_syms or sym in seen_extra:
                continue
            seen_extra.add(sym)
            holding = {"symbol": sym, "name": f"{sym} (innehav)", "market": market}
            try:
                process_ticker(holding, cfg, state, now_utc, args.dry_run, args.force)
            except Exception:
                print(f"FEL för innehav {sym}:", file=sys.stderr)
                traceback.print_exc()

    if not args.dry_run:
        save_state(state)   # dry-run ska aldrig bränna seen_news/alerts mot riktig state.json
    print("Klar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
