# DRIFT.md — driftmanual för Börsvakt

Allt en ny operatör (människa eller AI-assistent) behöver för att **ta över driften**.
`README.md` beskriver vad systemet gör, `DEPLOY.md` hur det sattes upp, `CLAUDE.md` varför
koden ser ut som den gör. Det här dokumentet handlar om att hålla det igång.

> **Grundregel:** molnets `state.json` är driftsanningen. Kör aldrig motorerna lokalt
> parallellt med Actions, och rebasa alltid ovanpå `origin/main` innan du pushar.

---

## 1. Hur systemet kör sig självt

Ingen server. Allt körs i GitHub Actions på schema, med datorn avstängd.

| Workflow | Trigger | Innehåll (i ordning) |
|---|---|---|
| `scan.yml` | cron `*/15 4-22 * * 1-5` **+ extern pinger var 15:e min** | `watchdog.py` (schemavakt, körs FÖRST) → `scanner.py` → spara state |
| `daily.yml` | **enbart** `workflow_dispatch` från schemavakten | `pead.py` → `leadlag.py` → `exits.py` → `sectortrend.py` → `breakout.py` → `insiders.py` → `holdings.py` → `alertlog.py evaluate` → `marknadspuls.py` → `dashboard.py` → spara state |
| `monthly.yml` | **enbart** `workflow_dispatch` från schemavakten | `momentum.py` → `stocks.py` → `multifactor.py` → `trend.py` → `sectors.py` → `regime.py` → `alertlog.py report` → `dashboard.py` → spara state |
| `marknadspuls.yml` | manuell | Marknadsklimat-fliken separat |

**Schemavakten (`watchdog.py`) är enda schemaläggaren för daily och monthly.** GitHubs egen
cron var opålitlig på det här repot — daily landade 10+ timmar sent (mitt under nästa
handelsdag, så MA-brott bedömdes på levande intradagsbarer) och månadscronen uteblev helt
1 juli 2026. Därför har daily.yml och monthly.yml **ingen cron alls**. Vakten kollar via
GitHub-API om daily körts efter senaste vardagsstängning (21:30 UTC) och om monthly körts
under innevarande månad; annars dispatchar den. I praktiken:
- **daily** landar ~04:00 UTC — före börsöppning, på färdiga dagsstängningar.
- **monthly** landar vid första skan ≥ 06:15 UTC den 1:a (eller första vardagen därefter).

Vakten är fail-soft: den fäller aldrig skannern och avslutar alltid med exit 0. Max en
dispatch per varv.

> ⛔ **Ta ALDRIG bort** watchdog-steget eller `actions: write` ur `scan.yml`. Då slutar
> daily och monthly att köras, tyst.

`concurrency: borsvakt-state` serialiserar alla tre workflows så att state-pushar aldrig krockar.

---

## 2. Hälsokoll — kör detta först vid minsta tveksamhet

```bash
# 1. Lever molnet? Senaste state-commit ska vara minuter/timmar gammal, inte dagar.
git fetch origin && git log -3 --format="%ci  %s" origin/main

# 2. Går körningarna igenom?
gh run list --limit 10 --json workflowName,status,conclusion,createdAt

# 3. Kördes daily och monthly när de skulle?
gh run list --workflow=daily.yml --limit 3
gh run list --workflow=monthly.yml --limit 3
```

**Friskt system:** state-commits var 15:e minut under börstid, `conclusion: success` rakt
igenom, en daily per vardag ~04:00 UTC, en monthly per månad.

Snabb tolkning av tystnad:
- Inga state-commits alls på en timme under börstid → **pingern har dött** (se §5).
- Skan-körningar gröna men ingen daily på flera dagar → **schemavakten dispatchar inte**;
  kolla att `actions: write` finns kvar i `scan.yml` och läs vakt-steget i en skan-logg.
- Allt grönt men inga Telegram-larm → normalt. Systemet larmar bara när något händer.

---

## 3. Löpande rutiner

**Dagligen (30 sekunder):** läs Telegram. Inga larm = inget hände; det är ett giltigt svar.

**Varje vecka:** öppna dashboarden (https://q-stocks.github.io/borsvakt/) → fliken
*Scorecard*. Det är facit — out-of-sample, inte backtest. Titta på driftspåret per modul,
inte på enskilda larm.

**Den 1:a varje månad:** månadssignalen kommer i Telegram med exakta byten
("Sälj: X, Köp: Y"). Handla mekaniskt, i lugn takt — signalen håller i veckor, och
backtestet visar att senaste månaden kan skippas utan att avkastningen försämras.
Använd limit när spreaden är vid. USA-byten: handla från valutakonto (annars ~0,5 %
växlingsavgift per byte).

**Varje kvartal:** ny Börsdata-export av rapportkalendern → `data/earnings_sverige.csv`
(recept i `BORSDATA-EXPORT.md` §2). Exporten får innehålla framtida rapportdatum, så en
export per kvartal räcker för hela säsongen. Nuvarande fil täcker t.o.m. 3 nov 2026 →
**ny export behövs ~början av oktober 2026.**

**Vid behov:** `holdings.csv` är sanningskällan för "aktier jag äger" och läses av scanner,
exits och holdings.py. ⚠️ Repot är publikt — lägg bara **ticker + marknad** där.

### ⚠️ Innan du lägger en fundamentafil i `data/`

En filimport är en **strategiändring tills motsatsen bevisats**. Aktiemotorns
kvalitetsfilter styrs sedan 2026-09-06 av en egen flagga, `stocks.quality_filter_enabled`
(standard `false`). Dessförinnan räckte det att `quality_file` pekade på en fil som råkade
finnas: en rutinimport för multifaktorn kunde tyst slå på F-score-filtret som backtestet
förkastat (bred allpos +31 % → +17 %). Värre — med Börsdata-format på tickrarna (`HTRO B`
mot universumfilens `HTRO-B.ST`) matchade noll rader, universumet blev tomt och hela
portföljen hade sålts ut vid nästa rebalans.

Två skydd finns nu, men de ersätter inte en kontroll av vad du lägger in:
- Matchar färre än hälften av universumets tickrar hoppas filtret över, och notisen säger
  `HOPPAT ÖVER – bara N av M tickrar matchade`.
- Går ingen enda ticker att ranka står portföljen **oförändrad** och notisen säger `DATAFEL`.

Kör alltid `stocks.py --dry-run` efter en filimport och läs raden "Kvalitetsfilter:" för
varje marknad innan nästa månadskörning.

### ⚠️ Innan du städar en universumfil

Ett ägt innehav som saknas i universumfilen rankas numera ändå (det läggs till från
portföljen och bedöms på momentum som alla andra). Före 2026-09-06 fick det rank 10⁹ och
såldes utan att kursdata ens hämtats — en manuell städning var alltså en tyst säljorder.
Notisen listar sådana namn under "Ägs men saknas i universumfilen".

---

## 4. Så gör du en ändring säkert

Ordningen är inte förhandlingsbar — den har fångat flera fel som annars gått live:

```bash
# 1. Synka mot molnet FÖRST (molnet committar var 15:e minut)
git fetch origin && git merge --ff-only origin/main

# 2. Ändra koden

# 3. Kompilera, kör röktesterna, kör sedan berörda moduler torrt mot riktig data
.venv\Scripts\python -m py_compile <ändrade filer>
.venv\Scripts\python selftest.py              # 31 tester, inget nätverk, ingen state
.venv\Scripts\python stocks.py --dry-run      # tar några minuter på 326 tickers
.venv\Scripts\python exits.py  --dry-run
.venv\Scripts\python scanner.py --dry-run --force

# 4. Läs dry-run-utskriften som om den vore ett skarpt larm.
#    Blev det oväntade köp/sälj? Saknas data för fler tickers än vanligt?

# 5. Commit + rebase + push
git commit -am "..."          # långa meddelanden: git commit -F fil.txt
git fetch origin && git rebase origin/main
git -c credential.helper="!'C:/Program Files/GitHub CLI/gh.exe' auth git-credential" push origin main

# 6. Verifiera i molnet — nästa skan ska vara grön på din SHA
gh run list --limit 3 --json headSha,status,conclusion
```

**Strategiändringar kräver backtest först.** Repot innehåller `backtest.py`,
`backtest_filters.py` (vaktar/filter), `backtest_cap.py` (momentumtak),
`backtest_exits.py` (stopp), `backtest_pead*.py`, `backtest_breakout.py`,
`backtest_frequency.py`. Mönstret genom hela projektet: mät, dokumentera resultatet i
`CLAUDE.md`, ändra sedan — eller låt bli. Flera "självklara" förbättringar har fallit på
mätning (F-score, momentumtak, MA50-stopp) — se `CLAUDE.md` och §8 nedan.

**`--dry-run` i larmmodulerna är på riktigt:** de skriver varken state eller Telegram.
Vakterna finns i sju moduler; ta inte bort dem. **Men förutsätt inte att ordet
`--dry-run` gör vilket skript som helst ofarligt:**

| Skript | Skriver ändå |
|---|---|
| `alertlog.py evaluate` | Skrev till `log/evaluations.csv` även med `--dry-run` fram till 2026-09-06; nu vägrar den köra i dry-run i stället |
| `alertlog.py repair` | Skriver om `log/evaluations.csv` (med backup) — `--dry-run` visar bara vad som skulle tas bort |
| `watchdog.py` | Har ingen dry-run-flagga alls och **dispatchar skarpa workflows** om en token finns |
| `dashboard.py` | Skriver alltid `docs/index.html` lokalt |

Jämför `state.json`, `log/` och `holdings.csv` före och efter en torrkörning och säkerställ
att inget ändrats.

---

## 5. Felsökning — vanliga lägen och åtgärd

| Symptom | Sannolik orsak | Åtgärd |
|---|---|---|
| Inga state-commits under börstid | Extern pinger död (cron-job.org) eller dess PAT utgången | Se `PINGER.md`. Testa: `gh workflow run scan.yml --ref main` → ska ge HTTP 204 och starta en körning. Cron `*/15` finns kvar som backup-hjärtslag men firar glest. |
| Daily/monthly kör inte | Schemavakten kan inte dispatcha | Kontrollera `actions: write` i `scan.yml`; läs vakt-steget i skan-loggen. Nödutgång: `gh workflow run daily.yml --ref main` (⚠️ skickar skarpa larm). |
| `state.json KORRUPT – committar inte` | Avbruten körning mitt i skrivning | Steget är avsiktligt: hellre ingen commit än trasig state. Nästa körning läker oftast själv. Annars: återställ `state.json` från föregående commit. |
| Push-steget failar 5 gånger | Molnet committade samtidigt | Löser sig nästa körning (retry med `pull --rebase -X theirs`). |
| Månadsnotis uteblev men portföljen roterade | Ska inte kunna hända | Rotation är leveransvillkorad: state ändras bara efter bekräftad Telegram-leverans, annars exit 1 → vakten kör om. Kontrollera Telegram-secrets. |
| "kursdata saknas eller är fastfrusen" på ett innehav | Yahoo tappat symbolen | Fail-soft: innehavet behålls utan omprövning. Kontrollera manuellt om bolaget bytt ticker/avnoterats och städa universumfilen. |
| Larm på en aktie som inte går att handla | Frusen/ohandlad kursserie | Ska nu fångas av likviditetsgrinden i `stocks.liquidity()` (§8). Ett nytt fall = höj `min_daily_turnover` i `config.yaml`. |
| yfinance/SSL-fel lokalt (bara denna dator) | TLS-interception på maskinen | Windows-store-CA-bundle: `winca.pem` + `.venv/sitecustomize.py` är redan uppsatta. Sätt även `$env:PYTHONUTF8=1`. |
| Telegram tyst men körningar gröna | Inget hände. | Ingen åtgärd. |

**Manuella triggers** (`gh workflow run <fil> --ref main`) skickar **skarpa** larm och kan
rotera portföljer. Stäm av med ägaren innan du triggar `monthly.yml`.

---

## 6. Åtkomst och hemligheter

**GitHub-secrets** (Settings → Secrets and variables → Actions) — finns bara i molnet,
aldrig i repot:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `SEC_USER_AGENT` (SEC kräver kontaktmejl i
User-Agent), `ANTHROPIC_API_KEY` (valfri, PM-sammanfattningar).

**Push:** GitHub CLI-kontot `videlllinus-tech` med scopes `gist, read:org, repo`.
Mönstret i §4 fungerar för all vanlig kod. ⚠️ **Ingen `workflow`-scope** — ändringar i
`.github/workflows/` går inte att pusha den vägen och kräver en tillfällig PAT med
`repo`+`workflow` (skapa, pusha, radera).

**Repot är PUBLIKT.** Lägg aldrig token, mejladress eller kronbelopp i kod, config eller
commit-meddelanden. `dashboard.py` strippar belopp ur publicerad JSON och `holdings.py`
skriver aldrig antal/GAV till `state.json` — behåll de skydden.

---

## 7. Om något måste återställas

- **Fel portfölj i state:** `stock_portfolio` per marknad i `state.json`. Rätta, committa,
  pusha — men bevara molnets övriga driftstate (`seen_news`, `exit_alerts`, `sector_trend`,
  drift-portföljen). Radbaserad merge har tappat en hel sleeve tidigare; kontrollera efteråt.
- **Missad månadsrebalans:** trigga `monthly.yml` manuellt. Prev-portföljen är orörd, så
  omkörningen ger exakt samma bytesnotis igen.
- **Larmlogg:** `log/alerts.csv` (varje signal) och `log/evaluations.csv` (utfall per
  horisont) är append-only facit. Redigera dem inte för hand — utvärderingar fryses med
  flit, annars mäts mot en levande intradagsbar och facit blir permanent fel.
- **Ogiltiga mätpunkter:** `python alertlog.py repair` tar bort rader med icke-finit
  avkastning och sparar originalet som `log/evaluations_pre_repair_<datum>.csv`. De
  borttagna mäts om vid nästa `evaluate` och skrivs bara om de ger giltiga tal. Kör
  `--dry-run` först för att se vad som skulle tas bort. Bakgrund: fram till 2026-09-06
  kunde en NaN-kurs skrivas som utfall, och `_load_done()` räknade då raden som färdig →
  den mättes aldrig om (123 av 1509 rader). Nu vägrar utvärderaren skriva icke-finita tal,
  och både Telegram och dashboarden filtrerar bort dem med synligt bortfall.

**Vad facit faktiskt mäter:** stängning på första handelsdagen ≥ signaldatum, mot samma
dags indexstängning, över 1/5/20/60 handelsdagar. Det **loggade larmpriset används inte** —
priskolumnen i `alerts.csv` är dokumentation, inte mätpunkt. Det är därför en
scanner-signal som utlöses intradag mäts från den dagens stängning, inte från spiken.
Måttet är alltså "vad hände efter att signalen fanns", inte "vad gav affären" — en
riktig affärsbok med aktieantal och avslut finns inte i systemet.

---

## 8. Beslut som redan är fattade — ändra inte utan nytt underlag

| Sak | Status |
|---|---|
| `momentum_gate: allpos` | PÅ. +1,6 pp/år, Sharpe 0,90→0,99, maxDD −52→−41 % i backtest. |
| F-score-kvalitetsfilter | **AV** via egen flagga `quality_filter_enabled: false` (2026-09-06). Brett allpos +31 % → +17 % med F-score. Momentum och kvalitet drar åt olika håll. |
| Ägda innehav utanför universumet | Rankas alltid (2026-09-06). En universumstädning får aldrig bli en tyst säljorder. |
| Facit får inte innehålla NaN | Icke-finita utfall skrivs inte, fryses inte och räknas inte med (2026-09-06). |
| Mätbenchmark ≠ indexsignal | `alertlog.INDEX_BY_MARKET` mäter resultat; `stocks.markets[].index_signal` styr regimregeln; `sectortrend.BENCH` styr relativ styrka. Att byta det första ändrar redovisningen, de andra ändrar vad systemet gör. Blanda dem inte. |
| Backtest jämförs mot "Äg allt" | Sedan 2026-09-09. `^OMX` är ett prisindex och fel universum — se `CLAUDE.md` för de rättade talen (midlarge +31,9 % → +23,4 %). |
| `momentum_cap: 10.0` (+1000 %) | PÅ, men enbart som blow-off-försäkring. Snävare tak sänker CAGR och HÖJER maxDD. |
| MA50-stopp på innehav | **FÖRKASTAT** 2026-08-06. −6 till −11,5 pp CAGR i alla fyra universum, monotont i MA-längd = whipsaw. MA200 ≈ gratis men marginellt. `exits.py` förblir larm, aldrig autosälj. |
| Likviditetsgrind i Aktiemotorn | PÅ sedan 2026-08-06 efter att ett papper utan handel på 12 månader köpts in. |
| Autosälj generellt | Aldrig. Designprincip: larm + checklista, inte automatik. |
| Marknadstiming som prognos | Aldrig. `regime.py` beskriver nuläget med observerbara regler; bygg ingen cykeltimer. |

Full evidens och resonemang finns i `CLAUDE.md` (avsnitten om varje modul samt
"Kända fallgropar").

---

## 9. Vad som INTE är automatiserat

Detta kräver en människa:
- **Handla.** Systemet ger signaler, aldrig order.
- **Börsdata-exporter** (fundamenta + rapportkalender) — manuell nedladdning per kvartal.
- **Universumunderhåll** — döda/omdöpta tickers måste städas manuellt. Öppet just nu:
  `COFFEE-B.ST`, `NOBA.ST`, `SAMPO-SEK.ST`, `SNM-SDB.ST`, `VESTUM.ST`, `VSURE.ST` saknar
  Yahoo-data i `sverige_broad.csv`. VESTUM ägs dessutom i sleeven Sverige Stora och kan
  därför varken momentumprövas eller nedsidesbevakas.
- **`leadlag.links`** — lead-lag-kartan ska vara användardriven; auto-sökning vore data-mining.
- **Sektor-tickrar** (`QDVE.DE` m.fl.) är overifierade gissningar och hoppas tyst över.
