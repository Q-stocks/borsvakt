# Extern pinger för pålitlig scan-kadens

GitHub stryper högfrekventa `*/15`-cron på lågaktivitetsrepo — `scan.yml` firar i
praktiken bara några gånger per dag i stället för var 15:e minut. Lösningen är en
gratis extern cron som triggar workflowen via GitHubs API (`workflow_dispatch`)
på en pålitlig kadens. `scan.yml` har redan `workflow_dispatch:` aktiverat, så
ingen kodändring behövs — bara stegen nedan.

> **STATUS (2026-07-02): AKTIV OCH VERIFIERAD.** Pingern kör var 15:e minut,
> vardagar ca 06:00–21:45 **svensk tid** (04:00–19:45 UTC). Skanningarna syns
> som `workflow_dispatch`-händelser i Actions-fliken.
>
> **Schemavakt (watchdog.py):** GitHubs cron var opålitlig även för daily/monthly
> (körningar 10+ h sena eller uteblivna — månadskörningen 2026-07-01 uteblev
> tills den triggades manuellt). Därför kör `scan.yml` numera `watchdog.py`
> först i varje körning: den dispatchar `daily.yml`/`monthly.yml` automatiskt
> när en körning saknas för sitt fönster. **Ingen manuell trigg behövs längre.**
> daily/monthly har INGEN egen cron kvar (den gav bara sena dubbelkörningar) —
> schemavakten är enda avsändaren, med scan-cronen som backup-hjärtslag om
> pingern skulle dö.
>
> **Valfri förbättring:** pingern slutar 21:45 svensk tid, medan USA stänger
> 22:00 (sommartid). Vill du bevaka USA-sessionens sista kvart intradag,
> förläng cron-job.org-schemat till 06:00–23:45 svensk tid. Nedsidesvakten
> fångar ändå allt på stängningsdata morgonen efter, så detta är kosmetik.

## Steg

### 1. Skapa en fine-grained PAT (token)
- https://github.com/settings/tokens?type=beta → **Generate new token**
- Repository access: **Only select repositories** → `Q-stocks/borsvakt`
- Permissions: **Actions → Read and write** (räcker för att trigga workflows)
- Utgång: så långt du vill (eller "no expiration" för en pinger som ska leva)
- Kopiera token (`github_pat_…`)

### 2. Sätt upp gratis cron på cron-job.org
- Skapa konto på https://cron-job.org (gratis)
- **Create cronjob:**
  - **URL:** `https://api.github.com/repos/Q-stocks/borsvakt/actions/workflows/scan.yml/dispatches`
  - **Schedule:** var 15:e minut, vardagar 06–21 UTC (`*/15 6-21 * * 1-5`)
  - **Request method:** `POST`
  - **Request headers:**
    - `Accept: application/vnd.github+json`
    - `Authorization: Bearer github_pat_DIN_TOKEN`
    - `X-GitHub-Api-Version: 2022-11-28`
    - `User-Agent: borsvakt-pinger`
  - **Request body:** `{"ref":"main"}`
- Spara. Testa med **Run now** → en scan-körning ska dyka upp under repo:ts
  **Actions**-flik inom någon minut.

### 3. Klart
Pingern triggar nu `scan.yml` pålitligt var 15:e minut. `concurrency`-gruppen
`borsvakt-state` ser till att en pingad körning aldrig krockar med den
schemalagda cron-backupen eller daily/monthly — de serialiseras och pushar
state turvis.

## Alternativ
- **UptimeRobot**, **GitHub Actions i ett annat repo**, eller egen server gör
  samma POST-anrop.
- Vill du slippa token-hanteringen: acceptera den glesa cron-kadensen. Kärnan
  (monthly Aktiemotorn + daily nedsidesvakt) kör pålitligt ändå — bara den
  intraday-snabba skannern blir gles.
