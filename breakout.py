#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – Breakout-larm (modul, Qullamaggie-mekaniserad)

EGEN strategi, separat från momentum: fångar TIDIGT insteg i ett färskt ben.
Backtestad (backtest_breakout.py) till +4,6 %/affär förväntat utfall, 41 % träff,
snittvinst ~4,5× snittförlust, maxDD −8 %. Edge i timingen är äkta — men den är
bara investerad ~7 % av tiden, så den är ett KOMPLEMENT (boostar avkastning när
setups dyker upp), inte en ersättare för momentum-kärnan. Loggas som modul
'breakout' så larmloggen ger den ett eget facit.

Setup (per aktie, dagsstängning):
  • Ben: +run_up_min (30 %) in i basen (~60 dagar).
  • Bas: senaste base_days (10) dagars range < base_range_max (15 %) OCH < halva benet.
  • Utbrott: stängning över basens högsta, på RVOL ≥ rvol_min (2), uppdag.
  • Likviditetsgrind: snitt daglig omsättning ≥ min_avg_turnover (annars hoppas över
    — viktigt för small cap / First North där tunn handel + osäker Yahoo-volym
    förstör signalen).

LARM med checklista (stop = utbrottsdagens lägsta, sälj-i-styrka / traila 20d MA),
aldrig autotrade. Tung daglig hämtning → batchad via yf.download.

Körning:  python breakout.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import sys

import yaml

from alertlog import log_alert
from scanner import (CONFIG_FILE, ROOT, drop_live_bar, load_state, save_state,
                     send_telegram)
from stocks import load_universe


def batch_download(tickers: list[str], chunk: int = 40):
    """ticker -> OHLCV-DataFrame (8 mån dagsdata). Batchat för fart/robusthet."""
    import yfinance as yf
    out: dict = {}
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        try:
            data = yf.download(part, period="8mo", interval="1d", auto_adjust=True,
                               group_by="ticker", threads=True, progress=False)
        except Exception as exc:
            print(f"  batch {i}: {exc}", file=sys.stderr)
            continue
        for t in part:
            try:
                df = data[t] if len(part) > 1 else data
                df = df.dropna()
                if len(df) >= 75:
                    out[t] = df
            except Exception:
                continue
    return out


def setup_at_last(df, p: dict) -> dict | None:
    """Är SENASTE baren ett utbrott enligt spec? Returnerar setup-info annars None."""
    base = int(p["base_days"])
    c = df["Close"].to_numpy(float)
    hi = df["High"].to_numpy(float)
    lo = df["Low"].to_numpy(float)
    vol = df["Volume"].to_numpy(float)
    t = len(c) - 1
    if t < 75:
        return None
    avgvol = vol[t - 20:t].mean()
    if avgvol <= 0:
        return None
    turnover = float((df["Close"] * df["Volume"]).tail(20).mean())
    base_hi = hi[t - base:t].max()
    base_lo = lo[t - base:t].min()
    rng = (base_hi - base_lo) / base_lo if base_lo > 0 else 9.9
    prior_lo = lo[t - 70:t - base].min()
    prior_move = c[t - base] / prior_lo - 1 if prior_lo > 0 else 0.0
    ok = (vol[t] >= float(p["rvol_min"]) * avgvol and c[t] > c[t - 1]
          and c[t] > base_hi and rng < float(p["base_range_max"])
          and prior_move >= float(p["run_up_min"]) and rng < 0.5 * prior_move)
    if not ok:
        return None
    return {"price": float(c[t]), "stop": float(lo[t]), "rvol": float(vol[t] / avgvol),
            "run_up": float(prior_move), "base_range": float(rng), "turnover": turnover,
            "date": df.index[t].date().isoformat()}


def build_alert(name: str, ticker: str, s: dict) -> str:
    risk = (s["price"] / s["stop"] - 1.0) * 100.0 if s["stop"] > 0 else 0.0
    return (
        f"🚀 <b>BREAKOUT (Qullamaggie)</b>\n"
        f"<b>{html.escape(name)}</b> ({html.escape(ticker)})\n"
        f"Bröt basen på <b>RVOL {s['rvol']:.1f}×</b> efter ett ben på "
        f"{s['run_up']:+.0%}. Bas-range {s['base_range']:.0%}.\n\n"
        f"<b>Plan (checklista):</b>\n"
        f"1. Inträde efter stängning / nästa öppning — jaga inte om det redan dragit långt.\n"
        f"2. <b>Stop = dagslägsta ≈ {s['stop']:.2f}</b> (initial risk ~{risk:.0f}%).\n"
        f"3. Sälj i styrka / traila med 10–20-dagars MA så länge trenden håller.\n"
        f"4. Position liten — låg träff (41%), edgen kommer från få stora vinnare.\n\n"
        f"<i>Eget, loggat experiment (komplement till momentum, ej ersättare). "
        f"Snäva stoppar. Ej rådgivning.</i>"
    )


def process_market(mkt: dict, params: dict, state: dict, dry: bool) -> None:
    name = mkt["name"]
    market = "US" if name == "USA" else mkt.get("market", "SE")
    min_turnover = float(mkt.get("min_avg_turnover", 0))
    if not (ROOT / mkt["universe_file"]).exists():
        print(f"Breakout {name}: universumfil saknas ({mkt['universe_file']}) "
              f"– väntar på Börsdata-export.")
        return
    universe = load_universe(mkt["universe_file"])
    names = dict(universe)
    frames = batch_download([t for t, _ in universe])
    fired = state.setdefault("breakout_alerts", {})

    hits = 0
    for ticker, _nm in universe:
        # Utbrott bekräftas på AVSLUTAD dagsbar – en levande intradagsbar kan
        # se ut som ett utbrott som sedan stängs under nivån.
        df = drop_live_bar(frames.get(ticker))
        if df is None or len(df) == 0:
            continue
        try:
            s = setup_at_last(df, params)
        except Exception as exc:
            print(f"  {ticker}: breakout-fel: {exc}", file=sys.stderr)
            continue
        if not s:
            continue
        if min_turnover and s["turnover"] < min_turnover:
            continue  # likviditetsgrind: hoppa tunt handlade namn
        key = f"{ticker}:{s['date']}"
        if fired.get(key):
            continue
        # Markera skickat (och logga facit) först vid bekräftad leverans –
        # annars tappas utbrottslarmet permanent vid ett Telegram-fel.
        if send_telegram(build_alert(names.get(ticker, ticker), ticker, s), dry):
            log_alert("breakout", ticker, "breakout", market=market, price=s["price"],
                      meta={"rvol": round(s["rvol"], 1), "run_up": round(s["run_up"], 3)}, dry=dry)
            fired[key] = True
            hits += 1
    print(f"Breakout {name}: {len(frames)} aktier skannade, {hits} utbrott.")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Börsvakt – Breakout-larm (Qullamaggie)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg_b = cfg.get("breakout", {})
    if not cfg_b.get("enabled", False):
        print("breakout: avstängd i config.yaml.")
        return 0

    params = {"run_up_min": cfg_b.get("run_up_min", 0.30),
              "base_days": cfg_b.get("base_days", 10),
              "base_range_max": cfg_b.get("base_range_max", 0.15),
              "rvol_min": cfg_b.get("rvol_min", 2.0)}

    state = load_state()
    for mkt in cfg_b.get("markets", []):
        try:
            process_market(mkt, params, state, args.dry_run)
        except Exception as exc:
            print(f"Breakout {mkt.get('name')}: fel: {exc}", file=sys.stderr)
    # Trimma dedup-historik
    if len(state.get("breakout_alerts", {})) > 500:
        state["breakout_alerts"] = dict(list(state["breakout_alerts"].items())[-500:])
    if not args.dry_run:
        save_state(state)
    print("Breakout-koll klar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
