"""PumpPortal-integrasjon (https://pumpportal.fun).

- MigrationListener: gratis websocket-strøm (subscribeMigration) som lagrer tokens som
  forlater bonding-kurven. Disse blir kandidater når de har passert min_age_days.
  Kjør som egen prosess: `python -m memebot listen`.
- build_local_trade: Local Transaction API (0,5 % gebyr). Returnerer en usignert
  transaksjon som signeres lokalt – privatnøkkelen sendes aldri til PumpPortal.

Regel fra PumpPortal: bruk ÉN websocket-forbindelse og send alle abonnementer på den.
Gjentatte tilkoblinger kan gi midlertidig utestengelse.
"""
from __future__ import annotations

import asyncio
import json
import logging

import requests

from .storage import Store

WS_URL = "wss://pumpportal.fun/api/data"
TRADE_LOCAL_URL = "https://pumpportal.fun/api/trade-local"
POOLS = {"pump", "raydium", "pump-amm", "launchlab", "raydium-cpmm", "bonk", "auto"}

log = logging.getLogger("memebot.pumpportal")


def parse_migration(msg: dict) -> dict | None:
    """Tolker en migrasjonsmelding defensivt (feltnavn kan variere)."""
    mint = msg.get("mint") or msg.get("token") or msg.get("ca")
    if not mint or msg.get("message"):  # 'message' = abonnementsbekreftelse
        return None
    tx_type = (msg.get("txType") or "").lower()
    if tx_type and tx_type not in ("migrate", "migration"):
        return None
    return {"mint": mint, "symbol": msg.get("symbol"), "pool": msg.get("pool")}


async def _listen(store: Store, api_key: str | None) -> None:
    import websockets  # importeres her så resten av boten virker uten pakken

    url = f"{WS_URL}?api-key={api_key}" if api_key else WS_URL
    backoff = 5
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                log.info("Koblet til PumpPortal – lytter etter migrasjoner")
                backoff = 5
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    m = parse_migration(msg)
                    if m:
                        store.add_migration(m["mint"], m["symbol"], m["pool"], msg)
                        log.info("Migrasjon: %s (%s)", m["symbol"] or "?", m["mint"])
        except Exception as e:  # nettverksbrudd o.l.
            log.warning("Websocket brutt (%s). Ny tilkobling om %ss", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)  # rolig backoff for å unngå ban


def run_listener(store: Store, api_key: str | None = None) -> None:
    asyncio.run(_listen(store, api_key))


def build_local_trade(
    public_key: str,
    action: str,
    mint: str,
    amount: float | str,
    denominated_in_sol: bool,
    slippage_pct: float,
    priority_fee_sol: float,
    pool: str = "auto",
    timeout_s: float = 15,
) -> bytes:
    """Ber PumpPortal bygge en usignert VersionedTransaction. Returnerer rå bytes."""
    if action not in ("buy", "sell"):
        raise ValueError("action må være buy eller sell")
    if pool not in POOLS:
        raise ValueError(f"ukjent pool {pool}")
    r = requests.post(
        TRADE_LOCAL_URL,
        data={
            "publicKey": public_key,
            "action": action,
            "mint": mint,
            "amount": amount,
            "denominatedInSol": "true" if denominated_in_sol else "false",
            "slippage": slippage_pct,
            "priorityFee": priority_fee_sol,
            "pool": pool,
        },
        timeout=timeout_s,
    )
    if r.status_code != 200:
        raise RuntimeError(f"PumpPortal trade-local {r.status_code}: {r.text[:200]}")
    return r.content
