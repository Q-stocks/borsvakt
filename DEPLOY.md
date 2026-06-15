# Driftsättning — Börsvakt på mobil + dator

Systemet behöver **ingen egen server**. Beräkningarna körs gratis på GitHubs
maskiner (Actions, på schema). Resultaten når dina enheter via två kanaler som
båda funkar på mobil *och* dator:

| Kanal | Vad | Var |
|---|---|---|
| **Telegram** | Pushnotiser i realtid (volymspik, PEAD-köp, MA-brott, månadssignaler) | Telegram-appen på iOS/Android + Mac/Windows/webb |
| **GitHub Pages** | Dashboarden (cockpit, scorecard, innehav, historik) | En webbadress du öppnar i valfri webbläsare och lägger till på hemskärmen |

---

## Steg 1 — Telegram-bot (pushnotiserna)
1. Öppna [@BotFather](https://t.me/BotFather) i Telegram → `/newbot` → följ stegen → **spara token** (`TELEGRAM_BOT_TOKEN`).
2. Skicka valfritt meddelande till din nya bot (så den får prata med dig).
3. Öppna `https://api.telegram.org/bot<DIN_TOKEN>/getUpdates` i webbläsaren och läs av `"chat":{"id": ...}` → det är ditt `TELEGRAM_CHAT_ID`.

## Steg 2 — Lägg upp som GitHub-repo
Projektet är redan ett git-repo med en första commit (se `git log`). Skapa ett
**privat** repo på GitHub och pusha:

```bash
# med GitHub CLI (enklast):
gh repo create borsvakt --private --source=. --remote=origin --push

# eller manuellt: skapa repot på github.com, sedan:
git remote add origin https://github.com/<DITT-NAMN>/borsvakt.git
git branch -M main
git push -u origin main
```

## Steg 3 — Lägg in dina secrets
GitHub → repo → **Settings → Secrets and variables → Actions → New repository secret**:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `ANTHROPIC_API_KEY` *(valfri — låter Claude sammanfatta pressmeddelanden)*

## Steg 4 — Aktivera dashboarden (GitHub Pages)
Settings → **Pages** → Source: *Deploy from a branch* → välj `main` och mappen
`/docs` → Save. Efter en minut ligger dashboarden på
`https://<DITT-NAMN>.github.io/borsvakt/`.

> Den fylls med data först efter en körning. Trigga manuellt under
> **Actions → (valfri workflow) → Run workflow**, eller vänta på schemat.

## Steg 5 — "App" på hemskärmen
Öppna Pages-adressen på telefonen:
- **iPhone (Safari):** Dela-knappen → *Lägg till på hemskärmen*.
- **Android (Chrome):** ⋮-menyn → *Installera app* / *Lägg till på startskärm*.

Tack vare `manifest.webmanifest` + ikonerna startar den i helskärm med egen ikon,
som en riktig app. På datorn kan du lägga den som bokmärke eller installera via
Chromes "Installera"-ikon i adressfältet.

---

## Innan första skarpa körningen — checklista
1. **`config.yaml`:** sätt din riktiga e-post i `insiders.sec_user_agent`
   (SEC blockerar anonyma anrop). Gå igenom `tickers`, `momentum.universe` m.m.
2. **Verifiera tickers** (flaggade i koden som gissningar): `IBTL.DE`, `ICOM.DE`
   (trend), de döda `FNOX.ST`/`RESURS.ST`/`KIND-SDB.ST` i `universe/sverige.csv`,
   samt sektor-tickrarna. Overifierade hoppas tyst över.
3. **Dina innehav:** fyll `holdings.csv` (ticker, marknad, antal, inköp, datum)
   — direkt, eller via `holdings-editor.html` (nås på Pages bredvid dashboarden).
4. **(Valfritt) Börsdata-export** i `data/` enligt `BORSDATA-EXPORT.md` för
   multifaktorns värde/kvalitet och svensk PEAD.

## Sekretess
Dashboarden är **publik** (GitHub Pages är alltid publikt för vanliga konton).
Den är konfigurerad att **aldrig visa kronbelopp** — `dashboard.py` strippar bort
antal/inköp/värde/kr-resultat ur den publicerade JSON:en; bara tickers, %, pris
och trend syns. Vill du ha den helt privat senare: hosta på Netlify/Cloudflare
Pages med lösenord, eller kör `python dashboard.py` och öppna `docs/index.html`
lokalt. Telegram-larmen är alltid privata.

## Köra/testa lokalt (valfritt)
```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
# OBS Windows: denna maskin gör TLS-interception → yfinance/requests behöver en
# CA-bundle från Windows-storen (se .venv/sitecustomize.py + winca.pem som redan
# är uppsatta). Sätt även $env:PYTHONUTF8=1 så konsolen inte kraschar på Unicode.
.venv\Scripts\python scanner.py --dry-run --force
.venv\Scripts\python backtest.py universe/usa.csv SPY 20
.venv\Scripts\python dashboard.py            # -> docs/index.html
```

## Schemalagda körningar (sköts av GitHub Actions)
| Workflow | När | Gör |
|---|---|---|
| `scan.yml` | var 15:e min, börstid | volym/pris/PM/innehav + insiders |
| `daily.yml` | 21:30 UTC vardagar | PEAD → lead-lag → nedsidesvakt → innehav → larmlogg → dashboard |
| `monthly.yml` | 1:a varje månad | alla månadsstrategier + sektorer + regim + scorecard + dashboard |

Viktigast på sikt: låt **larmloggen** samla skarp data några veckor — den är
facit för om strategierna faktiskt fungerar, out-of-sample.
