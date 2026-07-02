#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – Aktiemotorn (modul 3): momentum på enskilda aktier.

Regeln (per marknad, en gång i månaden):
  1. (Valfritt) Kvalitetsfilter: behåll topp X % av universumet enligt
     en fundamenta-fil (t.ex. Börsdata-export i data/). "Trendande"-
     designen: fundamenta väljer poolen, momentum väljer aktierna.
  2. Ranka poolen på Sammansatt momentum = snitt av 3-, 6- och
     12-månadersavkastning (månadsstängningar).
  3. Äg topp `top_n` (10). BANDING: sälj en aktie först när den fallit
     under rank `band_keep` (20) – sänker omsättningen utan att kosta
     avkastning.
  4. Regimfilter (valfritt): index under sitt 10-mån glidande medel →
     kassa denna månad.
  5. Notisen listar EXAKTA byten: Sälj / Köp / Behåll. Ingen brådska –
     momentumsignalen håller i veckor; handla med limit när spreaden
     är tight.

Körning:  python stocks.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import math
import sys

import yaml

from momentum import _ret, month_end_closes
from alertlog import log_alert
from scanner import CONFIG_FILE, ROOT, load_state, save_state, send_telegram


# ----------------------------------------------------------------------
# Indata
# ----------------------------------------------------------------------

def _sniff_reader(fh):
    sample = fh.read(4096)
    fh.seek(0)
    delim = ";" if sample.count(";") > sample.count(",") else ","
    return csv.DictReader(fh, delimiter=delim)


def load_universe(path: str) -> list[tuple[str, str]]:
    rows = []
    with open(ROOT / path, encoding="utf-8-sig") as fh:
        for r in _sniff_reader(fh):
            low = {k.lower().strip(): (v or "").strip() for k, v in r.items() if k}
            t = low.get("ticker", "")
            if t:
                rows.append((t, low.get("namn") or low.get("name") or t))
    return rows


def borsdata_to_yahoo(raw: str, market: str, overrides: dict) -> str:
    """Konverterar en Börsdata-ticker till Yahoo-symbol.
    Börsdata: 'VOLV B' / 'ERIC B' (mellanslag + aktieslag).
    Yahoo SE: 'VOLV-B.ST'. Yahoo US: i stort sett oförändrad ('BRK.B'->'BRK-B').
    OBS: heuristik – kantfall (SDB, ADR, namnbyten) fångas av `overrides`
    i config eller verifieras i Claude Code. Se TODO."""
    t = raw.strip().upper()
    if t in overrides:
        return overrides[t]
    if market.upper() == "SE":
        return t.replace(" ", "-").replace(".", "-") + ".ST"
    return t.replace(" ", "-").replace(".", "-")  # US/övrigt


def load_borsdata(path: str, cfg_s: dict, market: str) -> list[dict] | None:
    """Läser en Börsdata-export → lista av {ticker(Yahoo), name, quality}.
    Används både som UNIVERSUM (alla rader = Large+Mid+Small Cap) och som
    kvalitetsfilter i ett svep. Returnerar None om filen saknas."""
    p = ROOT / path
    if not p.exists():
        return None
    tcol = cfg_s.get("ticker_column", "Ticker").lower()
    qcol = cfg_s.get("quality_column", "Kvalitet").lower()
    ncol = "namn"
    overrides = {k.upper(): v for k, v in (cfg_s.get("ticker_overrides") or {}).items()}
    rows = []
    with open(p, encoding="utf-8-sig") as fh:
        for r in _sniff_reader(fh):
            low = {k.lower().strip(): (v or "").strip() for k, v in r.items() if k}
            raw_t = low.get(tcol, "")
            if not raw_t:
                continue
            q = None
            if low.get(qcol):
                try:
                    q = float(low[qcol].replace("%", "").replace(",", "."))
                except ValueError:
                    q = None
            rows.append({
                "ticker": borsdata_to_yahoo(raw_t, market, overrides),
                "name": low.get(ncol) or low.get("name") or raw_t,
                "quality": q,
            })
    return rows or None


def load_quality(path: str, ticker_col: str, quality_col: str) -> dict | None:
    """Läser en fundamenta-fil (t.ex. Börsdata-export). Returnerar
    ticker -> tal (högre = bättre), eller None om filen saknas."""
    p = ROOT / path
    if not p.exists():
        return None
    vals: dict[str, float] = {}
    with open(p, encoding="utf-8-sig") as fh:
        for r in _sniff_reader(fh):
            low = {k.lower().strip(): (v or "").strip() for k, v in r.items() if k}
            t, q = low.get(ticker_col.lower()), low.get(quality_col.lower())
            if t and q:
                try:
                    vals[t.upper()] = float(q.replace("%", "").replace(",", "."))
                except ValueError:
                    pass
    return vals or None


# ----------------------------------------------------------------------
# Motorn
# ----------------------------------------------------------------------

def score_universe(universe: list[tuple[str, str]]) -> tuple[list[dict], list[str]]:
    scored, errors = [], []
    for ticker, name in universe:
        closes = month_end_closes(ticker)
        if closes is None:
            errors.append(ticker)
            continue
        r3, r6, r12 = _ret(closes, 3), _ret(closes, 6), _ret(closes, 12)
        above = float(closes.iloc[-1]) > float(closes.iloc[-10:].mean())  # eget 10-mån MA
        scored.append({"ticker": ticker, "name": name,
                       "r3": r3, "r6": r6, "r12": r12, "above": above,
                       "score": (r3 + r6 + r12) / 3.0})
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored, errors


def _passes_gate(r: dict, gate: str, cap: float | None = None) -> bool:
    """Vakt på NYA köp. Backtest (sverige, 14å): 'allpos' bäst — +1,6 pp/år,
    Sharpe 0,90→0,99, maxDD −52→−41 %, genom att utesluta trendbrutna studsare
    (positivt momentum-snitt men negativt 12m, t.ex. TOBII).
    `cap` (momentum_cap, valfritt): övre tak på 12-mån-avkastning för NYA köp –
    hoppa över redan extremt rusade namn. Banding behåller dem man REDAN äger.
    Test 2026-06 (backtest_cap.py): taket höjer INTE R/R (sänker CAGR, höjer maxDD);
    +1000 % (cap=10.0) ≈ gratis blow-off-försäkring, snävare tak = F-score-fällan."""
    if cap is not None and r.get("r12", 0.0) >= cap:
        return False
    if gate == "trend":
        return r.get("above", True)                       # eget 10-mån MA (svagt)
    if gate == "m12":
        return r.get("r12", 1.0) > 0                       # positivt årsmomentum
    if gate == "allpos":
        return r.get("r3", 1.0) > 0 and r.get("r6", 1.0) > 0 and r.get("r12", 1.0) > 0
    return True                                            # 'off'


def apply_banding(ranked: list[dict], prev: list[str], top_n: int, band_keep: int,
                  gate: str = "allpos", cap: float | None = None,
                  hold: set[str] | None = None) -> tuple[list[str], dict]:
    rank_of = {r["ticker"]: i + 1 for i, r in enumerate(ranked)}
    # `hold` = ägda tickers vars kursdata inte gick att hämta. De kan inte
    # rankas och BEHÅLLS oförändrade – ett tillfälligt Yahoo-fel får aldrig
    # generera ett säljlarm (fail-soft: sälj kräver en faktisk rank under band).
    hold = hold or set()
    keep = [t for t in prev if t in hold or rank_of.get(t, 10 ** 9) <= band_keep]
    # Banding behåller befintliga innehav via rank; vakten (inkl. tak) gäller bara nya köp.
    fill = [r["ticker"] for r in ranked
            if r["ticker"] not in keep and _passes_gate(r, gate, cap)
            ][: max(0, top_n - len(keep))]
    portfolio = sorted(keep + fill, key=lambda t: rank_of.get(t, 10 ** 9))
    return portfolio, rank_of


def risk_on(index_signal: str, sma_months: int = 10):
    closes = month_end_closes(index_signal)
    if closes is None:
        return True, False  # (risk på, okänt regimläge)
    return float(closes.iloc[-1]) > float(closes.iloc[-sma_months:].mean()), True


def process_market(mkt: dict, cfg_s: dict, state: dict, dry: bool = False) -> str:
    name = mkt["name"]
    quality_note = "av (ingen fundamenta-fil)"

    # Väg A (rekommenderad): Börsdata-exporten ÄR universumet – då får vi
    # hela Large+Mid+Small Cap och kvalitetsfiltret på en gång.
    bd = None
    if cfg_s.get("universe_from_quality", False):
        bd = load_borsdata(mkt.get("quality_file", ""), cfg_s, mkt.get("market", "SE"))

    if bd:
        have = [r for r in bd if r["quality"] is not None] or bd
        if any(r["quality"] is not None for r in bd):
            pct = float(cfg_s.get("quality_top_pct", 50))
            have.sort(key=lambda r: (r["quality"] if r["quality"] is not None else -1e9),
                      reverse=True)
            keep_n = max(1, math.ceil(len(have) * pct / 100.0))
            have = have[:keep_n]
            quality_note = (f"topp {pct:.0f} % av {len(bd)} bolag från "
                            f"{mkt.get('quality_file')} (universum + filter)")
        else:
            quality_note = f"universum från {mkt.get('quality_file')} (ingen kvalitetskolumn)"
        universe = [(r["ticker"], r["name"]) for r in have]
    else:
        # Väg B: hårdkodat universum + valfri separat kvalitetsfil.
        universe = load_universe(mkt["universe_file"])
        q = load_quality(mkt.get("quality_file", ""),
                         cfg_s.get("ticker_column", "Ticker"),
                         cfg_s.get("quality_column", "Kvalitet"))
        if q:
            pct = float(cfg_s.get("quality_top_pct", 50))
            have = [(t, n) for t, n in universe if t.upper() in q]
            have.sort(key=lambda tn: q[tn[0].upper()], reverse=True)
            keep_n = max(1, math.ceil(len(have) * pct / 100.0))
            universe = have[:keep_n]
            quality_note = f"topp {pct:.0f} % av {len(have)} bolag ({mkt.get('quality_file')})"

    # 2–3) Momentumranking + banding
    ranked, errors = score_universe(universe)
    prev = list(state.setdefault("stock_portfolio", {}).get(name, []))
    held_errors = [t for t in prev if t in set(errors)]
    top_n = int(cfg_s.get("top_n", 10))
    band = int(cfg_s.get("band_keep", 20))
    gate = cfg_s.get("momentum_gate", "allpos")
    cap_raw = cfg_s.get("momentum_cap")
    cap = float(cap_raw) if cap_raw not in (None, "", False) else None
    portfolio, rank_of = apply_banding(ranked, prev, top_n, band, gate, cap,
                                       hold=set(held_errors))

    # 4) Regimfilter
    regime_line = ""
    if cfg_s.get("regime_filter", True):
        on, known = risk_on(mkt["index_signal"])
        if known and not on:
            portfolio = []
            regime_line = ("\n🛑 <b>RISK AV:</b> index under 10-mån glidande medel → "
                           "kassa denna månad. Regeln återinvesterar när index vänder upp.")
        elif not known:
            regime_line = "\n⚠️ Kunde inte läsa indexdata – regimfilter ej utvärderat."

    sells = [t for t in prev if t not in portfolio]
    buys = [t for t in portfolio if t not in prev]
    by_ticker = {r["ticker"]: r for r in ranked}
    for t in buys:
        log_alert("stocks", t, "buy", market=("US" if name == "USA" else "SE"), dry=dry)

    lines = [f"📈 <b>Aktiemotorn – {html.escape(name)} – {dt.date.today():%Y-%m}</b>"]
    lines.append(f"Kvalitetsfilter: {html.escape(quality_note)}{regime_line}")
    lines.append("")
    if portfolio:
        lines.append(f"<b>Portfölj (topp {top_n}, banding {band}):</b>")
        for t in portfolio:
            r = by_ticker.get(t)
            if r:
                lines.append(f"{rank_of[t]:>2}. <b>{html.escape(t)}</b> "
                             f"{html.escape(r['name'])}  12m {r['r12']:+.0%}")
            else:
                lines.append(f" –. <b>{html.escape(t)}</b> kursdata saknas – "
                             f"behålls utan omprövning (kontrollera manuellt)")
    lines.append("")
    lines.append("<b>Byten denna månad:</b>")
    lines.append("• Sälj: " + (", ".join(html.escape(t) for t in sells) if sells else "–"))
    lines.append("• Köp: " + (", ".join(html.escape(t) for t in buys) if buys else "–"))
    lines.append(f"• Behåll: {len(portfolio) - len(buys)} st")
    if name == "USA" and (buys or sells):
        lines.append("💱 <i>USA-byten: handla från valutakonto (USD) – annars "
                     "~0,5 % växlingsavgift per byte.</i>")
    if errors:
        lines.append(f"⚠️ Saknar data ({len(errors)} av {len(universe)}): "
                     f"{', '.join(html.escape(e) for e in errors[:8])}"
                     + (" …" if len(errors) > 8 else ""))
    lines.append("")
    cap_note = (f" Tak: nya köp hoppas över om 12m-avkastning ≥ {cap:.0%} "
                "(banding behåller befintliga).") if cap else ""
    lines.append("<i>Ingen brådska – signalen håller i veckor. Handla med limit när "
                 "spreaden är tight. Banding: innehav säljs först när de fallit under "
                 f"rank {band}.{cap_note} Ej rådgivning.</i>")

    state["stock_portfolio"][name] = portfolio
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Börsvakt – Aktiemotorn (momentum på enskilda aktier)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg_s = cfg.get("stocks", {})
    if not cfg_s.get("enabled", False):
        print("stocks: avstängd i config.yaml.")
        return 0

    state = load_state()
    for mkt in cfg_s.get("markets", []):
        try:
            send_telegram(process_market(mkt, cfg_s, state, args.dry_run), args.dry_run)
        except Exception as exc:
            print(f"Aktiemotorn {mkt.get('name')}: fel: {exc}", file=sys.stderr)
    if not args.dry_run:
        save_state(state)   # dry-run ska inte persistera omräknad portfölj
    return 0


if __name__ == "__main__":
    sys.exit(main())
