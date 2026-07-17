# -*- coding: utf-8 -*-
"""
marknadspuls.py – marknadsklimat: sentiment NU + ledande signaler FRAMÅT.

Fristående kontextvy (INGA larm, INGA signaler in i övriga moduler – jfr
designprincip 6: observerbar kontext, inte prognos-timing). Hämtar gratis
data från FRED, Yahoo, CNN Fear & Greed, alternative.me och NAAIM,
poängsätter enligt fasta trösklar och genererar en statisk HTML-sida.

Kanonisk version – den lokala mappen C:\\Users\\linus\\projects\\marknadspuls
kör SAMMA fil via sin uppdatera.bat (med egna --ut/--cache/--historik).

Körning i börsvakt (daily.yml, Pages-fliken):
    python marknadspuls.py --borsvakt
        -> docs/marknadspuls.html, cache/historik i log/

Fail-soft: varje källa faller tillbaka på senaste cache och markeras
"cachead"; fel listas i sidfoten. Kraschar aldrig på enskild källa.
"""
import argparse
import json
import math
import os
import re
import sys
import html as html_mod
from datetime import datetime, timezone
from urllib.parse import quote

from curl_cffi import requests as creq

ROOT = os.path.dirname(os.path.abspath(__file__))
# Sätts i main() (argparse). Defaults = börsvakt-repots layout.
CACHE_FIL = os.path.join(ROOT, "log", "marknadspuls_cache.json")
UT_FIL = os.path.join(ROOT, "docs", "marknadspuls.html")
HIST_FIL = os.path.join(ROOT, "log", "marknadspuls_historik.csv")

FEL = []  # (källa, felmeddelande) – visas i sidfoten


# ---------------------------------------------------------------- hämtning

def http_get(url, headers=None):
    # curl_cffi med Chrome-fingeravtryck: FRED (Akamai) blockerar vanliga
    # Python-klienter på TLS-nivå.
    return creq.get(url, impersonate="chrome", timeout=45, headers=headers or {})


def fetch_fred(serie):
    """FRED utan API-nyckel via fredgraph.csv. -> [(datum, värde), ...]"""
    r = http_get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={serie}")
    r.raise_for_status()
    ut = []
    for rad in r.text.strip().splitlines()[1:]:
        delar = rad.split(",")
        if len(delar) == 2 and delar[1] not in (".", ""):
            try:
                ut.append((delar[0], float(delar[1])))
            except ValueError:
                pass
    if not ut:
        raise ValueError("tom serie")
    return ut


def fetch_yahoo(symbol, rng="5y"):
    """Yahoo v8 chart-API. -> [(datum, stängning), ...]"""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}"
           f"?range={rng}&interval=1d")
    j = http_get(url).json()
    res = j["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0].get("close") or []
    ut = {}
    for t, c in zip(ts, closes):
        if c is not None:
            ut[datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")] = float(c)
    if not ut:
        raise ValueError("tom serie")
    return sorted(ut.items())


def fetch_cnn():
    """CNN Fear & Greed: totalscore + 7 delkomponenter. Kräver browser-headers (annars 418)."""
    h = {"Accept": "application/json, text/plain, */*",
         "Referer": "https://edition.cnn.com/",
         "Origin": "https://edition.cnn.com"}
    return http_get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                    headers=h).json()


def fetch_crypto_fng():
    j = http_get("https://api.alternative.me/fng/?limit=120").json()
    return j["data"]  # nyast först: value, value_classification, timestamp


def fetch_naaim():
    r = http_get("https://naaim.org/programs/naaim-exposure-index/")
    # Talet ligger i en egen <div> efter rubriken "...Exposure Index number is*:"
    m = re.search(r"Exposure\s+Index\s+number\s+is.{0,200}?>\s*(-?[0-9]{1,3}(?:\.[0-9]+)?)\s*<",
                  r.text, re.I | re.S)
    if not m:
        raise ValueError("hittade inte NAAIM-värdet på sidan")
    v = float(m.group(1))
    if not -50 <= v <= 250:
        raise ValueError(f"orimligt NAAIM-värde: {v}")
    return v


FRED_SERIER = [
    "T10Y3M", "T10Y2Y", "BAMLH0A0HYM2", "NFCI", "STLFSI4",
    "ICSA", "CCSA", "SAHMREALTIME", "UNRATE",
    "HTRUCKSSAAR", "TRUCKD11", "FRGSHPUSM649NCIS",
    "PERMIT", "AWHMAN", "NEWORDER", "UMCSENT",
    "RECPROUSM156N", "DRTSCILM",
    "DGS10", "DGS2", "DGS3MO", "DFF",
]
YAHOO_SYMBOLER = [
    "^GSPC", "^VIX", "^VIX3M", "^MOVE",
    "RSP", "SPY", "XLY", "XLP", "SPHB", "SPLV", "HYG", "LQD",
    "IYT", "SMH", "HG=F", "GC=F", "DX-Y.NYB", "BTC-USD", "CL=F",
]


def hamta_allt():
    cache = {}
    if os.path.exists(CACHE_FIL):
        try:
            with open(CACHE_FIL, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    data = {"fred": {}, "yahoo": {}, "cnn": None, "cfng": None, "naaim": None,
            "stale": []}

    for s in FRED_SERIER:
        try:
            data["fred"][s] = fetch_fred(s)
            print(f"  FRED {s}: {len(data['fred'][s])} obs, senaste {data['fred'][s][-1]}")
        except Exception as e:
            gammal = cache.get("fred", {}).get(s)
            if gammal:
                data["fred"][s] = [tuple(x) for x in gammal]
                data["stale"].append(f"FRED {s}")
            FEL.append((f"FRED {s}", str(e)[:120]))
            print(f"  FRED {s}: FEL {e}")

    for s in YAHOO_SYMBOLER:
        try:
            data["yahoo"][s] = fetch_yahoo(s)
            print(f"  Yahoo {s}: {len(data['yahoo'][s])} obs, senaste {data['yahoo'][s][-1]}")
        except Exception as e:
            gammal = cache.get("yahoo", {}).get(s)
            if gammal:
                data["yahoo"][s] = [tuple(x) for x in gammal]
                data["stale"].append(f"Yahoo {s}")
            FEL.append((f"Yahoo {s}", str(e)[:120]))
            print(f"  Yahoo {s}: FEL {e}")

    for nyckel, fn in [("cnn", fetch_cnn), ("cfng", fetch_crypto_fng),
                       ("naaim", fetch_naaim)]:
        try:
            data[nyckel] = fn()
            print(f"  {nyckel}: OK")
        except Exception as e:
            if cache.get(nyckel) is not None:
                data[nyckel] = cache[nyckel]
                data["stale"].append(nyckel)
            FEL.append((nyckel, str(e)[:120]))
            print(f"  {nyckel}: FEL {e}")

    try:
        os.makedirs(os.path.dirname(CACHE_FIL), exist_ok=True)
        with open(CACHE_FIL, "w", encoding="utf-8") as f:
            json.dump({k: data[k] for k in ("fred", "yahoo", "cnn", "cfng", "naaim")},
                      f, ensure_ascii=False)
    except Exception as e:
        print(f"  cache-skrivning misslyckades: {e}")
    return data


# ---------------------------------------------------------------- beräkning

def varden(serie):
    return [v for _, v in serie]


def sista(serie):
    return serie[-1][1]


def sista_datum(serie):
    return serie[-1][0]


def medel(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def yoy_pct(serie, steg=12):
    """Procentuell förändring mot `steg` observationer bakåt (12 för månadsdata)."""
    if len(serie) <= steg:
        return None
    _, nu = serie[-1]
    _, forr = serie[-1 - steg]
    if forr == 0:
        return None
    return (nu / forr - 1) * 100


def diff_bakat(serie, steg=12):
    if len(serie) <= steg:
        return None
    return serie[-1][1] - serie[-1 - steg][1]


def kvot_serie(a, b):
    """Datumalignad kvot mellan två (datum, värde)-serier."""
    db = dict(b)
    return [(d, v / db[d]) for d, v in a if d in db and db[d] != 0]


def mot_ma(serie, n):
    """Senaste värdet relativt glidande medel över n obs, i procent."""
    if len(serie) < n:
        return None
    xs = varden(serie)[-n:]
    m = medel(xs)
    return (serie[-1][1] / m - 1) * 100 if m else None


def procentil(serie, x=None, fonster=None):
    xs = varden(serie)
    if fonster:
        xs = xs[-fonster:]
    if x is None:
        x = xs[-1]
    if not xs:
        return None
    under = sum(1 for v in xs if v < x)
    return under / len(xs) * 100


def sv(v, dec=1, suffix=""):
    """Svensk talformatering: decimalkomma, mellanslag som tusentalsavgränsare."""
    if v is None:
        return "–"
    s = f"{v:,.{dec}f}".replace(",", " ").replace(".", ",")
    return s + suffix


# score: -2 (max risk-av/varning) .. +2 (max risk-på/styrka)
def status_av(score):
    if score is None:
        return "neutral"
    if score > 0.5:
        return "gron"
    if score < -0.5:
        return "rod"
    return "gul"


def trappa(v, granser, poang):
    """granser stigande; poang har len(granser)+1 element. v < granser[i] -> poang[i]."""
    if v is None:
        return None
    for g, p in zip(granser, poang):
        if v < g:
            return p
    return poang[-1]


def indikator(iid, namn, grupp, varde_str, datum, score, spark, forklaring,
              kalla, extra=""):
    return {"id": iid, "namn": namn, "grupp": grupp, "varde": varde_str,
            "datum": datum, "score": score, "status": status_av(score),
            "spark": spark, "forklaring": forklaring, "kalla": kalla,
            "extra": extra}


def glesa(xs, max_n=120):
    if len(xs) <= max_n:
        return xs
    steg = len(xs) / max_n
    return [xs[int(i * steg)] for i in range(max_n)]


def bygg_indikatorer(data):
    F, Y = data["fred"], data["yahoo"]
    ind, flaggor = [], []

    def spark_av(serie, n):
        return glesa(varden(serie)[-n:]) if serie else []

    # ============ NULÄGE ============
    if "^VIX" in Y:
        s = Y["^VIX"]
        v = sista(s)
        sc = trappa(v, [14, 17, 21, 26, 33], [1.5, 1, 0.25, -0.5, -1.25, -2])
        ind.append(indikator("vix", "VIX", "nu", sv(v, 1), sista_datum(s), sc,
                             spark_av(s, 250),
                             "Optionsmarknadens 30-dagarsvolatilitet – marknadens rädslotermometer.",
                             "Yahoo ^VIX"))
        if v > 33:
            flaggor.append(("varning", f"VIX över 33 ({sv(v,1)}) – akut stressläge (men extremnivåer har historiskt varit kontrariska köplägen)"))

    if "^VIX" in Y and "^VIX3M" in Y:
        ks = kvot_serie(Y["^VIX3M"], Y["^VIX"])
        if ks:
            v = sista(ks)
            sc = trappa(v, [0.92, 0.97, 1.03, 1.08], [-2, -1, 0, 1, 1.5])
            lage = "contango (lugn)" if v > 1 else "backwardation (stress)"
            ind.append(indikator("vixstrukt", "VIX-terminsstruktur (3m/1m)", "nu",
                                 sv(v, 2), ks[-1][0], sc, spark_av(ks, 250),
                                 "Kvot över 1 = normal oro långt fram (contango). Under 1 = akut panik nu. "
                                 "Flippen tillbaka över 1 efter stress är historiskt ett starkt köpläge.",
                                 "Yahoo ^VIX3M/^VIX", extra=lage))
            if v < 0.95:
                flaggor.append(("varning", "VIX-strukturen i backwardation – akut marknadsstress pågår"))
            senaste10 = varden(ks)[-10:]
            if v > 1.0 and any(x < 0.97 for x in senaste10[:-1]):
                flaggor.append(("styrka", "VIX-strukturen har just normaliserats efter stress – historiskt ett av de starkaste köplägena"))

    if "^MOVE" in Y:
        s = Y["^MOVE"]
        v = sista(s)
        sc = trappa(v, [80, 100, 120, 140], [1.5, 0.75, -0.25, -1, -2])
        ind.append(indikator("move", "MOVE (räntevolatilitet)", "nu", sv(v, 0),
                             sista_datum(s), sc, spark_av(s, 250),
                             "Obligationsmarknadens VIX. Räntestress spiller nästan alltid över på aktier.",
                             "Yahoo ^MOVE"))

    if data.get("cnn"):
        fg = data["cnn"].get("fear_and_greed", {})
        v = fg.get("score")
        if v is not None:
            histo = data["cnn"].get("fear_and_greed_historical", {}).get("data", [])
            spark = glesa([p["y"] for p in histo]) if histo else []
            sc = trappa(v, [25, 45, 55, 78], [-1.5, -0.75, 0, 0.75, 0.25])
            ind.append(indikator("cnnfg", "CNN Fear & Greed", "nu", sv(v, 0) + " / 100",
                                 str(fg.get("timestamp", ""))[:10], sc, spark,
                                 "CNN:s sammanvägning av 7 delsignaler: momentum, bredd, put/call, "
                                 "junk bonds, VIX, safe havens, nya toppar/bottnar.",
                                 "CNN", extra=str(fg.get("rating", ""))))
            if v < 20:
                flaggor.append(("styrka", f"Extrem rädsla i CNN Fear & Greed ({sv(v,0)}) – historiskt bra köpläge"))
            if v > 80:
                flaggor.append(("varning", f"Extrem girighet i CNN Fear & Greed ({sv(v,0)}) – sencykliskt varningstecken"))

    if data.get("cfng"):
        try:
            rad = data["cfng"][0]
            v = float(rad["value"])
            spark = [float(x["value"]) for x in reversed(data["cfng"])]
            sc = trappa(v, [25, 45, 55, 78], [-1.25, -0.5, 0, 0.75, 0.25])
            ind.append(indikator("cfng", "Krypto Fear & Greed", "nu", sv(v, 0) + " / 100",
                                 datetime.fromtimestamp(int(rad["timestamp"]), timezone.utc).strftime("%Y-%m-%d"),
                                 sc, spark,
                                 "Riskaptiten i marknadens mest spekulativa hörn – kanariefågeln för likviditet.",
                                 "alternative.me", extra=rad.get("value_classification", "")))
        except Exception as e:
            FEL.append(("cfng-parse", str(e)[:100]))

    if data.get("naaim") is not None:
        v = data["naaim"]
        sc = trappa(v, [20, 40, 65, 90], [-1.5, -0.75, 0, 0.75, 0.5])
        ind.append(indikator("naaim", "NAAIM-exponering", "nu", sv(v, 1) + " %",
                             "senaste veckan", sc, [],
                             "Aktiva förvaltares faktiska aktieexponering (kan vara −200 till +200 %). "
                             "Under 20 % har historiskt varit kontrariskt köpläge.",
                             "naaim.org"))
        if v < 20:
            flaggor.append(("styrka", f"NAAIM-exponering extremt låg ({sv(v,1)} %) – förvaltarna redan ur marknaden"))

    if "RSP" in Y and "SPY" in Y:
        ks = kvot_serie(Y["RSP"], Y["SPY"])
        m = mot_ma(ks, 100)
        if m is not None:
            sc = trappa(m, [-3, -1, 1, 3], [-1.5, -0.75, 0, 0.75, 1.5])
            ind.append(indikator("bredd", "Marknadsbredd (RSP/SPY)", "nu",
                                 sv(m, 1, " % vs 100d"), ks[-1][0], sc, spark_av(ks, 250),
                                 "Likaviktad vs kapitalviktad S&P 500. Fallande kvot = smal uppgång "
                                 "buren av ett fåtal jättar – historiskt skört läge.",
                                 "Yahoo RSP/SPY"))

    if "XLY" in Y and "XLP" in Y:
        ks = kvot_serie(Y["XLY"], Y["XLP"])
        m = mot_ma(ks, 100)
        if m is not None:
            sc = trappa(m, [-4, -1, 1, 4], [-1.5, -0.5, 0, 0.5, 1.5])
            ind.append(indikator("xlyxlp", "Riskaptit (XLY/XLP)", "nu",
                                 sv(m, 1, " % vs 100d"), ks[-1][0], sc, spark_av(ks, 250),
                                 "Sällanköpsvaror vs dagligvaror – vågar konsumentmarknaden ta risk?",
                                 "Yahoo XLY/XLP"))

    if "SPHB" in Y and "SPLV" in Y:
        ks = kvot_serie(Y["SPHB"], Y["SPLV"])
        m = mot_ma(ks, 100)
        if m is not None:
            sc = trappa(m, [-4, -1, 1, 4], [-1.5, -0.5, 0, 0.5, 1.5])
            ind.append(indikator("betalowvol", "Hög beta / låg vol (SPHB/SPLV)", "nu",
                                 sv(m, 1, " % vs 100d"), ks[-1][0], sc, spark_av(ks, 250),
                                 "Rotation mot högbeta = risk-på; flykt till lågvol = risk-av.",
                                 "Yahoo SPHB/SPLV"))

    if "HYG" in Y and "LQD" in Y:
        ks = kvot_serie(Y["HYG"], Y["LQD"])
        m = mot_ma(ks, 100)
        if m is not None:
            sc = trappa(m, [-2, -0.5, 0.5, 2], [-2, -1, 0, 0.75, 1.5])
            ind.append(indikator("hyglqd", "Kreditaptit (HYG/LQD)", "nu",
                                 sv(m, 1, " % vs 100d"), ks[-1][0], sc, spark_av(ks, 250),
                                 "High yield vs investment grade. Kreditmarknaden brukar sniffa problem före aktiemarknaden.",
                                 "Yahoo HYG/LQD"))

    if "^GSPC" in Y:
        s = Y["^GSPC"]
        pris = sista(s)
        xs = varden(s)
        ma50 = medel(xs[-50:]) if len(xs) >= 50 else None
        ma200 = medel(xs[-200:]) if len(xs) >= 200 else None
        ath = max(xs)
        fran_ath = (pris / ath - 1) * 100
        if ma50 and ma200:
            if pris > ma50 and pris > ma200:
                sc, lage = 1.5, "över 50d och 200d – upptrend"
            elif pris > ma200:
                sc, lage = 0.25, "över 200d men under 50d"
            elif pris > ma50:
                sc, lage = -0.5, "under 200d men över 50d"
            else:
                sc, lage = -1.75, "under 50d och 200d – nedtrend"
            ind.append(indikator("sptrend", "S&P 500-trend", "nu",
                                 sv(fran_ath, 1, " % från ATH"), sista_datum(s), sc,
                                 spark_av(s, 250),
                                 "Pris mot 50- och 200-dagars medel. Trendfilter är det enklaste som faktiskt fungerat.",
                                 "Yahoo ^GSPC", extra=lage))
            if pris < ma200:
                flaggor.append(("varning", "S&P 500 handlas under 200-dagars medelvärde"))

    if "BTC-USD" in Y:
        s = Y["BTC-USD"]
        xs = varden(s)
        ma200 = medel(xs[-200:]) if len(xs) >= 200 else None
        if ma200:
            m = (sista(s) / ma200 - 1) * 100
            sc = trappa(m, [-15, -5, 5, 30], [-1.5, -0.5, 0, 0.75, 1])
            ind.append(indikator("btc", "Bitcoin vs 200d", "nu", sv(m, 1, " %"),
                                 sista_datum(s), sc, spark_av(s, 250),
                                 "Global likviditets- och riskaptitsproxy – rör sig först när likviditeten svänger.",
                                 "Yahoo BTC-USD"))

    if "DX-Y.NYB" in Y:
        s = Y["DX-Y.NYB"]
        if len(s) > 63:
            f3m = (sista(s) / s[-64][1] - 1) * 100
            sc = trappa(f3m, [-4, -1, 2, 5], [0.75, 0.5, 0, -0.75, -1.5])
            ind.append(indikator("dxy", "Dollarindex, 3-mån", "nu", sv(f3m, 1, " %"),
                                 sista_datum(s), sc, spark_av(s, 250),
                                 "Snabbt stigande dollar = global åtstramning och risk-av; fallande = medvind för risk.",
                                 "Yahoo DXY"))

    if "CL=F" in Y:
        s = Y["CL=F"]
        if len(s) > 63:
            f3m = (sista(s) / s[-64][1] - 1) * 100
            sc = trappa(f3m, [-20, 25], [-0.5, 0, -0.75])
            ind.append(indikator("olja", "Olja (WTI), 3-mån", "nu",
                                 sv(sista(s), 0, " $ (") + sv(f3m, 0, " %)"),
                                 sista_datum(s), sc, spark_av(s, 250),
                                 "Snabba oljespikar har föregått avmattningar; kollaps signalerar efterfrågetapp.",
                                 "Yahoo CL=F"))

    # ============ FRAMTID ============
    if "T10Y3M" in F:
        s = F["T10Y3M"]
        v = sista(s)
        xs18 = varden(s)[-380:]
        var_inverterad = any(x < -0.05 for x in xs18)
        sc = trappa(v, [0, 0.4, 1.0], [-2, -0.75, 0.5, 1.25])
        extra = ""
        if v > 0.1 and var_inverterad:
            sc = min(sc, -0.5)
            extra = "åter-brantning efter inversion"
            flaggor.append(("varning", "10å–3m-kurvan har åter-brantat efter inversion – historiskt kommer recessionen ofta först EFTER att kurvan vänt upp"))
        elif v < 0:
            flaggor.append(("varning", "Räntekurvan 10å–3m är inverterad – har föregått varje recession sedan 1970"))
        ind.append(indikator("t10y3m", "Räntekurva 10 år − 3 mån", "framtid",
                             sv(v, 2, " pe"), sista_datum(s), sc, spark_av(s, 500),
                             "NY Feds favoritmått: inverterad före varje recession sedan 1970. "
                             "Bäst av alla enskilda indikatorer på 12 mån sikt.",
                             "FRED T10Y3M", extra=extra))

    if "T10Y2Y" in F:
        s = F["T10Y2Y"]
        v = sista(s)
        sc = trappa(v, [0, 0.3, 1.0], [-1.75, -0.5, 0.5, 1])
        ind.append(indikator("t10y2y", "Räntekurva 10 år − 2 år", "framtid",
                             sv(v, 2, " pe"), sista_datum(s), sc, spark_av(s, 500),
                             "Klassikern: inverterad före varje recession sedan 1955 med en enda felsignal (1966).",
                             "FRED T10Y2Y"))

    if "BAMLH0A0HYM2" in F:
        s = F["BAMLH0A0HYM2"]
        v = sista(s) * 100  # till baspunkter
        f3m = diff_bakat(s, 63)
        sc = trappa(v, [325, 450, 600, 800], [1.5, 0.5, -0.75, -1.5, -2])
        if f3m is not None and f3m * 100 > 75:
            sc = min(sc, -1)
        ind.append(indikator("hyspread", "High yield-spread", "framtid",
                             sv(v, 0, " bp"), sista_datum(s), sc, spark_av(s, 500),
                             "Kreditspreadar vidgas när defaultrisk prisas in – varnar tidigt för både ekonomi och börs.",
                             "FRED BAMLH0A0HYM2",
                             extra=(f"3-mån: {sv(f3m*100, 0, ' bp')}" if f3m is not None else "")))
        if v > 500:
            flaggor.append(("varning", f"High yield-spread över 500 bp ({sv(v,0)}) – kreditstress"))
        elif v < 350 and (f3m or 0) <= 0:
            flaggor.append(("styrka", "Kreditspreadar snäva och stabila – kreditmarknaden ser inga problem"))

    if "NFCI" in F:
        s = F["NFCI"]
        v = sista(s)
        sc = trappa(v, [-0.4, 0, 0.5], [1.25, 0.5, -0.75, -1.75])
        ind.append(indikator("nfci", "Finansiella förhållanden (NFCI)", "framtid",
                             sv(v, 2), sista_datum(s), sc, spark_av(s, 260),
                             "Chicago Feds sammanvägning av 105 delserier. Över 0 = stramare än historiskt snitt.",
                             "FRED NFCI"))

    if "STLFSI4" in F:
        s = F["STLFSI4"]
        v = sista(s)
        sc = trappa(v, [-0.5, 0.3, 1.2], [1, 0.25, -0.75, -1.75])
        ind.append(indikator("stlfsi", "Finansiell stress (St. Louis Fed)", "framtid",
                             sv(v, 2), sista_datum(s), sc, spark_av(s, 260),
                             "Systemstress i banker, räntor och volatilitet. Noll = normalläge.",
                             "FRED STLFSI4"))

    if "DRTSCILM" in F:
        s = F["DRTSCILM"]
        v = sista(s)
        sc = trappa(v, [0, 15, 35], [1, 0, -1, -1.75])
        ind.append(indikator("sloos", "Bankernas kreditgivning (SLOOS)", "framtid",
                             sv(v, 1, " %"), sista_datum(s), sc, spark_av(s, 60),
                             "Nettoandel banker som stramar åt företagslån. Leder kreditcykeln med 2–4 kvartal.",
                             "FRED DRTSCILM"))

    if "ICSA" in F:
        s = F["ICSA"]
        xs = varden(s)
        ma4 = medel(xs[-4:])
        ma4_forr = medel(xs[-56:-52]) if len(xs) >= 56 else None
        yoy = (ma4 / ma4_forr - 1) * 100 if ma4_forr else None
        sc = trappa(yoy, [-5, 5, 15, 30], [1.25, 0.5, -0.5, -1.25, -2]) if yoy is not None else None
        ind.append(indikator("claims", "Nyanmälda arbetslösa (4v-snitt)", "framtid",
                             sv(ma4 / 1000, 0, " tus") + (f" ({sv(yoy,1)} % å/å)" if yoy is not None else ""),
                             sista_datum(s), sc, spark_av(s, 156),
                             "Snabbaste arbetsmarknadsdatan som finns – vändningar i ekonomin syns här först.",
                             "FRED ICSA"))
        if yoy is not None and yoy > 15:
            flaggor.append(("varning", f"Nyanmälda arbetslösa upp {sv(yoy,0)} % å/å – arbetsmarknaden viker"))

    if "SAHMREALTIME" in F:
        s = F["SAHMREALTIME"]
        v = sista(s)
        sc = trappa(v, [0.2, 0.35, 0.5], [1.25, 0.25, -0.75, -2])
        ind.append(indikator("sahm", "Sahm-regeln", "framtid", sv(v, 2, " pe"),
                             sista_datum(s), sc, spark_av(s, 60),
                             "Arbetslöshetens 3-mån-snitt ≥ 0,50 pe över 12-mån-lägsta = recession. "
                             "Har träffat varje recession utan falska larm.",
                             "FRED SAHMREALTIME"))
        if v >= 0.5:
            flaggor.append(("varning", f"SAHM-REGELN UTLÖST ({sv(v,2)} pe) – historiskt betyder det att recessionen redan börjat"))

    if "RECPROUSM156N" in F:
        s = F["RECPROUSM156N"]
        v = sista(s)
        sc = trappa(v, [1, 10, 30], [1, 0, -1, -2])
        ind.append(indikator("recprob", "Recessionssannolikhet (modell)", "framtid",
                             sv(v, 1, " %"), sista_datum(s), sc, spark_av(s, 60),
                             "Chauvet–Piger-modellen: sannolikhet att USA redan är i recession, baserat på fyra månadsserier.",
                             "FRED RECPROUSM156N"))

    if "PERMIT" in F:
        s = F["PERMIT"]
        yoy = yoy_pct(s)
        sc = trappa(yoy, [-15, -5, 3, 15], [-1.75, -0.75, 0, 0.75, 1.5]) if yoy is not None else None
        ind.append(indikator("permit", "Byggnadslov", "framtid",
                             sv(sista(s), 0, " tus") + (f" ({sv(yoy,1)} % å/å)" if yoy is not None else ""),
                             sista_datum(s), sc, spark_av(s, 60),
                             "”Housing IS the business cycle” – byggloven är den klassiska LEI-komponenten.",
                             "FRED PERMIT"))

    if "AWHMAN" in F:
        s = F["AWHMAN"]
        d = diff_bakat(s, 12)
        sc = trappa(d, [-0.5, -0.15, 0.15], [-1.5, -0.5, 0.25, 1]) if d is not None else None
        ind.append(indikator("awhman", "Arbetstimmar tillverkning", "framtid",
                             sv(sista(s), 1, " h") + (f" ({sv(d,2)} å/å)" if d is not None else ""),
                             sista_datum(s), sc, spark_av(s, 60),
                             "Arbetsgivare skär timmar innan de skär tjänster – timmarna leder sysselsättningen.",
                             "FRED AWHMAN"))

    if "NEWORDER" in F:
        s = F["NEWORDER"]
        yoy = yoy_pct(s)
        sc = trappa(yoy, [-5, 0, 4, 10], [-1.5, -0.5, 0.25, 0.75, 1.5]) if yoy is not None else None
        ind.append(indikator("neworder", "Kärnkapitalvaror, nyorder", "framtid",
                             (f"{sv(yoy,1)} % å/å" if yoy is not None else "–"),
                             sista_datum(s), sc, spark_av(s, 60),
                             "Företagens investeringsvilja (capex ex försvar/flyg) – nominell serie, tolka mot inflationen.",
                             "FRED NEWORDER"))

    if "UMCSENT" in F:
        s = F["UMCSENT"]
        v = sista(s)
        p = procentil(s, fonster=300)
        sc = trappa(p, [15, 35, 65], [-1.25, -0.5, 0.25, 1]) if p is not None else None
        ind.append(indikator("umcsent", "Konsumentsentiment (Michigan)", "framtid",
                             sv(v, 1), sista_datum(s), sc, spark_av(s, 60),
                             "Hushållens humör leder konsumtionen – extremt lågt sentiment har dock ofta varit kontrariskt köpläge.",
                             "FRED UMCSENT"))

    if "HG=F" in Y and "GC=F" in Y:
        ks = kvot_serie(Y["HG=F"], Y["GC=F"])
        m = mot_ma(ks, 100)
        if m is not None:
            sc = trappa(m, [-6, -2, 2, 6], [-1.25, -0.5, 0, 0.5, 1.25])
            ind.append(indikator("koppargULD", "Koppar/guld-kvot", "framtid",
                                 sv(m, 1, " % vs 100d"), ks[-1][0], sc, spark_av(ks, 250),
                                 "Gundlachs favorit för tillväxt- och ränteriktning. OBS: centralbankernas "
                                 "guldköp sedan 2022 stör signalen – väg lätt.",
                                 "Yahoo HG=F/GC=F"))

    if "IYT" in Y and "SPY" in Y:
        ks = kvot_serie(Y["IYT"], Y["SPY"])
        m = mot_ma(ks, 100)
        if m is not None:
            sc = trappa(m, [-4, -1, 1, 4], [-1.25, -0.5, 0, 0.5, 1.25])
            ind.append(indikator("iyt", "Transporter vs S&P (Dow-teori)", "framtid",
                                 sv(m, 1, " % vs 100d"), ks[-1][0], sc, spark_av(ks, 250),
                                 "Dow-teorin: transporterna ska bekräfta – godset rör sig före vinsterna.",
                                 "Yahoo IYT/SPY"))

    if "SMH" in Y and "SPY" in Y:
        ks = kvot_serie(Y["SMH"], Y["SPY"])
        m = mot_ma(ks, 100)
        if m is not None:
            sc = trappa(m, [-6, -2, 2, 6], [-1.25, -0.5, 0, 0.5, 1.25])
            ind.append(indikator("smh", "Halvledare vs S&P", "framtid",
                                 sv(m, 1, " % vs 100d"), ks[-1][0], sc, spark_av(ks, 250),
                                 "Halvledarordrar är konjunkturens tidigaste orderbok – chipsen viker före allt annat.",
                                 "Yahoo SMH/SPY"))

    # ============ FRAKT & LASTBILAR ============
    if "HTRUCKSSAAR" in F:
        s = F["HTRUCKSSAAR"]
        v = sista(s) * 1000  # miljoner -> tusental
        yoy = yoy_pct(s)
        sc = trappa(yoy, [-15, -5, 3, 12], [-2, -1, 0, 0.75, 1.5]) if yoy is not None else None
        ind.append(indikator("trucks", "Tunga lastbilar, försäljning", "frakt",
                             sv(v, 0, " tus/år") + (f" ({sv(yoy,1)} % å/å)" if yoy is not None else ""),
                             sista_datum(s), sc, spark_av(s, 60),
                             "Flottor beställer lastbilar för behovet 6–12 mån fram. Branta fall har föregått "
                             "i stort sett varje recession sedan 1970-talet.",
                             "FRED HTRUCKSSAAR"))
        if yoy is not None and yoy < -10:
            flaggor.append(("varning", f"Tunga lastbilsförsäljningen ner {sv(abs(yoy),0)} % å/å – klassisk recessionssignal"))

    if "TRUCKD11" in F:
        s = F["TRUCKD11"]
        yoy = yoy_pct(s)
        sc = trappa(yoy, [-5, -1, 2, 6], [-1.5, -0.5, 0, 0.5, 1.25]) if yoy is not None else None
        ind.append(indikator("tonnage", "Truck-tonnage (ATA)", "frakt",
                             sv(sista(s), 1) + (f" ({sv(yoy,1)} % å/å)" if yoy is not None else ""),
                             sista_datum(s), sc, spark_av(s, 60),
                             "Hur mycket gods som faktiskt rullar på amerikanska vägar – ca 70 % av allt fraktat gods.",
                             "FRED TRUCKD11"))

    if "FRGSHPUSM649NCIS" in F:
        s = F["FRGSHPUSM649NCIS"]
        yoy = yoy_pct(s)
        sc = trappa(yoy, [-8, -2, 2, 8], [-1.5, -0.5, 0, 0.5, 1.25]) if yoy is not None else None
        ind.append(indikator("cass", "Cass Freight Index (volym)", "frakt",
                             sv(sista(s), 2) + (f" ({sv(yoy,1)} % å/å)" if yoy is not None else ""),
                             sista_datum(s), sc, spark_av(s, 60),
                             "Faktiska fraktbetalningar från 36 mdr $/år i fakturor. Föll 23 mån i rad in i 2026 "
                             "– längsta svackan sedan 2008–09. Frakten leder lager och BNP.",
                             "Cass via FRED"))

    # ============ RÄNTOR ============
    for fid, iid, namn, forkl in [
            ("DFF", "dff", "Fed funds (styrränta)", "Prislappen på pengar – allt annat prissätts härifrån."),
            ("DGS3MO", "dgs3mo", "3-mån statsränta", "Marknadens förväntan på Fed den närmaste tiden."),
            ("DGS2", "dgs2", "2-års statsränta", "Marknadens Fed-bana på 2 års sikt – rör sig före Fed."),
            ("DGS10", "dgs10", "10-års statsränta", "Diskonteringsräntan för allt: aktier, bolån, fastigheter.")]:
        if fid in F:
            s = F[fid]
            d3m = diff_bakat(s, 63 if fid != "DFF" else 90)
            riktning = "" if d3m is None else (f" ({'+' if d3m >= 0 else ''}{sv(d3m, 2)} pe 3m)")
            ind.append(indikator(iid, namn, "rantor", sv(sista(s), 2, " %") + riktning,
                                 sista_datum(s), None, spark_av(s, 500), forkl, f"FRED {fid}"))

    return ind, flaggor


def berakna_kompositer(ind):
    vikter_nu = {"vix": 1.5, "vixstrukt": 1.5, "move": 1, "cnnfg": 1, "cfng": 0.5,
                 "naaim": 1, "bredd": 1.25, "xlyxlp": 1, "betalowvol": 1,
                 "hyglqd": 1.5, "sptrend": 1.5, "btc": 0.5, "dxy": 0.5, "olja": 0.5}
    vikter_framtid = {"t10y3m": 2, "t10y2y": 1, "hyspread": 2, "nfci": 1.5,
                      "stlfsi": 1, "sloos": 1.5, "claims": 1.75, "sahm": 2,
                      "recprob": 1, "permit": 1.5, "awhman": 0.75, "neworder": 1,
                      "umcsent": 0.75, "koppargULD": 1, "iyt": 0.75, "smh": 0.75,
                      "trucks": 1.5, "tonnage": 1, "cass": 1}

    def vagt(vikter):
        summa, vsum = 0.0, 0.0
        for i in ind:
            if i["id"] in vikter and i["score"] is not None:
                summa += vikter[i["id"]] * i["score"]
                vsum += vikter[i["id"]]
        if vsum == 0:
            return None
        return round((summa / vsum + 2) / 4 * 100)

    return vagt(vikter_nu), vagt(vikter_framtid)


def logga_historik(nu, framtid):
    idag = datetime.now().strftime("%Y-%m-%d")
    rader = []
    if os.path.exists(HIST_FIL):
        with open(HIST_FIL, encoding="utf-8") as f:
            rader = [r for r in f.read().splitlines() if r.strip()]
    if not rader:
        rader = ["datum,nu,framtid"]
    rader = [r for r in rader if not r.startswith(idag)]
    rader.append(f"{idag},{nu},{framtid}")
    os.makedirs(os.path.dirname(HIST_FIL) or ".", exist_ok=True)
    with open(HIST_FIL, "w", encoding="utf-8") as f:
        f.write("\n".join(rader) + "\n")
    hist = []
    for r in rader[1:]:
        d = r.split(",")
        try:
            hist.append((d[0], float(d[1]), float(d[2])))
        except (ValueError, IndexError):
            pass
    return hist


# ---------------------------------------------------------------- HTML

# Tema: standalone = lokal fristående look; borsvakt = Pages-flikens look
TEMAN = {
    "standalone": {
        "%%BG%%": "#0d1117", "%%BGEXTRA%%": "none",
        "%%PANEL%%": "#161b22", "%%PANEL2%%": "#1f2630", "%%LINE%%": "#21262d",
        "%%TEXT%%": "#e6edf3", "%%MUTED%%": "#8b949e",
        "%%GRON%%": "#3fb950", "%%GUL%%": "#d29922", "%%ROD%%": "#f85149",
        "%%ACCENT%%": "#58a6ff", "%%ACCENT2%%": "#bc8cff",
        "%%FONT%%": "'Segoe UI', system-ui, sans-serif",
        "%%FONTHEAD%%": "'Segoe UI', system-ui, sans-serif",
        "%%FONTMONO%%": "Consolas, monospace",
    },
    "borsvakt": {
        "%%BG%%": "#0d1420",
        "%%BGEXTRA%%": "radial-gradient(1100px 500px at 82% -8%, rgba(233,185,73,.07), transparent 60%)",
        "%%PANEL%%": "#141e2e", "%%PANEL2%%": "#1b2840", "%%LINE%%": "#26385a",
        "%%TEXT%%": "#e9eef7", "%%MUTED%%": "#88a0c0",
        "%%GRON%%": "#4cc38a", "%%GUL%%": "#e9b949", "%%ROD%%": "#e5664f",
        "%%ACCENT%%": "#5fb0c9", "%%ACCENT2%%": "#e9b949",
        "%%FONT%%": "Inter, system-ui, sans-serif",
        "%%FONTHEAD%%": "'Space Grotesk', sans-serif",
        "%%FONTMONO%%": "'IBM Plex Mono', monospace",
    },
}

FONTLANK = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
            '<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700'
            '&family=IBM+Plex+Mono:wght@400;500;600&family=Inter:wght@400;500&display=swap" rel="stylesheet">')

CSS_MALL = """
:root { color-scheme: dark; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: %%BGEXTRA%%, %%BG%%; background-color: %%BG%%; color: %%TEXT%%;
  font-family: %%FONT%%; padding: 24px 18px 60px; line-height: 1.5; }
.wrap { max-width: 1240px; margin: 0 auto; }
h1 { font-size: 26px; letter-spacing: 0.5px; font-family: %%FONTHEAD%%; }
h1 .puls { color: %%ACCENT2%%; }
.tidsstampel { color: %%MUTED%%; font-size: 13px; margin-top: 4px; }
nav.flikar { display: flex; gap: 6px; border-bottom: 1px solid %%LINE%%; margin: 18px 0 8px; overflow-x: auto; }
nav.flikar a { color: %%MUTED%%; font-family: %%FONTHEAD%%; font-weight: 500; font-size: 14px;
  padding: 11px 14px; text-decoration: none; border-bottom: 2px solid transparent; white-space: nowrap; }
nav.flikar a:hover { color: %%TEXT%%; }
nav.flikar a.active { color: %%ACCENT2%%; border-bottom-color: %%ACCENT2%%; }
.oversikt { display: flex; flex-wrap: wrap; gap: 22px; align-items: stretch; margin: 26px 0; }
.gauge { background: %%PANEL%%; border: 1px solid %%LINE%%; border-radius: 12px; padding: 18px 26px 16px; text-align: center; flex: 0 0 250px; }
.gauge svg { width: 200px; }
.gauge-rubrik { font-size: 15px; font-weight: 600; margin-top: 2px; font-family: %%FONTHEAD%%; }
.gauge-etikett { font-size: 13px; font-weight: 700; margin-top: 2px; text-transform: uppercase; letter-spacing: 1px; }
.gauge-under { font-size: 11.5px; color: %%MUTED%%; margin-top: 5px; line-height: 1.35; }
.gauge-tal { font-size: 34px; font-weight: 700; margin: 20px 0; }
.flaggor { flex: 1 1 340px; display: flex; flex-direction: column; gap: 8px; min-width: 300px; }
.flagga { border-radius: 9px; padding: 9px 13px; font-size: 13.5px; line-height: 1.4; border: 1px solid; }
.flagga.varning { background: rgba(229,102,79,.09); border-color: rgba(229,102,79,.45); }
.flagga.varning::before { content: "⚠ "; }
.flagga.styrka { background: rgba(76,195,138,.09); border-color: rgba(76,195,138,.45); }
.flagga.styrka::before { content: "✔ "; }
.flagga.info { background: rgba(95,176,201,.08); border-color: rgba(95,176,201,.4); color: %%ACCENT%%; }
h2 { font-size: 18px; margin: 34px 0 6px; padding-top: 10px; border-top: 1px solid %%LINE%%; font-family: %%FONTHEAD%%; }
.sektion-forkl { color: %%MUTED%%; font-size: 13px; margin-bottom: 14px; }
.rutnat { display: grid; grid-template-columns: repeat(auto-fill, minmax(285px, 1fr)); gap: 14px; }
.kort { background: %%PANEL%%; border: 1px solid %%LINE%%; border-left: 4px solid %%MUTED%%; border-radius: 10px; padding: 13px 15px 11px; }
.kort.gron { border-left-color: %%GRON%%; }
.kort.gul { border-left-color: %%GUL%%; }
.kort.rod { border-left-color: %%ROD%%; }
.kort-topp { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.kort-namn { font-size: 13.5px; font-weight: 600; color: %%TEXT%%; font-family: %%FONTHEAD%%; }
.kort-varde { font-size: 17px; font-weight: 700; white-space: nowrap; font-family: %%FONTMONO%%; }
.kort.gron .kort-varde { color: %%GRON%%; } .kort.gul .kort-varde { color: %%GUL%%; }
.kort.rod .kort-varde { color: %%ROD%%; } .kort.neutral .kort-varde { color: %%TEXT%%; }
.kort-extra { font-size: 12px; color: %%GUL%%; margin-top: 1px; }
.spark { width: 100%; height: 42px; margin: 7px 0 4px; }
.kort-forkl { font-size: 11.8px; color: %%MUTED%%; line-height: 1.45; }
.kort-meta { font-size: 10.5px; color: %%MUTED%%; opacity: .75; margin-top: 7px; display: flex; justify-content: space-between; font-family: %%FONTMONO%%; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 4px; }
.chip { background: %%PANEL%%; border: 1px solid %%LINE%%; border-radius: 20px; padding: 5px 13px; font-size: 12px; color: %%TEXT%%; }
.chip b { font-size: 12.5px; font-family: %%FONTMONO%%; }
.metodik { background: %%PANEL%%; border: 1px solid %%LINE%%; border-radius: 10px; padding: 18px 22px; margin-top: 14px; font-size: 13px; line-height: 1.65; color: %%MUTED%%; }
.metodik h3 { color: %%TEXT%%; font-size: 14px; margin: 12px 0 4px; font-family: %%FONTHEAD%%; }
.metodik a { color: %%ACCENT%%; text-decoration: none; }
.fotnot { color: %%MUTED%%; opacity: .8; font-size: 11.5px; margin-top: 26px; line-height: 1.6; }
"""


def spark_svg(xs, farg, w=170, h=42):
    if not xs or len(xs) < 2:
        return ""
    mn, mx = min(xs), max(xs)
    span = (mx - mn) or 1
    pts = []
    for i, v in enumerate(xs):
        x = i / (len(xs) - 1) * (w - 4) + 2
        y = h - 4 - (v - mn) / span * (h - 10)
        pts.append(f"{x:.1f},{y:.1f}")
    poly = " ".join(pts)
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none">'
            f'<polyline points="{poly}" fill="none" stroke="{farg}" stroke-width="1.6" '
            f'stroke-linejoin="round" opacity="0.9"/>'
            f'<circle cx="{pts[-1].split(",")[0]}" cy="{pts[-1].split(",")[1]}" r="2.4" fill="{farg}"/></svg>')


def _polar(cx, cy, r, grader):
    rad = math.radians(grader)
    return cx + r * math.cos(rad), cy - r * math.sin(rad)


def _bage(cx, cy, r, fran, till, farg, bredd):
    x1, y1 = _polar(cx, cy, r, fran)
    x2, y2 = _polar(cx, cy, r, till)
    stor = 1 if abs(fran - till) > 180 else 0
    return (f'<path d="M {x1:.1f} {y1:.1f} A {r} {r} 0 {stor} 1 {x2:.1f} {y2:.1f}" '
            f'fill="none" stroke="{farg}" stroke-width="{bredd}" stroke-linecap="round"/>')


def gauge_svg(varde, rubrik, underrubrik, farg_map, text_farg):
    if varde is None:
        return f'<div class="gauge"><div class="gauge-rubrik">{rubrik}</div><div class="gauge-tal">–</div></div>'
    vinkel = 180 - varde / 100 * 180
    nx, ny = _polar(100, 100, 62, vinkel)
    if varde >= 60:
        farg, etikett = farg_map["gron"], "Risk-på"
    elif varde >= 40:
        farg, etikett = farg_map["gul"], "Blandat"
    else:
        farg, etikett = farg_map["rod"], "Risk-av"
    return f'''<div class="gauge">
<svg viewBox="0 0 200 118">
{_bage(100, 100, 84, 180, 109, farg_map["rod"], 13)}
{_bage(100, 100, 84, 107, 73, farg_map["gul"], 13)}
{_bage(100, 100, 84, 71, 0, farg_map["gron"], 13)}
<line x1="100" y1="100" x2="{nx:.1f}" y2="{ny:.1f}" stroke="{text_farg}" stroke-width="3.5" stroke-linecap="round"/>
<circle cx="100" cy="100" r="6" fill="{text_farg}"/>
<text x="100" y="82" text-anchor="middle" font-size="30" font-weight="700" fill="{farg}">{varde}</text>
</svg>
<div class="gauge-rubrik">{rubrik}</div>
<div class="gauge-etikett" style="color:{farg}">{etikett}</div>
<div class="gauge-under">{underrubrik}</div>
</div>'''


CNN_KOMPONENT_NAMN = {
    "market_momentum_sp500": "Momentum S&P 500",
    "stock_price_strength": "Nya toppar/bottnar",
    "stock_price_breadth": "Volymbredd (McClellan)",
    "put_call_options": "Put/call-optioner",
    "market_volatility_vix": "VIX-komponent",
    "junk_bond_demand": "Junk bond-efterfrågan",
    "safe_haven_demand": "Safe haven-efterfrågan",
}
RATING_SV = {"extreme fear": "extrem rädsla", "fear": "rädsla", "neutral": "neutral",
             "greed": "girighet", "extreme greed": "extrem girighet"}


def bygg_html(ind, flaggor, nu, framtid, hist, data, borsvakt=False):
    tema = TEMAN["borsvakt" if borsvakt else "standalone"]
    css = CSS_MALL
    for k, v in tema.items():
        css = css.replace(k, v)
    farg_map = {"gron": tema["%%GRON%%"], "gul": tema["%%GUL%%"],
                "rod": tema["%%ROD%%"], "neutral": tema["%%MUTED%%"]}

    def kort_html(i):
        farg = farg_map[i["status"]]
        extra = f'<div class="kort-extra">{html_mod.escape(str(i["extra"]))}</div>' if i["extra"] else ""
        spark = spark_svg(i["spark"], farg) if i["spark"] else ""
        return f'''<div class="kort {i["status"]}">
<div class="kort-topp"><span class="kort-namn">{html_mod.escape(i["namn"])}</span>
<span class="kort-varde">{i["varde"]}</span></div>
{extra}{spark}
<div class="kort-forkl">{html_mod.escape(i["forklaring"])}</div>
<div class="kort-meta"><span>{html_mod.escape(i["kalla"])}</span><span>{html_mod.escape(str(i["datum"]))}</span></div>
</div>'''

    per_grupp = {}
    for i in ind:
        per_grupp.setdefault(i["grupp"], []).append(i)

    nu_tid = datetime.now().strftime("%Y-%m-%d %H:%M")
    delar = [f'''<!DOCTYPE html><html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Marknadspuls{" – Börsvakt" if borsvakt else ""}</title>
{FONTLANK if borsvakt else ""}<style>{css}</style></head><body><div class="wrap">''']

    if borsvakt:
        delar.append(f'''<h1>Börsvakt<span class="puls">.</span></h1>
<div class="tidsstampel">marknadspuls — sentiment nu &amp; ledande signaler · uppdaterad {nu_tid} (UTC)</div>
<nav class="flikar">
<a href="index.html#cockpit">Cockpit</a>
<a href="index.html#scorecard">Scorecard</a>
<a href="index.html#innehav">Innehav</a>
<a href="index.html#historik">Historik</a>
<a class="active" href="marknadspuls.html">Marknadspuls</a>
</nav>''')
    else:
        delar.append(f'''<h1>Marknads<span class="puls">puls</span></h1>
<div class="tidsstampel">Uppdaterad {nu_tid} &nbsp;·&nbsp; kör <code>uppdatera.bat</code> för färsk data</div>''')

    # Översikt
    delar.append('<div class="oversikt">')
    delar.append(gauge_svg(nu, "Nuläget", "Sentiment & riskaptit i marknaden just nu (dagar–veckor)",
                           farg_map, tema["%%TEXT%%"]))
    delar.append(gauge_svg(framtid, "Framtidsblicken", "Ledande konjunktur- & kreditsignaler (3–18 månader)",
                           farg_map, tema["%%TEXT%%"]))
    delar.append('<div class="flaggor">')
    if not any(t == "varning" for t, _ in flaggor):
        delar.append('<div class="flagga styrka">Inga klassiska recessions- eller stressflaggor aktiva just nu</div>')
    for typ, text in flaggor:
        delar.append(f'<div class="flagga {typ}">{html_mod.escape(text)}</div>')
    if data["stale"]:
        delar.append(f'<div class="flagga info">Cachead (ej färsk) data används för: {", ".join(data["stale"][:8])}</div>')
    delar.append('</div></div>')

    # Nuläge
    delar.append('<h2>Nuläget – sentiment &amp; riskaptit</h2>'
                 '<div class="sektion-forkl">Vad marknaden känner och gör i dag. Grönt = sunt risk-på, rött = stress/risk-av. Extremer är ofta kontrariska.</div>')
    delar.append('<div class="rutnat">')
    for i in per_grupp.get("nu", []):
        delar.append(kort_html(i))
    delar.append('</div>')

    # CNN-komponenter som chips
    if data.get("cnn"):
        chips = []
        for nyckel, namn in CNN_KOMPONENT_NAMN.items():
            k = data["cnn"].get(nyckel)
            if isinstance(k, dict) and k.get("score") is not None:
                rating = RATING_SV.get(str(k.get("rating", "")).lower(), k.get("rating", ""))
                chips.append(f'<span class="chip">{namn}: <b>{sv(k["score"], 0)}</b> ({rating})</span>')
        if chips:
            delar.append('<div class="sektion-forkl" style="margin-top:12px">CNN:s sju delkomponenter (0 = extrem rädsla, 100 = extrem girighet):</div>')
            delar.append('<div class="chips">' + "".join(chips) + '</div>')

    # Framtid
    delar.append('<h2>Framtidsblicken – ledande indikatorer</h2>'
                 '<div class="sektion-forkl">Signaler som historiskt legat 3–18 månader före ekonomin och börsen: räntekurvor, kredit, arbetsmarknad, byggande, capex och cykliska kvoter. Observerbar kontext – ingen prognos.</div>')
    delar.append('<div class="rutnat">')
    for i in per_grupp.get("framtid", []):
        delar.append(kort_html(i))
    delar.append('</div>')

    # Frakt & lastbilar
    delar.append('<h2>Frakt &amp; lastbilar – realekonomins puls</h2>'
                 '<div class="sektion-forkl">Godset ljuger inte: lastbilsköp och fraktvolymer visar vad företagen faktiskt tror om efterfrågan 6–12 månader fram.</div>')
    delar.append('<div class="rutnat">')
    for i in per_grupp.get("frakt", []):
        delar.append(kort_html(i))
    delar.append('</div>')

    # Räntor
    delar.append('<h2>Räntor</h2>'
                 '<div class="sektion-forkl">Räntenivåerna och kurvans form – motorn bakom både Framtidsblicken och värderingen av allt annat.</div>')
    delar.append('<div class="rutnat">')
    for i in per_grupp.get("rantor", []):
        delar.append(kort_html(i))
    delar.append('</div>')

    # Egen indexhistorik
    if len(hist) >= 2:
        nu_xs = [h[1] for h in hist]
        fr_xs = [h[2] for h in hist]
        delar.append('<h2>Marknadspulsens egen historik</h2>'
                     '<div class="sektion-forkl">Kompositindexens utveckling över körningarna.</div>')
        delar.append('<div class="rutnat">')
        delar.append(f'<div class="kort neutral"><div class="kort-topp"><span class="kort-namn">Nuläget över tid</span>'
                     f'<span class="kort-varde">{sv(nu_xs[-1],0)}</span></div>{spark_svg(nu_xs, tema["%%ACCENT%%"])}'
                     f'<div class="kort-meta"><span>{hist[0][0]}</span><span>{hist[-1][0]}</span></div></div>')
        delar.append(f'<div class="kort neutral"><div class="kort-topp"><span class="kort-namn">Framtidsblicken över tid</span>'
                     f'<span class="kort-varde">{sv(fr_xs[-1],0)}</span></div>{spark_svg(fr_xs, tema["%%ACCENT2%%"])}'
                     f'<div class="kort-meta"><span>{hist[0][0]}</span><span>{hist[-1][0]}</span></div></div>')
        delar.append('</div>')

    # Metodik
    delar.append('''<h2>Metodik &amp; källor</h2><div class="metodik">
<p>Varje indikator poängsätts −2 … +2 enligt fasta trösklar (se <code>marknadspuls.py</code>) och vägs ihop till två index 0–100:
<b>Nuläget</b> (sentiment/riskaptit, tyngst vikt på VIX-struktur, kredit och trend) och <b>Framtidsblicken</b>
(tyngst vikt på räntekurvan 10å−3m, high yield-spreadar, Sahm-regeln, nyanmälda arbetslösa och tunga lastbilar).
Över 60 = risk-på, under 40 = risk-av. Detta är observerbar kontext, ingen prognos och inga köp/säljsignaler.</p>
<h3>Varför just dessa? Det här har historiskt fungerat:</h3>
<p>• <b>Räntekurvan (10å−3m)</b> – inverterad före varje amerikansk recession sedan 1970; enligt Chicago Fed och NY Fed
den bästa enskilda prediktorn på ~12 mån sikt.<br>
• <b>Sahm-regeln</b> – träffat varje recession sedan 1970 utan falska larm, i realtid.<br>
• <b>High yield-spreadar &amp; SLOOS</b> – kreditmarknaden stramar åt före realekonomin viker.<br>
• <b>Tunga lastbilar &amp; frakt</b> – flottorna köper för morgondagens efterfrågan; Cass-index föll 23 månader i rad in i 2026,
längsta svackan sedan finanskrisen.<br>
• <b>VIX-terminsstruktur</b> – backwardation→contango-flippar har historiskt gett kraftig positiv edge på 1–4 veckors sikt.<br>
• <b>Bredd, transporter, halvledare, koppar/guld</b> – bekräftelsesignaler; smal marknad och vikande cykliska kvoter
har ofta föregått toppar. Koppar/guld väger lätt pga centralbankernas guldköp sedan 2022.</p>
<h3>Källor</h3>
<p><a href="https://fred.stlouisfed.org">FRED (St. Louis Fed)</a> ·
<a href="https://finance.yahoo.com">Yahoo Finance</a> ·
<a href="https://edition.cnn.com/markets/fear-and-greed">CNN Fear &amp; Greed</a> ·
<a href="https://alternative.me/crypto/fear-and-greed-index/">alternative.me</a> ·
<a href="https://naaim.org/programs/naaim-exposure-index/">NAAIM</a> ·
<a href="https://www.chicagofed.org/publications/chicago-fed-letter/2019/425">Chicago Fed: vilka indikatorer träffade recessioner</a> ·
<a href="https://www.newyorkfed.org/medialibrary/media/research/capital_markets/ycfaq.pdf">NY Fed om räntekurvan</a></p>
<p style="margin-top:8px">⚠ Inget här är en köp/säljsignal – det är en väderkarta, inte en autopilot. Enskilda indikatorer
felar ofta; styrkan ligger i att många oberoende signaler pekar åt samma håll samtidigt.</p></div>''')

    if FEL:
        felrader = "<br>".join(html_mod.escape(f"{k}: {m}") for k, m in FEL[:12])
        delar.append(f'<div class="fotnot"><b>Datafel denna körning:</b><br>{felrader}</div>')
    slutrad = ("Uppdateras automatiskt av börsvakts daily-körning (~06 svensk tid) · manuell körning: Actions → Marknadspuls"
               if borsvakt else "uppdatera med uppdatera.bat")
    delar.append(f'<div class="fotnot">Genererad av marknadspuls.py · {len(ind)} indikatorer · all data gratis/offentlig · {slutrad}</div>')
    delar.append('</div></body></html>')
    return "".join(delar)


# ---------------------------------------------------------------- huvud

def main(argv=None):
    global CACHE_FIL, UT_FIL, HIST_FIL
    p = argparse.ArgumentParser(description="Marknadspuls – sentiment nu + ledande signaler")
    p.add_argument("--ut", default=UT_FIL, help="utfil (HTML)")
    p.add_argument("--cache", default=CACHE_FIL, help="cachefil (JSON)")
    p.add_argument("--historik", default=HIST_FIL, help="historikfil (CSV)")
    p.add_argument("--borsvakt", action="store_true",
                   help="börsvakt-tema + fliknavigering (Pages-fliken)")
    a = p.parse_args(argv)
    UT_FIL, CACHE_FIL, HIST_FIL = a.ut, a.cache, a.historik

    print("Hämtar data ...")
    data = hamta_allt()
    print("Beräknar indikatorer ...")
    ind, flaggor = bygg_indikatorer(data)
    nu, framtid = berakna_kompositer(ind)
    print(f"Nuläget: {nu}/100, Framtidsblicken: {framtid}/100, "
          f"{len(ind)} indikatorer, {len(flaggor)} flaggor, {len(FEL)} fel")
    hist = logga_historik(nu, framtid)
    html_ut = bygg_html(ind, flaggor, nu, framtid, hist, data, borsvakt=a.borsvakt)
    os.makedirs(os.path.dirname(UT_FIL) or ".", exist_ok=True)
    with open(UT_FIL, "w", encoding="utf-8") as f:
        f.write(html_ut)
    print(f"Skrev {UT_FIL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
