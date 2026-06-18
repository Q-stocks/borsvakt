# Extern pinger för pålitlig scan-kadens

GitHub stryper högfrekventa `*/15`-cron på lågaktivitetsrepo — `scan.yml` firar i
praktiken bara några gånger per dag i stället för var 15:e minut. Lösningen är en
gratis extern cron som triggar workflowen via GitHubs API (`workflow_dispatch`)
på en pålitlig kadens. `scan.yml` har redan `workflow_dispatch:` aktiverat, så
ingen kodändring behövs — bara stegen nedan.

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
