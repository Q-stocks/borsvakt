#!/usr/bin/env python3
"""Schemavakt: DEN enda schemaläggaren för daily.yml och monthly.yml.

GitHubs cron-schemaläggare var opålitlig på det här repot: körningar
kom 10+ timmar sent eller uteblev (daily-cronen 21:30 UTC landade i
praktiken 08–12 UTC nästa förmiddag, mitt under handelsdagen;
monthly-cronen 06:15 den 1:a uteblev 2026-07-01 tills manuell trigg och
firade sedan 16:08 = dubbelkörning). Därför har daily/monthly INGEN
egen cron längre — det här skriptet körs FÖRST i varje scan (pålitligt
var 15:e minut via extern pinger, se PINGER.md; GitHubs sporadiska
scan-cron är backup-hjärtslag) och dispatchar dem via workflow_dispatch:

- daily.yml  : ska ha en lyckad körning efter senaste vardagsstängning
               (21:30 UTC). Saknas en sådan dispatchas den — i praktiken
               vid första pinget ~04:00 UTC nästa morgon, dvs. larmen
               kommer FÖRE börsöppning, på färdiga dagsstängningar.
- monthly.yml: ska ha en lyckad körning under innevarande kalendermånad,
               tidigast 06:15 UTC den 1:a (första vardagen därefter om
               den 1:a är en helg — scan körs bara vardagar).

Skulle en dubbelkörning ändå ske (manuell trigg etc.) är den ofarlig —
motorerna är idempotenta och larmen dedupas via state (verifierat
2026-07-01) — men vakten undviker dem: den hoppar över om en körning
redan är igång/köad eller nyss misslyckats (backoff). Max en dispatch
per varv, eftersom concurrency-gruppen borsvakt-state bara har EN
pending-plats och en nyare köad körning avbryter en äldre pending.

Endast standardbibliotek (körs före pip install). Fel här får ALDRIG
fälla skannern — allt är fail-soft och exit-koden är alltid 0.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"
REPO = os.environ.get("GITHUB_REPOSITORY", "Q-stocks/borsvakt")
TOKEN = os.environ.get("GITHUB_TOKEN", "")


def api(path, data=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {TOKEN}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "borsvakt-schemavakt",
        },
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        return json.loads(body) if body.strip() else {}


def workflow_runs(wf):
    """Senaste körningarna för en workflow-fil, nyast först."""
    d = api(f"/repos/{REPO}/actions/workflows/{wf}/runs?per_page=20")
    runs = []
    for r in d.get("workflow_runs", []):
        runs.append({
            "created": datetime.strptime(r["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc),
            "status": r["status"],
            "conclusion": r["conclusion"],
        })
    return runs


def dispatch(wf, why):
    api(f"/repos/{REPO}/actions/workflows/{wf}/dispatches", {"ref": "main"})
    print(f"Schemavakt: dispatchar {wf} – {why}")


def needs_dispatch(runs, target, now):
    """Ska workflowen dispatchas? target = tidpunkt en lyckad körning måste ligga efter."""
    for r in runs:
        if r["status"] in ("queued", "in_progress", "waiting", "requested", "pending"):
            return False  # kör redan eller står i kö
    if any(r["conclusion"] == "success" and r["created"] >= target for r in runs):
        return False  # redan avklarad för det här fönstret
    # Backoff: nyligt misslyckat/avbrutet försök i SAMMA fönster får tid på sig
    # innan omförsök (avbrutna beror oftast på concurrency-köns pending-plats
    # och kan tas om snabbare än riktiga fel).
    attempts = [r for r in runs if r["created"] >= target]
    if attempts:
        latest = max(attempts, key=lambda r: r["created"])
        wait = timedelta(minutes=45) if latest["conclusion"] == "cancelled" else timedelta(hours=4)
        if now - latest["created"] < wait:
            return False
    return True


def last_close_deadline(now):
    """Senaste vardags-21:30 UTC (daily-cronens avsedda tid) strikt före nu."""
    t = now.replace(hour=21, minute=30, second=0, microsecond=0)
    if now < t:
        t -= timedelta(days=1)
    while t.weekday() >= 5:  # lör/sön -> backa till fredag
        t -= timedelta(days=1)
    return t


def main():
    if not TOKEN:
        print("Schemavakt: GITHUB_TOKEN saknas – hoppar över.")
        return
    now = datetime.now(timezone.utc)

    # 1) Månadssignaler – en lyckad körning per kalendermånad, tidigast 06:15 den 1:a.
    try:
        due = now.replace(day=1, hour=6, minute=15, second=0, microsecond=0)
        if now >= due:
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if needs_dispatch(workflow_runs("monthly.yml"), month_start, now):
                dispatch("monthly.yml", f"ingen lyckad månadskörning ännu i {now:%Y-%m}")
                return  # max en dispatch per varv (concurrency-gruppens pending-plats)
            print("Schemavakt: månadssignalerna OK.")
    except Exception as e:  # noqa: BLE001 - vakten får aldrig fälla skannern
        print(f"Schemavakt: kunde inte kontrollera monthly ({e}) – ignorerar.", file=sys.stderr)

    # 2) Nedsidesvakten – en lyckad körning efter senaste vardagsstängning.
    try:
        target = last_close_deadline(now)
        if needs_dispatch(workflow_runs("daily.yml"), target, now):
            dispatch("daily.yml", f"ingen lyckad daglig körning sedan stängningen {target:%Y-%m-%d %H:%M} UTC")
        else:
            print("Schemavakt: nedsidesvakten OK.")
    except Exception as e:  # noqa: BLE001
        print(f"Schemavakt: kunde inte kontrollera daily ({e}) – ignorerar.", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"Schemavakt: oväntat fel ({e}) – skannern fortsätter ändå.", file=sys.stderr)
    sys.exit(0)
