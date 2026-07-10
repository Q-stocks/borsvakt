# Börsdata-export — recept

Två CSV-filer driver `multifactor.py` och svensk `pead.py`. Parsrarna är
förlåtande (skiftlägesokänsliga, klarar `;` eller `,` som avgränsare,
kommatecken som decimal och `%`-tecken), men **kolumnrubrikerna måste
matcha det config.yaml letar efter** — eller tvärtom: sätt config efter
dina rubriker. Börsdata-tickrar som `VOLV B` konverteras automatiskt till
Yahoo (`VOLV-B.ST`); kantfall läggs i `ticker_overrides`.

---

## 1. Fundamenta-export → `data/fundamenta_sverige.csv`
**Används av:** multifaktor (värde + kvalitet) och Aktiemotorns kvalitetsfilter.

I Börsdatas screener/bolagslista, välj hela Stockholmsbörsen (Large + Mid +
Small Cap, gärna även First North) och lägg till dessa kolumner:

| Kolumn (rubrik i filen) | Vad | Roll |
|---|---|---|
| `Ticker` | bolagets kortnamn | nyckel |
| `Namn` | bolagsnamn | visning |
| `F-Score` | Piotroski F-score (0–9) | kvalitet (högre = bättre) |
| `EV/EBIT` | rörelsevärdering | värde (lägre = bättre) |

Exportera till Excel → spara som CSV (UTF-8) i `data/fundamenta_sverige.csv`.

Sätt sedan i `config.yaml`:
```yaml
multifactor:
  quality_column: "F-Score"
  value_column: "EV/EBIT"
  value_lower_better: true
stocks:                      # om du vill att Aktiemotorn använder samma fil
  universe_from_quality: true
  quality_column: "F-Score"
```
F-Score är medvetet valt: Börslabbet tar bort bolag med lägst F-score
**före** momentumranking, så det är rätt kvalitetsmått för den här designen.

---

## 2. Rapportexport → `data/earnings_sverige.csv`
**Används av:** svensk PEAD (vinstdrift). **AKTIVERAD i config 2026-07-02** —
marknaden är en (högljudd) no-op tills den här filen finns.

Exportera rapportdata för bolagsurvalet med kolumnerna:

| Kolumn | Vad | Krav |
|---|---|---|
| `Ticker` | kortnamn | nyckel |
| `Rapportdatum` | rapportdatum — **får ligga i framtiden!** | **måste vara `ÅÅÅÅ-MM-DD`** |
| `EPS` | redovisad vinst per aktie | valfri — bara för surprise-larmen |
| `EPS estimat` | konsensusestimat | valfri — bara för surprise-larmen |

Har Börsdata en färdig `Surprise`-kolumn (%) räcker den i stället för EPS +
estimat. Spara som CSV i `data/earnings_sverige.csv` och committa
(workflows läser filen ur repot — den innehåller inget privat).

> **Datumformat:** parsern kräver ISO (`2026-05-14`). Exporterar Börsdata
> ett annat format (`14/05/2026`) får Claude Code justera datumtolkningen
> i `pead.load_earnings_export` — säg till.

### Kalender-tricket: EN export per kvartal räcker

PEAD-larmet har **två oberoende utlösare**, och de ställer olika krav på filen:

| Utlösare | Krav på exporten | Täcks av |
|---|---|---|
| **Kursreaktion** ≥ 5 % mot index på rapportdagen | bara **rapportdatumet** — kursen mäts live från Yahoo när dagen passerat | rapport**kalendern**, exporterad i förväg |
| **Vinstöverraskning** (EPS ≥ 5 % över estimat) | EPS-siffrorna — finns först efter rapporten | uppdaterad export inom 5 dagar efter rapporten |

**Minsta arbetsinsats (rekommenderad start):** exportera Börsdatas
**rapportdatum-kalender** för kommande kvartal ~4 ggr/år (början av januari,
april, juli, oktober). Reaktionslarmen — som fångar de flesta stora
rapportöverraskningarna, eftersom stora EPS-slag brukar synas i kursen —
fungerar då **automatiskt hela säsongen** utan att du rör något.

**Ambitiös nivå:** uppdatera dessutom exporten med EPS-utfall var ~4:e dag
under rapportsäsong → även de tystare surprise-larmen (EPS-slag utan stor
kursreaktion) fångas inom 5-dagarsfönstret (`lookback_days`).

**Helautomatiskt (framtid):** Börsdata Pro+ API bakom samma
`load_earnings_export`-gränssnitt — säg till Claude Code när/om du uppgraderar.

Fundamenta-exporten (fil 1) är inte lika tidskänslig: multifaktor
ombalanseras månads-/årsvis, så en månatlig uppdatering räcker gott på Pro.

---

## 3. Breakout-universum → `data/breakout_sverige.csv` (eller direkt `universe/sverige_broad.csv`)
**Används av:** `breakout.py` (Qullamaggie-utbrott). Behöver **bredd** — backtesten
visade att 60 storbolag är för smalt (kapitalet bara ~7 % investerat). Här gäller
*ju fler likvida namn desto bättre*.

I Börsdatas screener, välj **Small Cap, Mid Cap, Large Cap** (och gärna **First
North**) och lägg till kolumnerna:

| Kolumn | Vad | Roll |
|---|---|---|
| `Ticker` | kortnamn (t.ex. `VOLV B`) | nyckel — konverteras automatiskt till Yahoo (`VOLV-B.ST`) |
| `Namn` | bolagsnamn | visning |
| `Lista` | Large/Mid/Small/First North | (valfritt) för att filtrera/sortera |
| `Oms/dag` | genomsnittlig daglig omsättning (kr) | **likviditetsgrind** — se nedan |

Exportera som CSV (Börsdata ger semikolon-CSV direkt — funkar) och kör
**konverteraren**, som fixar Yahoo-tickrar, filtrerar bort sektorindex
(`SX…PI`-rader) och likviditetsgallrar åt dig:

```bash
python borsdata_universe.py "C:/Users/<du>/Downloads/Borsdata_ÅÅÅÅ-MM-DD.csv"
# → skriver universe/sverige_broad.csv (Yahoo-format) — klart för breakout.py
#   --min-msek 2   för hårdare likviditetsgolv (standard 1,5 MSEK/dag)
```

Verifierat på en riktig export: 388 rader → 326 aktier (62 index bort),
**98 % täckning på Yahoo**. Kantfall (valutaklass/SDB) hoppas tyst över;
lägg ev. fix i `stocks.ticker_overrides`.

**First North & likviditet — viktigt:** Yahoos täckning och volymdata är ojämn för
First North/microbolag, och tunn handel förstör både RVOL-signalen och din chans att
faktiskt ta dig in/ur. Därför har `breakout.py` en **`min_avg_turnover`-grind** (i
SEK) i `config.yaml` — sätt den t.ex. till 1–2 Mkr/dag så filtreras de tunt
handlade namnen bort automatiskt, oavsett vilka du exporterat. Ta gärna med First
North i exporten; grinden sköter gallringen.

> Uppdateringstakt: börsintroduktioner/avnoteringar gör att listan glider — en
> **månatlig** ny export räcker gott (breakout-universumet behöver inte vara
> färskt på dagen, bara brett och likvidt).
