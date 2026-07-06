#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – insiderlarm (modul 2)

USA: SEC EDGAR Form 4 – officiell, gratis och strukturerad data.
  • Larmar på öppna marknadsköp (transaktionskod P).
  • Flaggar KLUSTER när ≥2 olika insiders köpt inom `cluster_window_days`
    – mönstret med starkast stöd i forskningen.
  • Försäljningar (kod S) är avstängda som standard: de är brusiga
    (optionsprogram, skatt, diversifiering) och har lågt signalvärde.

Sverige (FI:s insynsregister): implementeras i Claude Code – se CLAUDE.md.

OBS: SEC kräver en identifierande User-Agent med kontaktuppgift.
Sätt din e-post i config: insiders.sec_user_agent.

Körning:
  python insiders.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import sys
import xml.etree.ElementTree as ET

import requests
import yaml

from alertlog import log_alert
from scanner import CONFIG_FILE, load_state, save_state, send_telegram

SEC_TICKER_MAP = "https://www.sec.gov/files/company_tickers.json"


def _fmt(n: float) -> str:
    return f"{n:,.0f}".replace(",", " ")


def sec_get(url: str, ua: str) -> requests.Response:
    resp = requests.get(url, headers={"User-Agent": ua}, timeout=30)
    resp.raise_for_status()
    return resp


def get_cik(ticker: str, ua: str, cache: dict) -> int | None:
    t = ticker.upper()
    if t in cache:
        return int(cache[t])
    data = sec_get(SEC_TICKER_MAP, ua).json()
    # Cacha BARA den efterfrågade tickern i state – hela SEC-kartan är
    # ~10 000 poster och blåste upp state.json (86 % av filstorleken).
    full = {str(row["ticker"]).upper(): int(row["cik_str"]) for row in data.values()}
    if t in full:
        cache[t] = full[t]
    return full.get(t)


def recent_form4(cik: int, ua: str, limit: int = 40) -> list[dict]:
    sub = sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", ua).json()
    rec = sub.get("filings", {}).get("recent", {})
    out = []
    for form, acc, date, doc in zip(
        rec.get("form", []), rec.get("accessionNumber", []),
        rec.get("filingDate", []), rec.get("primaryDocument", []),
    ):
        # Endast original-Form 4: rättelser (4/A) har eget accession-nummer
        # och skulle ge DUBBLETTLARM för samma transaktion.
        if form == "4":
            out.append({"acc": acc, "date": date, "doc": doc})
            if len(out) >= limit:
                break
    return out


def fetch_form4_xml(cik: int, filing: dict, ua: str) -> str | None:
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{filing['acc'].replace('-', '')}"
    doc = (filing.get("doc") or "").strip()
    if doc.lower().endswith(".xml") and "xsl" not in doc.lower():
        return sec_get(f"{base}/{doc.split('/')[-1]}", ua).text
    # Fallback: leta upp råa form4-XML:en i filningens index
    idx = sec_get(f"{base}/index.json", ua).json()
    for item in idx.get("directory", {}).get("item", []):
        name = item.get("name", "")
        if name.lower().endswith(".xml") and "xsl" not in name.lower():
            return sec_get(f"{base}/{name}", ua).text
    return None


def parse_form4(xml_text: str) -> dict:
    """Plockar ut ägare, roll och köp/sälj-transaktioner ur Form 4-XML."""
    root = ET.fromstring(xml_text)
    owner = (root.findtext(".//rptOwnerName") or "Okänd").title()
    title = root.findtext(".//officerTitle")
    if not title:
        is_dir = (root.findtext(".//isDirector") or "").strip() in ("1", "true")
        title = "Styrelseledamot" if is_dir else "Insider"

    buys, sells = [], []
    for tr in root.findall(".//nonDerivativeTransaction"):
        code = (tr.findtext(".//transactionCode") or "").strip()
        rec = {
            "shares": float(tr.findtext(".//transactionShares/value") or 0),
            "price": float(tr.findtext(".//transactionPricePerShare/value") or 0),
            "date": tr.findtext(".//transactionDate/value") or "",
        }
        if code == "P":
            buys.append(rec)
        elif code == "S":
            sells.append(rec)
    return {"owner": owner, "title": title, "buys": buys, "sells": sells}


def build_buy_alert(ticker: str, parsed: dict, filing_date: str,
                    n_cluster_owners: int, window: int) -> str:
    tot_sh = sum(b["shares"] for b in parsed["buys"])
    tot_val = sum(b["shares"] * b["price"] for b in parsed["buys"])
    avg_px = (tot_val / tot_sh) if tot_sh else 0.0
    cluster = ""
    if n_cluster_owners >= 2:
        cluster = (f"\n🔥 <b>KLUSTER:</b> {n_cluster_owners} olika insiders "
                   f"har anmält köp de senaste {window} dagarna.")
    return (
        f"👤 <b>INSIDERKÖP – {html.escape(ticker)}</b>\n"
        f"{html.escape(parsed['owner'])} ({html.escape(parsed['title'])}) köpte "
        f"{_fmt(tot_sh)} aktier à ${avg_px:.2f} ≈ ${_fmt(tot_val)}\n"
        f"Anmält {html.escape(str(filing_date))} (Form 4, SEC){cluster}\n\n"
        f"<i>Öppna marknadsköp (kod P). Kluster av köp – flera insiders, egna pengar – "
        f"är den insidersignal med starkast stöd i forskningen. Ej rådgivning.</i>"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Börsvakt – insiderlarm (SEC EDGAR)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    ins = cfg.get("insiders", {})
    if not ins.get("enabled", False):
        print("insiders: avstängd i config.yaml.")
        return 0

    ua = os.environ.get("SEC_USER_AGENT") or str(ins.get("sec_user_agent", ""))
    if "@" not in ua:
        print("insiders: SEC_USER_AGENT/sec_user_agent saknar giltig e-post – SEC kräver "
              "identifierande User-Agent. Hoppar över insiderkoll (sätt secret "
              "SEC_USER_AGENT till din e-post för att aktivera) i stället för att "
              "hamra EDGAR med 403.", file=sys.stderr)
        return 0

    window = int(ins.get("cluster_window_days", 14))
    state = load_state()
    state.setdefault("seen_filings", {})
    state.setdefault("insider_buys", {})
    state.setdefault("cik_cache", {})

    for ticker in ins.get("us_tickers", []) or []:
        try:
            cik = get_cik(ticker, ua, state["cik_cache"])
            if not cik:
                print(f"  {ticker}: hittade ingen CIK hos SEC.", file=sys.stderr)
                continue
            seen = state["seen_filings"].setdefault(ticker, [])
            first_run = len(seen) == 0  # första körningen: lär in, larma inte historik
            for filing in recent_form4(cik, ua):
                if filing["acc"] in seen:
                    continue
                if first_run:
                    seen.append(filing["acc"])
                    continue
                xml_text = fetch_form4_xml(cik, filing, ua)
                if not xml_text:
                    # Markera INTE som sedd – hämtningen försöks om nästa körning
                    # i stället för att filingen tappas för alltid.
                    continue
                parsed = parse_form4(xml_text)
                if parsed["buys"]:
                    log = state["insider_buys"].setdefault(ticker, [])
                    cutoff = (dt.date.today() - dt.timedelta(days=window)).isoformat()
                    owners = ({x["owner"] for x in log if x["date"] >= cutoff}
                              | {parsed["owner"]})
                    # Markera sedd + logga först vid bekräftad leverans, annars
                    # retry nästa körning (tappa aldrig ett insiderköp på 429/5xx).
                    if send_telegram(
                        build_buy_alert(ticker, parsed, filing["date"], len(owners), window),
                        args.dry_run,
                    ):
                        log.append({"date": filing["date"], "owner": parsed["owner"]})
                        log_alert("insiders", ticker, "buy", market="US",
                                  meta={"owner": parsed["owner"], "cluster": len(owners)},
                                  dry=args.dry_run)
                        seen.append(filing["acc"])
                elif parsed["sells"] and ins.get("alert_on_sales", False):
                    tot = sum(s["shares"] * s["price"] for s in parsed["sells"])
                    if send_telegram(
                        f"👤 <b>Insidersälj – {html.escape(ticker)}</b>: "
                        f"{html.escape(parsed['owner'])} sålde för ≈ ${_fmt(tot)} "
                        f"({html.escape(str(filing['date']))}). "
                        f"<i>Sälj är brusiga – lågt signalvärde.</i>",
                        args.dry_run,
                    ):
                        seen.append(filing["acc"])
                else:
                    # Varken köplarm eller säljlarm – inget att leverera.
                    seen.append(filing["acc"])
        except Exception as exc:
            print(f"  {ticker}: insider-fel: {exc}", file=sys.stderr)

    # Trimma loggar så state.json inte växer
    for t in list(state["seen_filings"]):
        state["seen_filings"][t] = state["seen_filings"][t][-200:]
    for t in list(state["insider_buys"]):
        state["insider_buys"][t] = state["insider_buys"][t][-100:]
    # cik_cache: behåll BARA konfigurerade tickers – hela SEC-kartan (10 000+
    # poster, ~200 KB) blåste upp state.json som committas var 15:e minut.
    wanted = {t.upper() for t in (ins.get("us_tickers", []) or [])}
    state["cik_cache"] = {k: v for k, v in state["cik_cache"].items() if k in wanted}
    if not args.dry_run:
        save_state(state)
    print("Insiderkoll klar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
