# CLAUDE.md – kontext för Claude Code

## Vad det här är
Börsvakt v3: fyra Python-moduler + GitHub Actions.
- `scanner.py` – var 15:e min, börstid: volymspikar (RVOL), stora
  rörelser, användarens prisnivåer, PM via RSS, valfri Claude-
  sammanfattning. State i `state.json` (committas av workflow).
- `insiders.py` – samma workflow: SEC EDGAR Form 4 (USA), larm vid
  insiderköp (kod P) med klusterflagga.
- `momentum.py` – den 1:a varje månad: dual momentum + 10-mån
  trendfilter på UCITS-ETF-universum (tillgångsnivå).
- `stocks.py` – samma månadskörning: **Aktiemotorn** – Sammansatt
  momentum (snitt 3/6/12 mån) på enskilda aktier, topp 10 med banding
  (sälj under rank 20), valfritt kvalitetsfilter från Börsdata-export
  i `data/`, regimfilter (index < 10-mån MA → kassa). Portfölj sparas
  i state och notisen ger exakta byten.
- `exits.py` – daglig nedsidesvakt: larmar (aldrig autosälj) när INNEHAV
  (Aktiemotorns state-portfölj) bryter MA50/MA200 eller faller ≥20 %
  från 60-dagars topp. Frivillig overlay; månadskärnan står kvar.
- `scanner.py` är portföljmedveten: skannar även innehaven för
  volym/pris/nyheter (scan_holdings).
- `pead.py` – daglig, eventdriven: vinstdrift (PEAD). Köplarm vid
  vinstslag/stark rapportreaktion, drift-portfölj, säljlarm efter
  ~70 dagar. USA via yfinance, SE via Börsdata-rapportexport.
- `multifactor.py` – månadsvis: momentum + trendande värde + trendande
  kvalitet (Börsdata-export). Kombinerad portfölj sparas i state.
- `trend.py` – månadsvis: multi-tillgång trendföljning (UCITS-korg över
  10-mån MA, annars kassa). Defensiv overlay/bas.
- `alertlog.py` – FACIT: loggar varje skarp signal med pris, mäter
  framåtblickande avkastning vs index (1/5/20/60 d), scorecard. Standalone
  ROOT (ingen cirkelimport). Övriga moduler anropar log_alert().
- `sectors.py` – månadsvis: sektormomentum (topp N sektorer i uppåttrend).
- `regime.py` – månadsvis: observerbar regim-KONTEXT (index vs 200d MA,
  räntekurva 10å-3m, bredd). INTE prognos. Persisterar snapshot till
  state['regime'] för dashboarden.
- `dashboard.py` – genererar docs/index.html (statisk webbvy, ingen server/
  CDN) från log/ + state.json. TRE flikvyer: Cockpit (regim+sektorledare+
  dagens larm), Scorecard (driftspår+tabell), Historik (filterbar logg).
  Körs sist i daily/monthly; docs/ committas. Pages: deploy from /docs.
- `sectors.py` persisterar rankning till state['sectors'] för cockpiten.
- `holdings.csv` = sanningskällan för 'aktier jag äger'; läses av scanner,
  exits, holdings.py. Auto-härleder marknad från .ST. Committas av workflows.
- `sectortrend.py` – DAGLIG sektorvändnings-vakt (US+SE): erkända system
  (200d MA-korsning, guld-/dödskors, relativ styrka). Sektorindex =
  likaviktade aktiekorgar (Sverige saknar sektor-ETF:er). Larmar bara vid
  statusskifte; persisterar state['sector_trend'] för dashboardens cockpit.
  Tung daglig hämtning (~80 aktier) – batcha via yf.download vid behov.
- `leadlag.py` – EXPERIMENTELL: när en mappad leader får PEAD-larm (läses ur
  drift_portfolio) skickas följarlarm på användarens manuellt/AI-mappade
  nedströmsbolag, loggas som modul 'leadlag' för out-of-sample-utvärdering.
  Körs i daily DIREKT efter pead.py. Länkkartan är användardriven (ej
  auto-sökning – det vore data-mining). Research-nudge när followers tom.
- `holdings.py` – läser holdings.csv (dina egna aktier), beräknar status
  (pris, P/L, MA-trend, drawdown, avstånd till MA) → state['holdings_status']
  för dashboardens Innehav-flik. LARMAR EJ (exits+scanner+sectortrend gör det,
  de läser nu holdings.csv via scanner.load_holdings). Daglig.
- `holdings-editor.html` – inmatnings-GUI (localStorage + export till
  holdings.csv). Kopieras till docs/ av dashboard.py för Pages.
- `backtest.py` – historisk simulering av den bestämda momentumregeln
  (+ --trend för trendföljning). Per-år + Sharpe/maxDD vs index. Körs
  lokalt (Yahoo). Survivorship-bias-varning inbyggd; rekommendera
  point-in-time/Börsdata-universum för ren körning.

Evidensgrund: cross-sectional momentum (Jegadeesh & Titman m.fl.).
Börslabbets backtest 2001-2021 (källa, Stockholmsbörsen): Sammansatt
momentum (topp 10, månadsvis, banding) gav 31,9 %/år vs index 10,1 %/år,
Sharpe 1,25 vs 0,55 – MEN volatilitet 23,6 %, max nedgång -49 %, och
underpresterar index 5 %+ en tiondel av månaderna. Backtest, ej facit.
"Trendande"-design = fundamenta (helst F-score) som pool-filter +
momentumranking. Senaste månaden kan skippas utan att tappa avkastning
(=> ingen brådska; reagera INTE snabbare på hela portföljen).

## Designprinciper (ändra inte utan att fråga)
1. **Inga låtsas-köpsignaler.** Mekaniska regler eller fakta +
   checklista. Ägaren vill uttryckligen INTE ha en "Sharpe 5"-maskin.
2. **Fail soft.** Per-ticker/per-modul try/except bevaras.
3. **Transparens.** Notiser visar villkor, filterstatus och datafel.
4. Svenska i användartext; Telegram HTML mode; `html.escape` på allt externt.
5. EU-kund (PRIIPs): ETF-förslag = UCITS. Enskilda USA-aktier OK.
   USA-byten: påminn om valutakonto (växling ~0,5 %/byte annars).
6. INGEN cykel-/marknadstiming som prognos. regime.py beskriver nuläget
   med observerbara regler; systemet reagerar via trendfilter, spår inte
   vändpunkter. Bygg aldrig en "4-årscykel-timer" e.d.

## Kommandon
```bash
pip install -r requirements.txt
python scanner.py  --dry-run --force
python insiders.py --dry-run
python momentum.py --dry-run
python stocks.py   --dry-run
python exits.py    --dry-run
python pead.py     --dry-run
python multifactor.py --dry-run
python trend.py    --dry-run
python sectors.py  --dry-run
python regime.py   --dry-run
python alertlog.py show
python alertlog.py evaluate
python alertlog.py report --dry-run
python dashboard.py        # -> docs/index.html
```
Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY` (valfri).

## TODO – prioriterad ordning
1. **Sätt användarens e-post** i `insiders.sec_user_agent` (SEC-krav).
2. **Universum – bredd är avgörande för momentum.** Bästa vägen: sätt
   `stocks.universe_from_quality: true` och låt Börsdata-exporten vara
   hela universumet (Large+Mid+Small Cap) + kvalitetsfilter. Verifiera då
   `borsdata_to_yahoo`-konverteringen mot ett urval rader och fyll
   `ticker_overrides` för kantfall (SDB/ADR/namnbyten). Alternativ
   startlista finns hårdkodad i `universe/` (SE ~100, US ~60) men är ej
   komplett. Småbolagstickers MÅSTE verifieras – flera i sverige.csv är
   bästa gissningar.
3. **Verifiera tickers/RSS:** alla `.ST`-symboler, momentum-universumet
   (SXR8.DE, EUNL.DE, IS3N.DE, XEON.DE, ^OMX; byt gärna ^OMX mot
   totalavkastande alternativ) samt MFN-RSS-länkarna i config.
4. **Börsdata-export (användaren har Pro):** dokumentera exportreceptet
   steg för steg → `data/fundamenta_{usa,sverige}.csv` med kolumnerna
   Ticker, Namn och en kvalitetskolumn (helst **F-score**, som Börslabbet,
   alt. ROC). Mappa via `ticker_column`/`quality_column`. Med
   `universe_from_quality: true` blir exporten både universum och filter.
   Pro+-API först om helautomatik önskas – bakom samma gränssnitt som
   `load_borsdata`.
5. **Kör ALLA moduler `--dry-run`** end-to-end mot riktig data.
   - `insiders.py`: verifiera parsning mot bolag med färskt Form 4-köp.
   - `pead.py` (USA): bekräfta att yfinance `get_earnings_dates` ger
     surprise för urvalet; hantera tickers som saknar data. Prestanda:
     ett earnings-anrop per ticker – cacha/batcha vid stora universa.
   - `trend.py`: VERIFIERA bond/råvaru-tickrarna (IBTL.DE, ICOM.DE).
   - `sectors.py`: VERIFIERA sektor-UCITS-tickrarna (QDVE.DE m.fl. är
     gissningar). Alternativ: STOXX Europe 600-sektorer. Overifierade
     hoppas tyst över.
   - `regime.py`: bekräfta ^TNX/^IRX-enheter i utfallet (Yahoo visar
     räntor i %); spreadens TECKEN är det viktiga.
   - `alertlog.py`: efter några veckors körning, granska log/alerts.csv +
     evaluations.csv. Detta är den HÖGSTA prioriteten på sikt – det är
     enda sättet att veta vad som faktiskt fungerar.
6. **PEAD svensk rapportexport** (recept finns i BORSDATA-EXPORT.md):
   dokumentera/verifiera Börsdata-exporten →
   `data/earnings_sverige.csv` med kolumnerna Ticker, Rapportdatum och
   antingen Surprise (%) eller EPS + EPS estimat. Samma ticker-konvertering
   som övriga moduler.
7. **Multifaktor-export** (recept i BORSDATA-EXPORT.md): `data/fundamenta_sverige.csv` behöver utöver
   F-score även en värdekolumn (EV/EBIT, lägre=bättre – sätt
   value_lower_better). Mappa i `multifactor`-sektionen.
8. **FI:s insynsregister (Sverige):** `se_tickers` i config; polla
   marknadssok.fi.se (sök/export per emittent), samma larmformat och
   klusterlogik som EDGAR.
9. **Larmlogg + utvärdering (HÖG PRIO på sikt):** CSV-logg av varje
   larm/signal/portföljbyte och ett skript som mäter utfall efter
   1/5/20/60 dagar. Detta är facit för ALLA strategier – framåtblickande,
   out-of-sample, inte backtest. Särskilt viktigt för PEAD och de
   experimentella delarna.
8. **Breakout-larm (Qullamaggie-mekaniserat) — KLART (2026-06):** byggt som
   EGEN modul `breakout.py` (loggas som modul `breakout`, separat facit), inte
   inuti scanner.py. Backtestat i `backtest_breakout.py` (förväntat +4,6 %/affär,
   41 % träff, maxDD −8 %) — äkta entry-edge men bara ~7 % deployat → KOMPLEMENT
   till momentum, ej ersättare. Kräver BREDD: USA på `universe/usa_broad.csv`
   (S&P 500), Sverige väntar på Börsdata-export → `universe/sverige_broad.csv`
   (Small Cap+ & First North bakom `min_avg_turnover`-likviditetsgrind). Batchad
   `yf.download`. Körs i daily.yml. Spec: +30 % ben, bas 10d range <15 % & <halva
   benet, utbrott på RVOL ≥2, stop = dagslägsta. Kvar: tuna parametrar mot bredare
   data; ev. intradagsvariant i scan.yml för ännu tidigare insteg.
9. **Lead-lag länkkarta (användardriven):** hjälp användaren bygga ut
   `leadlag.links` för SE+US via research — för varje kanonfågel, vilka
   nedströmsbolag gynnas 1–2 kvartal senare. Säkerställ att alla leaders
   ligger i `universe/*.csv` (annars fångas ingen PEAD). Påminn om att det
   är ett LOGGAT EXPERIMENT, inte en bevisad signal.
10. Svensk helgdagskalender (lågprio). Realtid (Nordnet External API,
   officiellt men kräver certifiering; eller Avanzas inofficiella)
   bakom `fetch_metrics`-gränssnittet – först vid behov.

## Kända fallgropar
- Yahoo: dagens rad kan ha volym 0 nära öppning → behåll
  `min_avg_volume` + elapsed-golvet (0.05).
- `momentum.py`/`stocks.py` släpper innevarande (ofullbordade) månad –
  signaler ska bygga på senast AVSLUTADE månadsstängning.
- EDGAR: `primaryDocument` kan vara XSL-renderad; fallback via
  `index.json` hittar rå-XML. Båda SEC-hostarna kräver User-Agent.
- Första insiders-körningen lär in historik tyst (first_run).
- Aktiemotorns portfölj ligger i `state.json` → monthly.yml committar
  state. Kör inte lokalt och i Actions parallellt (merge-konflikter).
- `stocks.py` gör 1 yfinance-anrop per ticker → fullständiga universa
  (500+) tar några minuter; överväg batch via `yf.download` vid behov.
