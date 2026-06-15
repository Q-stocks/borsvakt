#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Börsvakt – Börsdata-export → breakout-universum

Konverterar en Börsdata screener-CSV (semikolon, Börsdata-tickrar, sektorindex
inblandade) till en ren Yahoo-formaterad lista som breakout.py kan läsa.

  • Hittar kolumnerna automatiskt (Ticker / Bolagsnamn / omsättning / sektor).
  • Konverterar Börsdata-tickrar → Yahoo (VOLV B → VOLV-B.ST) via stocks-modulen.
  • Filtrerar bort sektorindex (tickrar som SX502010PI).
  • Likviditetsgrind (valfri): behåll bara namn med snittomsättning >= --min-msek
    (standard 1,5 MSEK/dag) — håller tunt handlade First North-namn ute.

Körning:
  python borsdata_universe.py "C:/Users/<du>/Downloads/Borsdata_2026-06-15.csv"
  python borsdata_universe.py export.csv --out universe/sverige_broad.csv --min-msek 2
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

from stocks import borsdata_to_yahoo

ROOT = Path(__file__).resolve().parent
INDEX_RE = re.compile(r"^SX\d", re.IGNORECASE)   # Börsdata sektorindex, t.ex. SX502010PI


def _find(headers: list[str], *needles: str) -> str | None:
    for h in headers:
        hl = h.lower()
        if all(n in hl for n in needles):
            return h
    return None


def _num(s: str) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace("\xa0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def convert(src: Path, out: Path, min_msek: float) -> dict:
    # Börsdata-export är semikolon; encoding oftast UTF-8 (ev. BOM), annars cp1252.
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = src.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    reader = csv.DictReader(text.splitlines(), delimiter=";")
    headers = reader.fieldnames or []
    tcol = _find(headers, "ticker")
    ncol = _find(headers, "bolagsnamn") or _find(headers, "namn") or _find(headers, "name")
    vcol = _find(headers, "volume", "snitt") or _find(headers, "oms")
    scol = _find(headers, "sektor")
    if not tcol:
        raise SystemExit(f"Hittade ingen ticker-kolumn i {headers}")

    rows, seen = [], set()
    total = idx = illq = 0
    for r in reader:
        total += 1
        raw_t = (r.get(tcol) or "").strip()
        if not raw_t or INDEX_RE.match(raw_t):
            idx += 1
            continue
        if scol and not (r.get(scol) or "").strip():
            idx += 1                       # index/makro-rader saknar sektor
            continue
        if vcol and min_msek:
            tv = _num(r.get(vcol) or "")
            if tv is not None and tv < min_msek:
                illq += 1
                continue
        y = borsdata_to_yahoo(raw_t, "SE", {})
        if y in seen:
            continue
        seen.add(y)
        rows.append((y, (r.get(ncol) or raw_t).strip() if ncol else raw_t))

    rows.sort()
    out = out if out.is_absolute() else ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ticker", "namn"])
        w.writerows(rows)
    return {"total": total, "index_skipped": idx, "illiquid_skipped": illq,
            "kept": len(rows), "out": str(out), "cols": (tcol, ncol, vcol, scol)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Börsdata-export → breakout-universum")
    ap.add_argument("src", help="sökväg till Börsdata screener-CSV")
    ap.add_argument("--out", default="universe/sverige_broad.csv")
    ap.add_argument("--min-msek", type=float, default=1.5,
                    help="likviditetsgolv i MSEK/dag (0 = ingen filtrering)")
    args = ap.parse_args()
    info = convert(Path(args.src), Path(args.out), args.min_msek)
    print(f"Kolumner: ticker={info['cols'][0]!r} namn={info['cols'][1]!r} "
          f"oms={info['cols'][2]!r} sektor={info['cols'][3]!r}")
    print(f"Totalt {info['total']} rader → {info['kept']} aktier "
          f"({info['index_skipped']} index/makro bort, {info['illiquid_skipped']} illikvida bort)")
    print(f"Skrev {info['out']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
