#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – Dashboard (modul 11)

Fristående statisk webbvy (docs/index.html) – ditt "CRM för aktiesetups".
Ingen server, inget CDN-beroende. Genereras från larmloggen + state.json och
regenereras i varje daglig/månatlig körning.

Tre vyer (flikar):
  • Cockpit / Nuläge   – regim (offensiv/defensiv), sektorledare, dagens larm,
                         öppna positioner i korthet.
  • Scorecard          – out-of-sample edge: driftspår + tabeller med
                         träffprocent och avkastning (1/5/20/60 d) per modul.
  • Historik           – filterbar databas över ALLA larm.

Körning:  python dashboard.py
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "log"
DOCS = ROOT / "docs"
HORIZONS = [1, 5, 20, 60]
FEED_CAP = 1500


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _agg(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    ex = [float(r["excess"]) for r in rows]
    wins = sum(1 for e in ex if e > 0)
    return {"n": len(rows), "hit": round(100.0 * wins / len(rows), 1),
            "excess": round(sum(ex) / len(ex), 2),
            "ret": round(sum(float(r["ret"]) for r in rows) / len(rows), 2)}


def build_data() -> dict:
    state = {}
    sp = ROOT / "state.json"
    if sp.exists():
        try:
            state = json.loads(sp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}

    cfg = {}
    cp = ROOT / "config.yaml"
    if cp.exists():
        try:
            cfg = yaml.safe_load(cp.read_text(encoding="utf-8")) or {}
        except Exception:
            cfg = {}
    hold_days = int(cfg.get("pead", {}).get("hold_days", 70))

    evals = _read_csv(LOG_DIR / "evaluations.csv")
    alerts = _read_csv(LOG_DIR / "alerts.csv")

    modules = sorted({r["module"] for r in evals})
    scorecard = []
    for m in modules:
        tracks = {h: _agg([r for r in evals if r["module"] == m and int(r["horizon"]) == h])
                  for h in HORIZONS}
        scorecard.append({"module": m, "tracks": tracks})
    overall = {h: _agg([r for r in evals if int(r["horizon"]) == h]) for h in HORIZONS}
    total = _agg(evals)

    today = dt.date.today()
    pead_pos = []
    for market, holds in (state.get("drift_portfolio") or {}).items():
        for sym, info in holds.items():
            try:
                rep = dt.date.fromisoformat(info.get("report", today.isoformat()))
                held = max(0, (today - rep).days)
            except (ValueError, TypeError):
                held = 0
            pead_pos.append({"ticker": sym, "market": market, "held": held,
                             "pct": max(0, min(100, round(100 * held / hold_days))) if hold_days else 0,
                             "surprise": info.get("surprise"), "reaction": info.get("reaction")})
    pead_pos.sort(key=lambda p: p["held"], reverse=True)

    sleeves = [{"name": n, "tickers": t} for n, t in (state.get("stock_portfolio") or {}).items() if t]

    feed = [{"date": r.get("date", ""), "module": r.get("module", ""),
             "ticker": r.get("ticker", ""), "kind": r.get("kind", ""),
             "market": r.get("market", "")} for r in alerts[-FEED_CAP:][::-1]]
    counts = {}
    for r in alerts:
        counts[r["module"]] = counts.get(r["module"], 0) + 1

    # Publik dashboard: strippa absoluta kronbelopp ur innehaven så bara
    # ticker, %, pris och trend hamnar på Pages. Antal/inköp/värde/kr-resultat
    # lämnar aldrig maskinen (de finns kvar i state.json lokalt).
    holdings_pub = None
    hs = state.get("holdings_status")
    if hs and hs.get("rows"):
        keep = ("ticker", "market", "price", "pl_pct", "above50", "above200",
                "dd60", "dist50", "dist200", "error")
        holdings_pub = {"updated": hs.get("updated"),
                        "rows": [{k: r.get(k) for k in keep} for r in hs["rows"]]}

    return {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "today": today.isoformat(),
        "regime": state.get("regime"),
        "sectors": state.get("sectors"),
        "sector_trend": state.get("sector_trend"),
        "holdings": holdings_pub,
        "scorecard": scorecard, "overall": overall, "total": total,
        "horizons": HORIZONS, "pead": pead_pos, "sleeves": sleeves,
        "feed": feed, "alert_counts": counts, "hold_days": hold_days,
        "modules": sorted(counts.keys()),
        "markets": sorted({r["market"] for r in alerts if r.get("market")}),
    }


TEMPLATE = r"""<!DOCTYPE html>
<html lang="sv">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Börsvakt — labbjournal</title>
<meta name="theme-color" content="#0d1420">
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" href="icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="icon-180.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Börsvakt">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=IBM+Plex+Mono:wght@400;500;600&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root{
    --ink:#0d1420;--panel:#141e2e;--panel2:#1b2840;--line:#26385a;
    --text:#e9eef7;--muted:#88a0c0;--gold:#e9b949;--pos:#4cc38a;--neg:#e5664f;--teal:#5fb0c9;
  }
  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{margin:0;background:radial-gradient(1100px 500px at 82% -8%,rgba(233,185,73,.07),transparent 60%),var(--ink);
    color:var(--text);font-family:Inter,system-ui,sans-serif;-webkit-font-smoothing:antialiased;line-height:1.5}
  .wrap{max-width:1080px;margin:0 auto;padding:26px 20px 80px}
  .mono{font-family:'IBM Plex Mono',monospace;font-variant-numeric:tabular-nums}
  h1,h2,h3{font-family:'Space Grotesk',sans-serif;margin:0;font-weight:700;letter-spacing:-.01em}
  .pos{color:var(--pos)}.neg{color:var(--neg)}.gold{color:var(--gold)}

  header{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px 16px;padding-bottom:16px}
  header h1{font-size:25px}header h1 .dot{color:var(--gold)}
  .sub{color:var(--muted);font-size:13px}
  .stamp{margin-left:auto;color:var(--muted);font-size:12px}

  /* Flikar */
  nav{display:flex;gap:6px;border-bottom:1px solid var(--line);margin-bottom:26px;overflow-x:auto}
  nav button{background:none;border:none;color:var(--muted);font-family:'Space Grotesk';font-weight:500;
    font-size:14px;padding:11px 14px;cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap}
  nav button:hover{color:var(--text)}
  nav button.active{color:var(--gold);border-bottom-color:var(--gold)}
  .view{display:none}.view.active{display:block;animation:fade .35s ease both}
  @keyframes fade{from{opacity:0}to{opacity:1}}

  .eyebrow{font-family:'IBM Plex Mono';font-size:11px;letter-spacing:.18em;text-transform:uppercase;
    color:var(--muted);margin:30px 0 12px;display:flex;align-items:center;gap:12px}
  .eyebrow:first-child{margin-top:4px}
  .eyebrow:before{content:"";flex:0 0 22px;height:1px;background:var(--gold)}

  /* Cockpit regimbanner */
  .banner{display:flex;flex-wrap:wrap;align-items:center;gap:16px;border:1px solid var(--line);
    border-radius:14px;padding:20px 22px;background:var(--panel)}
  .banner .big{font-family:'Space Grotesk';font-weight:700;font-size:30px;letter-spacing:-.02em}
  .banner .desc{color:var(--muted);font-size:13.5px;max-width:560px}
  .signals-row{display:flex;flex-wrap:wrap;gap:10px;margin-left:auto}
  .chip{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);background:var(--panel2);
    border-radius:999px;padding:7px 13px;font-size:12.5px}
  .led{width:8px;height:8px;border-radius:50%;box-shadow:0 0 8px currentColor}

  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:13px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:15px}
  .card .tk{font-family:'IBM Plex Mono';font-weight:600;font-size:15px}
  .card .meta{color:var(--muted);font-size:12px;margin-top:3px}
  .mkt{font-size:10px;color:var(--teal);border:1px solid var(--line);border-radius:5px;padding:1px 6px;font-family:'IBM Plex Mono'}
  .bar{height:6px;background:var(--panel2);border-radius:3px;margin-top:11px;overflow:hidden}
  .bar i{display:block;height:100%;background:linear-gradient(90deg,var(--gold),#f0cd78)}
  .kv{display:flex;justify-content:space-between;font-size:12.5px;margin-top:6px}.kv span:first-child{color:var(--muted)}

  /* Sektorledare */
  .lead{display:flex;align-items:center;gap:12px;background:var(--panel);border:1px solid var(--line);
    border-radius:11px;padding:12px 15px;margin-bottom:8px}
  .lead .rank{font-family:'Space Grotesk';font-weight:700;color:var(--gold);font-size:18px;width:24px}
  .lead .nm{font-family:'Space Grotesk';font-weight:500}
  .lead .r12{margin-left:auto;font-family:'IBM Plex Mono'}

  /* Driftspår */
  .legend{display:flex;flex-wrap:wrap;gap:16px;color:var(--muted);font-size:11.5px;margin:0 2px 12px;font-family:'IBM Plex Mono'}
  .tracks{display:grid;gap:10px}
  .track{display:grid;grid-template-columns:130px 1fr;gap:14px;align-items:center;background:var(--panel);
    border:1px solid var(--line);border-radius:12px;padding:13px 16px}
  .track .name{font-family:'Space Grotesk';font-weight:500;font-size:14px}
  .track .name small{display:block;color:var(--muted);font-size:11px;font-family:'IBM Plex Mono';margin-top:2px}
  .svgwrap{overflow-x:auto}
  svg.drift{display:block;width:100%;min-width:300px;height:88px}
  .axis{fill:var(--muted);font-size:10px;font-family:'IBM Plex Mono'}

  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line)}
  th{font-family:'IBM Plex Mono';font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);font-weight:500}
  td.mono,.num{font-family:'IBM Plex Mono'}
  .num{text-align:right}
  .cell small{display:block;color:var(--muted);font-size:10.5px}
  .tag{font-size:11px;padding:2px 7px;border-radius:5px;border:1px solid var(--line);font-family:'IBM Plex Mono';color:var(--muted)}
  .scroll{overflow-x:auto;border:1px solid var(--line);border-radius:12px}
  .scroll table th,.scroll table td{padding:10px 12px}
  .scroll table tr:last-child td{border-bottom:none}

  /* Filter */
  .filters{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px;align-items:center}
  .filters select,.filters input{background:var(--panel);border:1px solid var(--line);color:var(--text);
    border-radius:9px;padding:9px 12px;font-family:'IBM Plex Mono';font-size:13px}
  .filters input{min-width:150px}
  .filters .count{margin-left:auto;color:var(--muted);font-size:12px;font-family:'IBM Plex Mono'}

  .sleeve{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:13px 16px;margin-bottom:11px}
  .sleeve h3{font-size:14px;margin-bottom:8px}
  .pills{display:flex;flex-wrap:wrap;gap:7px}
  .pill{font-family:'IBM Plex Mono';font-size:12px;background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:4px 8px}

  .empty{border:1px dashed var(--line);border-radius:12px;padding:24px;text-align:center;color:var(--muted)}
  .empty b{color:var(--text);font-family:'Space Grotesk'}
  .foot{margin-top:44px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:18px}
  @media (prefers-reduced-motion:no-preference){.track,.card,.sleeve,.lead{animation:rise .45s ease both}
    @keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}}

  /* Info-symboler */
  .info{margin-left:auto;flex:0 0 auto;width:19px;height:19px;border-radius:50%;
    border:1px solid var(--line);background:var(--panel2);color:var(--muted);
    font-family:'IBM Plex Mono';font-size:11px;font-style:italic;line-height:1;cursor:pointer;
    display:inline-flex;align-items:center;justify-content:center;
    text-transform:none;letter-spacing:normal;transition:color .15s,border-color .15s}
  .info:hover{color:var(--gold);border-color:var(--gold)}
  .infobox{background:var(--panel2);border:1px solid var(--line);border-left:3px solid var(--gold);
    border-radius:10px;padding:13px 16px;margin:-2px 0 14px;font-size:12.8px;color:var(--muted);line-height:1.58}
  .infobox b{color:var(--text);font-family:'Space Grotesk';font-weight:500}
  .infobox ul{margin:7px 0 0;padding-left:17px}
  .infobox li{margin:4px 0}
  .infobox code{font-family:'IBM Plex Mono';color:var(--teal);font-size:11.5px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Börsvakt<span class="dot">.</span></h1>
    <span class="sub">labbjournal — vad signalerna faktiskt gjorde</span>
    <span class="stamp mono" id="stamp"></span>
  </header>

  <nav>
    <button data-v="cockpit" class="active">Cockpit</button>
    <button data-v="scorecard">Scorecard</button>
    <button data-v="innehav">Innehav</button>
    <button data-v="historik">Historik</button>
  </nav>

  <!-- COCKPIT -->
  <section class="view active" id="cockpit">
    <div class="eyebrow">Marknadsläge<button class="info" data-box="i-regime" aria-label="Förklaring">i</button></div>
    <div class="infobox" id="i-regime" hidden><b>OFFENSIV / DEFENSIV</b> är en mekanisk beskrivning av läget — ingen prognos. OFFENSIV = globala aktier över sitt 200-dagars glidande medel och normal räntekurva; då har momentum, sektorrotation och PEAD historiskt medvind. DEFENSIV = aktier under 200d MA eller inverterad räntekurva; då lutar systemet mot trendföljning och kvalitet. Chipsen visar de underliggande måtten. Systemet <b>reagerar</b> på läget — det spår inte vändpunkter.</div>
    <div class="banner" id="banner"></div>

    <div class="eyebrow">Aktiemotorn — momentum-portfölj<button class="info" data-box="i-momentum" aria-label="Förklaring">i</button></div>
    <div class="infobox" id="i-momentum" hidden>Aktierna med starkast <b>sammansatt momentum</b> (snittet av 3-, 6- och 12-månadersavkastning), topp 10 per marknad. Systemets kärnstrategi — uppdateras vid varje <b>månadsskifte</b> (ingen brådska däremellan; signalen håller i veckor). Banding: ett innehav säljs först när det fallit under rank 20. Ej rådgivning.</div>
    <div id="momentum"></div>

    <div class="eyebrow">Sektorledare — ligg där styrkan är<button class="info" data-box="i-leaders" aria-label="Förklaring">i</button></div>
    <div class="infobox" id="i-leaders" hidden>Sektorerna rankade efter <b>sammansatt momentum</b> (snittet av 3-, 6- och 12-månadersavkastning). <b>12m</b> är avkastningen senaste året. <b>trend ✓</b> = sektorn ligger över sitt 10-månaders glidande medel (annars <b>svag</b>). Idén: ligg där kapitalet redan flödar, utan att gissa varför.</div>
    <div id="leaders"></div>

    <div class="eyebrow">Sektortrend — vänder upp eller ner<button class="info" data-box="i-sectortrend" aria-label="Förklaring">i</button></div>
    <div class="infobox" id="i-sectortrend" hidden>Erkända tekniska trendsignaler per sektor (USA + Sverige): <b>200-dagars MA</b> (över = trend upp), <b>guld-/dödskors</b> (50d över/under 200d) och <b>relativ styrka</b> mot index. Grönt = trend upp, rött = ner. Sektorindex byggs som likaviktade korgar av aktier. Signalerna släpar och kan whippa — en vägvisare, inte en order. Telegram-varning skickas när en sektor faktiskt vänder.</div>
    <div id="sectortrend"></div>

    <div class="eyebrow">Dagens skarpa larm<button class="info" data-box="i-today" aria-label="Förklaring">i</button></div>
    <div class="infobox" id="i-today" hidden>Larm som loggats <b>idag</b> (annars visas de senaste). Varje kort visar ticker, vilken strategi som utlöste (<code>pead</code>, <code>stocks</code>, <code>insiders</code>, <code>scanner</code> …) och larmtypen. Fullständig historik finns under fliken Historik.</div>
    <div id="today"></div>

    <div class="eyebrow">Öppna PEAD-positioner<button class="info" data-box="i-pead" aria-label="Förklaring">i</button></div>
    <div class="infobox" id="i-pead" hidden><b>PEAD</b> = aktier som driver vidare efter en stark kvartalsrapport. Siffrorna betyder:<ul><li><b>… dagar sedan rapport</b> — hur länge sedan bolaget rapporterade.</li><li><b>fönster 70d</b> — driftfönstret; efter ~ett kvartal klingar effekten av och du får ett säljlarm.</li><li><b>stapeln</b> — hur långt in i fönstret positionen är (full = dags att stänga).</li><li><b>vinstöverr.</b> — hur mycket bolaget slog vinstförväntan (utfall mot estimat).</li><li><b>reaktion</b> — kursrörelsen på rapportdagen, justerad mot index (ren bolagseffekt).</li></ul></div>
    <div id="pead"></div>
  </section>

  <!-- SCORECARD -->
  <section class="view" id="scorecard">
    <div class="eyebrow">Driftspår — överavkastning mot index per horisont<button class="info" data-box="i-tracks" aria-label="Förklaring">i</button></div>
    <div class="infobox" id="i-tracks" hidden>Visar om en strategis edge <b>växer eller klingar av</b>. Linjen är genomsnittlig <b>överavkastning mot index</b>, mätt 1, 5, 20 och 60 dagar efter varje larm. Över nollinjen (grönt) = slog index; under (rött) = sämre än index. <b>n</b> = antal mätpunkter; lågt n är osäkert.</div>
    <div class="legend"><span>▲ över index (edge)</span><span>▼ under index</span>
      <span>linjen = snitt-överavkastning · 1 → 5 → 20 → 60 dagar</span></div>
    <div class="tracks" id="tracks"></div>

    <div class="eyebrow">Tabell — träffprocent och avkastning<button class="info" data-box="i-sctable" aria-label="Förklaring">i</button></div>
    <div class="infobox" id="i-sctable" hidden>Samma data i siffror. Per strategi och horisont: <b>överavkastning</b> mot index (grönt/rött), <b>träffprocent</b> (andel larm som slog index) och <b>n</b> (antal mätpunkter). Raden <b>Alla</b> är allt sammanslaget.</div>
    <div class="scroll" id="sctable"></div>

    <div class="eyebrow">Portföljer<button class="info" data-box="i-sleeves" aria-label="Förklaring">i</button></div>
    <div class="infobox" id="i-sleeves" hidden>Aktuella <b>innehav</b> per strategi (momentum, multifaktor, sektorer). Uppdateras vid månadssignalerna.</div>
    <div id="sleeves"></div>
  </section>

  <!-- INNEHAV -->
  <section class="view" id="innehav">
    <div class="eyebrow">Mina innehav — följs noggrant<button class="info" data-box="i-innehav" aria-label="Förklaring">i</button></div>
    <div class="infobox" id="i-innehav" hidden>Aktierna du lagt i <code>holdings.csv</code> (redigera via sidan <code>holdings-editor.html</code> eller säg till Claude Code). Per innehav: pris, <b>vinst/förlust</b> mot inköp, trend mot 50/200-dagars MA, <b>nedgång från 60-dagars topp</b> och avstånd till MA50/MA200 (mjuk stoppreferens). Dessa innehav bevakas dessutom automatiskt av nedsidesvakten (MA-brott), scannern (volym/pris/PM) och sektortrenden. Grönt = över MA / vinst, rött = under / förlust.</div>
    <div id="holdings"></div>
  </section>

  <!-- HISTORIK -->
  <section class="view" id="historik">
    <div class="eyebrow">Filterbar logg — alla larm<button class="info" data-box="i-historik" aria-label="Förklaring">i</button></div>
    <div class="infobox" id="i-historik" hidden>Alla larm systemet skickat. <b>Filtrera</b> på strategi, marknad eller sök på ticker. Räknaren visar hur många som matchar.</div>
    <div class="filters">
      <select id="f-mod"></select>
      <select id="f-mkt"></select>
      <input id="f-tk" placeholder="sök ticker…" autocomplete="off">
      <span class="count" id="f-count"></span>
    </div>
    <div class="scroll" id="logtable"></div>
  </section>

  <div class="foot">
    Heuristiker, inte rådgivning. Kursdata ~15 min fördröjd. Träffprocent och
    överavkastning mäts framåtblickande mot index, out-of-sample. Lågt n är
    osäkert — döm ingen strategi förrän signalerna hunnit mogna.
  </div>
</div>

<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const H = D.horizons, X = [40,130,230,330];
document.getElementById('stamp').textContent = 'uppdaterad ' + D.generated;
const led = c => `<span class="led" style="color:${c}"></span>`;
const sign = v => (v>0?'+':'') + v;
const esc = s => String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

/* Flikar */
document.querySelectorAll('nav button').forEach(b=>{
  b.onclick=()=>{
    document.querySelectorAll('nav button').forEach(x=>x.classList.remove('active'));
    document.querySelectorAll('.view').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    document.getElementById(b.dataset.v).classList.add('active');
  };
});

/* ---------- COCKPIT ---------- */
(function(){
  const b = document.getElementById('banner'), r = D.regime;
  let label='OKÄNT', col='var(--muted)', desc='Regimläge beräknas vid månadsskifte.';
  if(r){
    const off = (r.risk_on===false)||r.inverted;
    if(r.risk_on===true && !r.inverted){label='OFFENSIV';col='var(--pos)';}
    else if(off){label='DEFENSIV';col='var(--neg)';}
    desc = r.stance || desc;
  }
  let chips='';
  if(r){
    if(r.risk_on!=null) chips+=`<span class="chip">${led(r.risk_on?'var(--pos)':'var(--neg)')}aktier ${r.risk_on?'över':'under'} 200d MA</span>`;
    if(r.inverted!=null) chips+=`<span class="chip">${led(r.inverted?'var(--gold)':'var(--teal)')}kurva ${r.inverted?'inverterad':'normal'}${r.curve_spread!=null?' '+sign(r.curve_spread)+'pe':''}</span>`;
    if(r.breadth_pct!=null) chips+=`<span class="chip">${led('var(--teal)')}bredd ${r.breadth_pct}%</span>`;
  }
  b.innerHTML = `<div><div class="big" style="color:${col}">${label}</div></div>
    <div class="desc">${esc(desc)}</div><div class="signals-row">${chips}</div>`;
})();

(function(){
  const el = document.getElementById('momentum');
  if(!D.sleeves || !D.sleeves.length){ el.innerHTML='<div class="empty">Momentum-portföljen beräknas vid månadsskifte (1:a varje månad).</div>'; return; }
  el.innerHTML = D.sleeves.map(s=>`<div class="sleeve"><h3>${esc(s.name)} <span class="mono" style="color:var(--muted);font-size:12px">(${s.tickers.length})</span></h3>
    <div class="pills">${s.tickers.map(t=>`<span class="pill">${esc(t)}</span>`).join('')}</div></div>`).join('');
})();

(function(){
  const el = document.getElementById('leaders');
  const s = D.sectors;
  if(s && s.leaders && s.leaders.length){
    el.innerHTML = s.leaders.map((l,i)=>`<div class="lead"><div class="rank">${i+1}</div>
      <div class="nm">${esc(l.name)}</div>
      <div class="r12 ${l.r12>=0?'pos':'neg'}">12m ${sign(l.r12)}%</div>
      <span class="mkt" style="margin-left:10px">${l.above?'trend ✓':'svag'}</span></div>`).join('');
  } else el.innerHTML = '<div class="empty">Sektorrankning beräknas vid månadsskifte.</div>';
})();

(function(){
  const el = document.getElementById('today');
  const t = D.feed.filter(f=>f.date===D.today);
  const show = t.length ? t : D.feed.slice(0,5);
  if(!D.feed.length){ el.innerHTML='<div class="empty">Inga loggade signaler ännu.</div>'; return; }
  const note = t.length ? '' : '<div class="meta" style="margin-bottom:8px;color:var(--muted)">Inga nya larm idag — visar de senaste.</div>';
  el.innerHTML = note + '<div class="grid">' + show.map(f=>`<div class="card">
    <div class="tk">${esc(f.ticker)} <span class="mkt">${esc(f.market)}</span></div>
    <div class="meta">${esc(f.module)} · ${esc(f.kind)}</div>
    <div class="kv"><span>datum</span><span class="mono">${f.date}</span></div></div>`).join('') + '</div>';
})();

(function(){
  const pe = document.getElementById('pead');
  if(!D.pead.length){ pe.innerHTML='<div class="empty">Inga öppna PEAD-positioner.</div>'; return; }
  pe.className='grid';
  pe.innerHTML = D.pead.map(p=>{
    const s = p.surprise!=null?`<div class="kv"><span>vinstöverr.</span><span class="mono ${p.surprise>=0?'pos':'neg'}">${sign(p.surprise)}%</span></div>`:'';
    const r = p.reaction!=null?`<div class="kv"><span>reaktion</span><span class="mono ${p.reaction>=0?'pos':'neg'}">${sign(p.reaction)}%</span></div>`:'';
    return `<div class="card"><div class="tk">${esc(p.ticker)} <span class="mkt">${esc(p.market)}</span></div>
      <div class="meta">${p.held} dagar sedan rapport · fönster ${D.hold_days}d</div>
      <div class="bar"><i style="width:${p.pct}%"></i></div>${s}${r}</div>`;
  }).join('');
})();

/* ---------- SCORECARD ---------- */
function track(item){
  const t=item.tracks, vals=H.map(h=>t[h]?t[h].excess:null);
  const present=vals.filter(v=>v!==null);
  const maxAbs=Math.max(2,...present.map(v=>Math.abs(v))), midY=44, sc=30/maxAbs;
  const y=v=>midY-v*sc; let pts=[],dots='';
  H.forEach((h,i)=>{ if(t[h]===null)return;
    const yy=y(vals[i]); pts.push([X[i],yy]);
    const c=vals[i]>=0?'var(--pos)':'var(--neg)';
    dots+=`<circle cx="${X[i]}" cy="${yy}" r="4" fill="${c}"></circle>`;
    dots+=`<text class="axis" x="${X[i]}" y="${yy<midY?yy-9:yy+16}" text-anchor="middle" fill="${c}">${sign(vals[i])}%</text>`;
    dots+=`<text class="axis" x="${X[i]}" y="80" text-anchor="middle">${h}d</text>`;
    dots+=`<title>${h}d — n=${t[h].n}, träff ${t[h].hit}%, överavk ${sign(t[h].excess)}%, rå ${sign(t[h].ret)}%</title>`;
  });
  const line=pts.length>1?`<polyline points="${pts.map(p=>p.join(',')).join(' ')}" fill="none" stroke="var(--gold)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`:'';
  const zero=`<line x1="20" y1="${midY}" x2="360" y2="${midY}" stroke="var(--line)" stroke-dasharray="3 4"/>`;
  const anyN=H.map(h=>t[h]?t[h].n:0).reduce((a,b)=>Math.max(a,b),0);
  return `<div class="track"><div class="name">${item.module}<small>n≤${anyN}</small></div>
    <div class="svgwrap"><svg class="drift" viewBox="0 0 380 88" preserveAspectRatio="xMidYMid meet">${zero}${line}${dots}</svg></div></div>`;
}
(function(){
  const tr=document.getElementById('tracks');
  if(D.scorecard.length){
    tr.innerHTML=D.scorecard.map(track).join('');
    // tabell
    const cell=a=>a?`<td class="num cell"><span class="${a.excess>=0?'pos':'neg'}">${sign(a.excess)}%</span><small>träff ${a.hit}% · n${a.n}</small></td>`:'<td class="num" style="color:var(--muted)">–</td>';
    let rows=D.scorecard.map(s=>`<tr><td>${s.module}</td>${H.map(h=>cell(s.tracks[h])).join('')}</tr>`).join('');
    rows+=`<tr style="border-top:2px solid var(--line)"><td><b>Alla</b></td>${H.map(h=>cell(D.overall[h])).join('')}</tr>`;
    document.getElementById('sctable').innerHTML=`<table><thead><tr><th>Strategi</th>${H.map(h=>`<th class="num">${h}d</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table>`;
  } else {
    tr.innerHTML='<div class="empty"><b>Loggen mognar.</b><br>Driftspåren ritas när de första larmen blir 1–60 dagar gamla.</div>';
    document.getElementById('sctable').innerHTML='';
  }
  const sl=document.getElementById('sleeves');
  sl.innerHTML = D.sleeves.length ? D.sleeves.map(s=>`<div class="sleeve"><h3>${esc(s.name)} <span class="mono" style="color:var(--muted);font-size:12px">(${s.tickers.length})</span></h3>
    <div class="pills">${s.tickers.map(t=>`<span class="pill">${t}</span>`).join('')}</div></div>`).join('')
    : '<div class="empty">Inga portföljinnehav ännu.</div>';
})();

/* ---------- HISTORIK ---------- */
(function(){
  const mod=document.getElementById('f-mod'), mkt=document.getElementById('f-mkt'),
        tk=document.getElementById('f-tk'), cnt=document.getElementById('f-count'),
        box=document.getElementById('logtable');
  mod.innerHTML='<option value="">alla strategier</option>'+D.modules.map(m=>`<option>${m}</option>`).join('');
  mkt.innerHTML='<option value="">alla marknader</option>'+D.markets.map(m=>`<option>${m}</option>`).join('');
  function render(){
    const fm=mod.value, fk=mkt.value, ft=tk.value.trim().toUpperCase();
    const rows=D.feed.filter(f=>(!fm||f.module===fm)&&(!fk||f.market===fk)&&(!ft||f.ticker.toUpperCase().includes(ft)));
    cnt.textContent=`${rows.length} av ${D.feed.length} larm`;
    if(!rows.length){ box.innerHTML='<div class="empty">Inga larm matchar filtret.</div>'; return; }
    box.innerHTML=`<table><thead><tr><th>Datum</th><th>Strategi</th><th>Ticker</th><th>Typ</th><th>Mkt</th></tr></thead><tbody>
      ${rows.map(f=>`<tr><td class="mono">${f.date}</td><td>${esc(f.module)}</td><td class="mono">${esc(f.ticker)}</td><td><span class="tag">${esc(f.kind)}</span></td><td class="mono">${esc(f.market)}</td></tr>`).join('')}</tbody></table>`;
  }
  [mod,mkt].forEach(e=>e.onchange=render); tk.oninput=render;
  if(D.feed.length) render(); else box.innerHTML='<div class="empty">Inga loggade signaler ännu.</div>';
})();

/* ---------- INNEHAV ---------- */
(function(){
  const el=document.getElementById('holdings'); if(!el) return;
  const h=D.holdings;
  if(!h||!h.rows||!h.rows.length){ el.innerHTML='<div class="empty">Inga innehav i <b>holdings.csv</b> ännu.</div>'; return; }
  const cls=v=>v==null?'':(v>=0?'pos':'neg');
  const pc=v=>v==null?'–':(v>0?'+':'')+v+'%';
  const ma=ok=>ok==null?'<span class="tag">–</span>':(ok?'<span class="tag" style="color:var(--pos)">över</span>':'<span class="tag" style="color:var(--neg)">under</span>');
  el.innerHTML='<div class="scroll"><table><thead><tr>'
    +'<th>Ticker</th><th>Mkt</th><th class="num">Pris</th><th class="num">V/F</th>'
    +'<th>MA50</th><th>MA200</th><th class="num">Från 60d-topp</th><th class="num">Till MA50</th><th class="num">Till MA200</th>'
    +'</tr></thead><tbody>'
    + h.rows.map(r=>r.error
        ? `<tr><td class="mono">${esc(r.ticker)}</td><td class="mono">${esc(r.market||'')}</td><td colspan="7" style="color:var(--muted)">ingen data</td></tr>`
        : `<tr><td class="mono">${esc(r.ticker)}</td><td class="mono">${esc(r.market||'')}</td>`
          +`<td class="num mono">${r.price!=null?r.price.toFixed(2):'–'}</td>`
          +`<td class="num mono ${cls(r.pl_pct)}">${pc(r.pl_pct)}</td>`
          +`<td>${ma(r.above50)}</td><td>${ma(r.above200)}</td>`
          +`<td class="num mono ${cls(r.dd60)}">${pc(r.dd60)}</td>`
          +`<td class="num mono ${cls(r.dist50)}">${pc(r.dist50)}</td>`
          +`<td class="num mono ${cls(r.dist200)}">${pc(r.dist200)}</td></tr>`
      ).join('')
    +'</tbody></table></div>';
})();

/* Info-symboler: tap för att fälla ut förklaring */
document.querySelectorAll('.info').forEach(b=>{
  b.addEventListener('click', ()=>{ const el=document.getElementById(b.dataset.box); if(el) el.hidden=!el.hidden; });
});
</script>
</body>
</html>"""


MANIFEST = """{
  "name": "Börsvakt",
  "short_name": "Börsvakt",
  "description": "Aktielarm och labbjournal",
  "start_url": "./index.html",
  "scope": "./",
  "display": "standalone",
  "orientation": "portrait",
  "background_color": "#0d1420",
  "theme_color": "#0d1420",
  "icons": [
    {"src": "icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
    {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
    {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
  ]
}"""


def main() -> int:
    data = build_data()
    DOCS.mkdir(exist_ok=True)
    html = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    # PWA: manifest + ikoner så dashboarden kan "läggas till på hemskärmen"
    (DOCS / "manifest.webmanifest").write_text(MANIFEST, encoding="utf-8")
    for name in ("icon.svg", "icon-180.png", "icon-192.png", "icon-512.png"):
        src = ROOT / "assets" / name
        if src.exists():
            (DOCS / name).write_bytes(src.read_bytes())
    # Kopiera innehavs-editorn till docs/ så den nås på samma Pages-sajt
    editor = ROOT / "holdings-editor.html"
    if editor.exists():
        (DOCS / "holdings-editor.html").write_text(editor.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"Dashboard skriven: {DOCS / 'index.html'} — {len(data['scorecard'])} strategier, "
          f"{len(data['pead'])} PEAD-pos, {len(data['feed'])} larm i loggen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
