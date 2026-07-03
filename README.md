# Börsvakt

Ett ärligt larmsystem för aktier: det hittar **lägen värda att titta på**
(volymspikar, stora rörelser, dina nivåer, nya pressmeddelanden), skickar
en Telegram-notis med **varför** larmet gick och en **regelbaserad
checklista** – och låter dig fatta beslutet.

Det här är medvetet *inte* en signaltjänst som lovar köp/sälj. Backtest
med Sharpe 4–5 på sociala medier är kurvanpassning, inte alfa. Värdet i
ett sådant här system är bevakning + disciplin: du slipper stirra på
börsen, och när något händer får du fakta i stället för puls.

## Vad den larmar på

| Larm | Logik | Varför |
|---|---|---|
| 🔔 Volymspik | Volym ≥ 3× normalt *för tidpunkten på dagen* (RVOL, 20d-snitt) **och** kursrörelse ≥ 1,5 % | Rå volym lurar (blockaffärer). Tidsjusterad RVOL + prisriktning skiljer "något händer" från brus. |
| 📰 Nytt PM | Nya poster i bolagets RSS-flöde (MFN/Cision) | I svenska småbolag är PM:et nästan alltid *orsaken* till rörelsen – och RSS är realtid, till skillnad från gratiskursdata (~15 min fördröjd). |
| 📈 Stor dagsrörelse | ≥ 6 % utan volymkrav | Fångar det RVOL missar. |
| 🎯 Dina nivåer | Kurs över/under nivåer du satt i `config.yaml` | Planen du satte i lugnt läge slår beslutet du fattar i panik. |

När volymspik och PM sammanfaller bakas de ihop till **en** notis, och om
`ANTHROPIC_API_KEY` är satt sammanfattar Claude PM:et i två meningar med
bedömd kurspåverkan (låg/medel/hög).

## Kom igång — komplett checklista

**Obligatoriskt (utan detta skickas inga notiser):**
1. **Telegram-bot:** [@BotFather](https://t.me/BotFather) → `/newbot` → spara token.
   Skicka ett meddelande till boten, öppna sedan
   `https://api.telegram.org/bot<TOKEN>/getUpdates` och läs av ditt `chat.id`.
2. **GitHub:** lägg upp mappen som repo. Settings → Secrets and
   variables → Actions: lägg in `TELEGRAM_BOT_TOKEN` och `TELEGRAM_CHAT_ID`.
   ⚠️ **Driftrepot är PUBLIKT** (gratis Pages kräver det): committa ALDRIG
   privata uppgifter — `holdings.csv` med antal/GAV/noteringar blir publik
   (fyll bara ticker+marknad = bevakningslista), och e-post/namn hör hemma i
   Secrets, inte i spårade filer.
3. **SEC-krav:** sätt din e-post som GitHub-secret **`SEC_USER_AGENT`**
   (Settings → Secrets) — INTE i `config.yaml` (spårad fil i publikt repo).
   Utan den blockerar SEC insider-anropen.

**Rekommenderat:**
4. `ANTHROPIC_API_KEY` som secret → Claude sammanfattar pressmeddelanden.
5. **GitHub Pages** (dashboarden): Settings → Pages → Deploy from branch →
   `main` / mappen `/docs`. Vyn hamnar på din `github.io`-adress.
6. **Verifiera gissade tickrar** i `trend` och `sectors` (obligationer,
   råvaror, sektor-UCITS). Overifierade hoppas tyst över tills de stämmer.

**För full effekt (kräver Börsdata Pro):**
7. Släpp exporter i `data/` enligt `BORSDATA-EXPORT.md`:
   `fundamenta_sverige.csv` (multifaktor) och `earnings_sverige.csv` (svensk
   PEAD). USA-PEAD och allt annat går på gratis data direkt.

**Experimentellt:**
8. Bygg ut `leadlag.links` med egen research (SE + US). Säkerställ att dina
   leaders ligger i `universe/*.csv` (annars fångas ingen PEAD).

**Workflows (ingen server behövs):**
- `scan.yml` — var 15:e min, börstid: volym/pris/PM/innehav + insiders.
- `daily.yml` — 21:30 UTC: PEAD → lead-lag → nedsidesvakt → larmlogg → dashboard.
- `monthly.yml` — 1:a varje månad: alla månadsstrategier + scorecard + dashboard.

Trigga en första körning manuellt under *Actions → Run workflow*.

**Kör/testa lokalt:** `pip install -r requirements.txt`, sätt samma
miljövariabler, sedan t.ex. `python scanner.py --dry-run --force` eller
`python backtest.py`.

**Viktigast på sikt:** låt larmloggen samla skarp data i några veckor.
Driftspåren och scorecardet är facit — döm ingen strategi (särskilt PEAD och
lead-lag) förrän signalerna mognat.

## Testa utan att skicka något

```bash
python scanner.py --dry-run --force
```

`--force` kör även när börsen är stängd, `--dry-run` skriver notiserna i
terminalen i stället för till Telegram.

## Ärliga begränsningar

- Kursdata från Yahoo Finance är **~15 min fördröjd** för Stockholmsbörsen.
  Räcker gott för swing-bevakning; inte för daytrading.
- Spotlight-bolag (t.ex. SHT Smart High-Tech) har osäker Yahoo-täckning.
- Checklistorna är heuristik. Inget här är finansiell rådgivning, och
  volymspikar utlöser fall lika ofta som uppgångar – därför kräver
  larmet riktningsinfo och PM-koppling innan du agerar.
- Svenska helgdagar filtreras inte ännu (ofarligt: inga kurser → inga larm).

## Nästa steg

Öppna mappen i **Claude Code** och säg *"läs CLAUDE.md och börja med
TODO-listan"*. Där ligger det som behöver verifieras (RSS-länkar, tickers)
och en prioriterad utbyggnadsplan (insynsregistret, breakout-logik,
historiklogg för att utvärdera larmens träffsäkerhet).

---

## Modul 1: Månadsmotorn (`momentum.py`)

Dual momentum med trendfilter: vid varje månadsskifte rankas universumet
på snittet av 3/6/12-månadersavkastning; bara tillgångar över sitt
10-månaders glidande medel får ägas; klarar ingen filtret → kassa/ränta.
En Telegram-signal den 1:a varje månad, noll beslut däremellan.

**USA/EU-detalj (PRIIPs):** som EU-kund kan du inte köpa USA-noterade
ETF:er hos Nordnet/Avanza. Därför kör både signal och handel på
UCITS-motsvarigheter (SXR8/CSPX = S&P 500, EUNL = MSCI World osv) –
identisk exponering, fullt köpbara. Enskilda USA-aktier berörs inte.

## Modul 2: Insiderlarm (`insiders.py`)

Bevakar SEC EDGAR (Form 4) för USA-listan i `config.yaml`: larm vid
öppna marknadsköp, med 🔥-flagga när flera olika insiders köpt inom
14 dagar (klustermönstret). Försäljningar ignoreras som standard – de
är brusiga. **Krav:** sätt din e-post som GitHub-secret `SEC_USER_AGENT`
(SEC blockerar anonyma anrop; lägg den INTE i config.yaml — publikt repo).
Första körningen lär bara in historiken
och larmar inte retroaktivt. Svenska insynsregistret (FI) byggs i
Claude Code – se CLAUDE.md.

## Börsdata?

Pro (Excel-export, inget API) behövs inte för någon av modulerna – allt
ovan går på gratis källor. Vill du senare lägga ett fundamenta-filter
(t.ex. "momentum bland lönsamma bolag") finns två vägar: släpp en
månatlig Pro-export i `data/` (manuellt, funkar för månadstakt) eller
uppgradera till Pro+ och låt Claude Code bygga en API-adapter.

## Modul 3: Aktiemotorn (`stocks.py`)

Momentum på enskilda aktier – designen som har bäst stöd både i den
akademiska litteraturen och i svenska backtester (Börslabbets
"Sammansatt momentum", körd på Börsdata-data): ranka universumet på
snittet av 3/6/12-månadersavkastning, äg topp 10, **banding** (sälj
först när en aktie fallit under rank 20 – lägre omsättning, bättre
netto), valfritt **kvalitetsfilter** från en Börsdata-export i `data/`
("trendande"-designen: fundamenta väljer poolen, momentum väljer
aktierna) och **regimfilter** (index under 10-mån MA → kassa).
Månadsnotisen ger exakta byten: Sälj / Köp / Behåll.

**Kostnadsdetalj för USA:** valutaväxling kan kosta ~0,5 % per byte och
äta upp månadsmomentum – öppna valutakonto (USD) hos Nordnet, eller kör
USA-motorn kvartalsvis.

## Modul 4: Nedsidesvakt (`exits.py`)

Asymmetrin: köp långsamt (månadsmomentum), överväg att sälja snabbare när
trenden viker. Bevakar dina innehav (Aktiemotorns portfölj + ev.
`extra_holdings`) en gång per handelsdag och larmar vid brott av 50-dagars
MA (tidig varning), 200-dagars MA (trend bruten) eller ≥20 % fall från
60-dagars topp. **Larm, aldrig autosälj.** Ärlig brasklapp: trendexit
minskar typiskt drawdown men kan kosta avkastning via whipsaw – det
välbelagda kraschskyddet är regimfiltret på portföljnivå, detta är en
overlay. Stäng av med `exit_watch.enabled: false`.

## Universum (viktigt för momentum)

Momentum behöver BREDD – de bästa trenderna finns ofta i mid/small cap.
`universe/sverige.csv` är nu ~100 bolag (large+mid+small startlista),
`universe/usa.csv` ~60. **Bästa lösningen när du har Börsdata:** sätt
`stocks.universe_from_quality: true` och släpp en Börsdata-export i
`data/` – då blir hela exporten universum (alla bolag, alla listor) OCH
kvalitetsfilter i ett svep. Börslabbets metod tar bort de med lägst
**F-score** före momentumranking, så använd helst F-score (eller ROC) som
`quality_column`. Tickers konverteras Börsdata→Yahoo automatiskt; kantfall
läggs i `ticker_overrides`.

Likviditetsbrasklapp för small cap: bredare spread äter avkastning –
banding, limitorder och kvalitetsfiltret (bort med skräpet) dämpar, men
håll positionsstorleken rimlig.

## Modul 5: PEAD-motorn (`pead.py`) — vinstdrift

Eventdriven, körs dagligen. Larmar när ett bolag i universumet slår
vinstförväntan (≥5 %) eller reagerar starkt positivt på rapportdagen
(≥5 % onormalt mot index), och håller en drift-portfölj: köplarm vid
färsk signal, säljlarm när driftfönstret (~70 dagar, ett kvartal) löpt
ut. USA använder yfinance-rapportdata; **Sverige kräver en Börsdata-
rapportexport** i `data/earnings_sverige.csv` (yfinance täcker inte
Stockholmsbörsen väl). Detta är den mest realistiska "snabbare än
momentum"-strategin — en av de mest ihållande anomalierna, och lägre
omsättning än daytrading.

## Modul 6: Multifaktor-motorn (`multifactor.py`)

Kombinerar tre sleeves — momentum, trendande värde, trendande kvalitet —
till en likaviktad portfölj. På riskjusterad basis slår kombon momentum
ensamt, eftersom faktorerna toppar vid olika tidpunkter. Momentum-sleeven
byts månads-/kvartalsvis, värde/kvalitet årsvis (mars) för låg
omsättning. **Kräver en Börsdata-export** med värde- (t.ex. EV/EBIT) och
kvalitetskolumn (helst F-score). Den kombinerade portföljen sparas i
state så nedsidesvakten och scannern bevakar innehaven.

## Modul 7: Trendföljning (`trend.py`) — defensiv overlay

Håll bara de tillgångsslag (globala/USA/EM-aktier, guld, långa
obligationer, råvaror — alla UCITS) som stänger över sitt 10-månaders
glidande medel; resten till kassa. Bred trendkorg = portföljens
kraschskydd, det som har bäst stöd mot momentums djupdyk i björnmarknad.
Komplement till de offensiva motorerna: kör som stabil bas, momentum/
aktier som satellit. Obligations- och råvarutickrarna är gissningar —
overifierade hoppas tyst över tills du bekräftat dem.

## Översikt — vad körs när

| När | Workflow | Moduler |
|---|---|---|
| Var 15:e min, börstid | `scan.yml` | Scanner (volym/pris/PM/innehav) + Insiders |
| Dagligen 21:30 UTC | `daily.yml` | **PEAD** + Nedsidesvakt |
| 1:a varje månad | `monthly.yml` | Tillgångsmomentum + Aktiemotorn + Multifaktor + Trendföljning |

Allt går på gratis datakällor; Börsdata-export behövs bara för
multifaktor och svensk PEAD. Stäng av valfri modul med `enabled: false`.

## Modul 8: Larmloggen (`alertlog.py`) — facit, inte backtest

Systemets sanningsserum. Varje skarp signal (PEAD-entry, insiderköp,
Aktiemotorns/multifaktorns köp, volymspikar) loggas med priset vid
larmtillfället till `log/alerts.csv`. Dagligen mäter `alertlog.py evaluate`
den faktiska framåtblickande avkastningen efter 1/5/20/60 dagar mot
respektive index, och `alertlog.py report` skickar ett månatligt scorecard:
träffprocent och snitt-överavkastning per modul. Detta är hur du avgör om
en strategi har en äkta edge — out-of-sample, i skarpt läge, innan riktiga
pengar riskeras — i stället för att lita på överoptimerade backtest.
Kommandon: `evaluate`, `report`, `show`.

## Modul 9: Sektorrotation (`sectors.py`) — "rätt sektor"

Momentum applicerat på sektorer: rankar en korg sektor-ETF:er på Sammansatt
momentum och håller topp 3 i uppåttrend. Ett mekaniskt sätt att ligga i de
ledande sektorerna utan att gissa. Sektor-UCITS-tickrarna är gissningar —
verifiera; saknad data hoppas tyst över.

## Modul 10: Regimläge (`regime.py`) — cykel-kontext, INTE prognos

Här går gränsen. **Cykel-timing (kalla toppar/bottnar, "gå in nu i 4-års-
cykeln") byggs inte** — det är opålitligt framåt och ren falsk precision.
Det som går ärligt är att *beskriva* nuläget med observerbara regler:
globala aktier över/under 200-dagars MA (risk på/av), räntekurvan 10å−3m
(invertering = gul lampa med lång fördröjning), och sektorbredd. Modulen
översätter läget till handling — offensivt (momentum/sektorer/PEAD) vs
defensivt (luta dig mot trendföljning + kvalitet) — men förutsäger inget.
Systemet *reagerar* mekaniskt på cykeln via trendfiltren; det spår den inte.

## Uppdaterad översikt

| När | Workflow | Moduler |
|---|---|---|
| Var 15:e min, börstid | `scan.yml` | Scanner + Insiders (loggar) |
| Dagligen 21:30 UTC | `daily.yml` | PEAD + Nedsidesvakt + Larmlogg-utvärdering |
| 1:a varje månad | `monthly.yml` | Tillgångsmomentum, Aktiemotorn, Multifaktor, Trendföljning, Sektorrotation, Regimläge, Scorecard |

## Modul 11: Dashboard (`dashboard.py`) — labbjournalen

En fristående statisk webbvy (`docs/index.html`) som genereras från loggen
och dina positioner — ditt "CRM för aktiesetups". Ingen server, inget
CDN-beroende. Tre vyer som flikar: **Cockpit** (offensiv/defensiv-banner, sektorledare, dagens larm, öppna positioner), **Scorecard** (driftspår + tabell med träffprocent och avkastning per strategi och horisont) och **Historik** (filterbar logg över alla larm — strategi, marknad, tickersökning). Regenereras i varje daglig och månatlig körning.

**Aktivera GitHub Pages:** Settings → Pages → Source: *Deploy from a branch*,
välj `main` och mappen `/docs`. Vyn hamnar på
`https://<ditt-användarnamn>.github.io/<repo>/`. Eller öppna `docs/index.html`
lokalt — den fungerar lika bra som fil. Allt är ett komplett, datadrivet
ekosystem: signaler → Telegram för exekvering, dashboard för utvärdering.

## Modul 12: Backtest (`backtest.py`) — validera bakåt

Kör den **bestämda** regeluppsättningen (samma som Aktiemotorn: sammansatt
momentum 3/6/12, topp 10, banding 20, trend- + regimfilter, 15 bp
handelskostnad) mot historik. Eftersom inget finjusteras är detta giltig
validering, inte kurvanpassning. Rapporterar per kalenderår + totalt:
avkastning, volatilitet, Sharpe, max nedgång, andel månader som slår index,
och de värsta månaderna. `--trend` backtestar i stället multi-tillgångs-
trendföljningen.

```bash
python backtest.py                          # USA, 15 år
python backtest.py universe/sverige.csv ^OMX 20
python backtest.py --trend 15
```

**Körs lokalt** (kräver Yahoo-åtkomst). **Två varningar i utskriften:**
universumet är dagens bolag → survivorship bias (för optimistiskt — använd
Börsdata med avnoterade bolag för ren test), och historik garanterar inget.

## Modul 13: Lead-Lag (`leadlag.py`) — EXPERIMENTELL värdekedja

Ovanpå PEAD: när en mappad "kanonfågel" (leader) får ett PEAD-larm, skickar
modulen ett experimentellt bevakningslarm på de nedströmsbolag du mappat —
de som ännu inte rört sig (redan rusade hoppas över). Bygger på Cohen-
Frazzini, men ärligt: det är PEAD i ett extra led, en försvagad effekt, så
allt loggas och **utvärderas out-of-sample** i scorecardet innan det får
styra pengar.

**Arbetsdelning:** du bygger länkkartan (`leadlag.links` i config, gärna med
AI-research — vilka bolag gynnas 1–2 kvartal senare); systemet sköter
trigger + loggning. Saknar en leader mappade följare får du en **research-
nudge** som ber dig fylla på. Leaders måste ligga i PEAD-universumet
(`universe/*.csv`) för att fångas; följarna behöver inte. Fungerar för SE
och US (marknad per länk). Exemplen i config är att verifiera/ersätta.

## Modul 14: Sektortrend-vakt (`sectortrend.py`)

Medan sektorrotationen (modul 9) *byter* sektorer månadsvis, varnar den här
dagligen när en sektor **vänder** — med erkända tekniska system: pris korsar
**200-dagars MA**, **guldkors/dödskors** (50d × 200d) och **relativ styrka**
mot index (sektorrotation/RRG). Larmar bara vid faktiskt statusskifte (ingen
spam); första körningen lär in baslinjen. Sektorindex byggs som likaviktade
korgar av aktier vi redan trackar — för både USA och Sverige (Sverige saknar
likvida sektor-ETF:er). Status syns även i dashboardens cockpit. Ärligt:
guld-/dödskors släpar och whippar — varning att titta närmare på, ej order.

## Mina innehav — följ dina egna aktier

Lägg aktierna du äger i **`holdings.csv`** (ticker, marknad, antal, inköpspris,
datum — allt utom ticker valfritt). Tre sätt att fylla den:
1. **Claude Code:** "lägg till 100 Volvo på 280 i holdings.csv".
2. **`holdings-editor.html`** — en inmatningssida (formulär, sparar lokalt,
   exporterar CSV-texten att lägga in i repot). Serveras på Pages bredvid
   dashboarden, eller öppnas lokalt.
3. Redigera filen direkt.

Innehaven följs sedan noggrant: **`holdings.py`** beräknar pris, vinst/förlust,
trend mot 50/200-dagars MA, nedgång från 60-dagars topp och avstånd till MA,
som visas i dashboardens **Innehav-flik**. Dessutom kopplas de automatiskt in i
nedsidesvakten (MA-brott), scannern (volym/pris/PM) och sektortrenden — så du
får larm på det du faktiskt äger, utan dubbletter.
