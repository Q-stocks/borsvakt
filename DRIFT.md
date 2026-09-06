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

---

## 4. Så gör du en ändring säkert

Ordningen är inte förhandlingsbar — den har fångat flera fel som annars gått live:

```bash
# 1. Synka mot molnet FÖRST (molnet committar var 15:e minut)
git fetch origin && git merge --ff-only origin/main

# 2. Ändra koden

# 3. Kompilera + kör berörda moduler torrt mot riktig data
.venv\Scripts\python -m py_compile <ändrade filer>
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

**Alla `--dry-run`-flaggor är på riktigt:** modulerna skriver varken state eller Telegram i
dry-run. Vakterna finns i sju moduler; ta inte bort dem.

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
  horisont) är append-only facit. Redigera dem inte i efterhand — utvärderingar fryses med
  flit, annars mäts mot en levande intradagsbar och facit blir permanent fel.

---

## 8. Beslut som redan är fattade — ändra inte utan nytt underlag

| Sak | Status |
|---|---|
| `momentum_gate: allpos` | PÅ. +1,6 pp/år, Sharpe 0,90→0,99, maxDD −52→−41 % i backtest. |
| F-score-kvalitetsfilter | **AV.** Brett allpos +31 % → +17 % med F-score. Momentum och kvalitet drar åt olika håll. |
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
