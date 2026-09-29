"""FIFO-rapport over realiserte gevinster/tap, per token.

Norge: hver token regnes som egen formuesgjenstand, og FIFO gjelder. Hver swap er en
realisasjon. Merk at SOL-beinet i hver swap (SOL→token / token→SOL) også er en
realisasjon av SOL – dette dekkes IKKE her. For selve skattemeldingen bør du importere
walleten i et kryptoskatteverktøy og bruke denne rapporten som kontroll.

Valutakurs: USD/NOK fra Norges Bank (dagskurs, forrige tilgjengelige virkedag).
"""
from __future__ import annotations

import csv
import io
import time
from collections import defaultdict, deque

import requests

NB_URL = "https://data.norges-bank.no/api/data/EXR/B.USD.NOK.SP"


def fifo_realizations(trades: list[dict]) -> list[dict]:
    lots: dict[str, deque] = defaultdict(deque)   # mint -> [qty, unit_cost]
    rows = []
    for t in sorted(trades, key=lambda x: (x["ts"], x["id"])):
        mint = t["mint"]
        if t["side"] == "buy":
            unit = (t["usd"] + t.get("fee_usd", 0)) / t["qty"]
            lots[mint].append([t["qty"], unit])
            continue
        remaining, cost = t["qty"], 0.0
        while remaining > 1e-12 and lots[mint]:
            lot = lots[mint][0]
            take = min(remaining, lot[0])
            cost += take * lot[1]
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-12:
                lots[mint].popleft()
        proceeds = t["usd"] - t.get("fee_usd", 0)
        rows.append({
            "dato": time.strftime("%Y-%m-%d", time.gmtime(t["ts"])),
            "symbol": t.get("symbol"),
            "mint": mint,
            "antall": t["qty"],
            "salgssum_usd": round(proceeds, 2),
            "kostpris_usd": round(cost, 2),
            "gevinst_usd": round(proceeds - cost, 2),
            "mangler_kjopsgrunnlag": remaining > 1e-9,
            "tx": t.get("tx_sig"),
        })
    return rows


def fetch_usdnok(start: str, end: str) -> dict[str, float]:
    """Dagskurser fra Norges Bank {YYYY-MM-DD: kurs}."""
    r = requests.get(NB_URL, params={"format": "csv", "startPeriod": start, "endPeriod": end,
                                     "locale": "en"}, timeout=20)
    r.raise_for_status()
    first = r.text.splitlines()[0] if r.text else ""
    delim = ";" if first.count(";") > first.count(",") else ","
    rates = {}
    for row in csv.DictReader(io.StringIO(r.text), delimiter=delim):
        try:
            rates[row["TIME_PERIOD"]] = float(row["OBS_VALUE"])
        except (KeyError, ValueError):
            continue
    return rates


def rate_for(day: str, rates: dict[str, float]) -> float | None:
    earlier = [d for d in rates if d <= day]
    return rates[max(earlier)] if earlier else None


def add_nok(rows: list[dict], rates: dict[str, float]) -> list[dict]:
    for r in rows:
        k = rate_for(r["dato"], rates)
        r["usdnok"] = k
        r["gevinst_nok"] = round(r["gevinst_usd"] * k, 2) if k else None
    return rows


def write_csv(rows: list[dict], path: str) -> None:
    if not rows:
        open(path, "w").close()
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
