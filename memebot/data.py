"""Datakilder: DexScreener (markedsdata), GeckoTerminal (OHLCV, trending), Solana RPC (on-chain).

Alle kall går via HttpClient, som har timeout, retry og enkel rate-limiting.
Endepunktene kan endre seg – base-URL-ene ligger i config.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import requests

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class HttpClient:
    def __init__(self, min_interval_s: float = 0.35, timeout_s: float = 15, retries: int = 3):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "memebot/0.1"
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.retries = retries
        self._last = 0.0

    def _wait(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.min_interval_s:
            time.sleep(self.min_interval_s - delta)
        self._last = time.monotonic()

    def request(self, method: str, url: str, **kw) -> Any:
        err: Exception | None = None
        for attempt in range(self.retries):
            self._wait()
            try:
                r = self.session.request(method, url, timeout=self.timeout_s, **kw)
                if r.status_code == 429:
                    time.sleep(2 ** attempt * 2)
                    continue
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as e:
                err = e
                time.sleep(2 ** attempt)
        raise RuntimeError(f"{method} {url} feilet: {err}")

    def get(self, url: str, **kw) -> Any:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> Any:
        return self.request("POST", url, **kw)


@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def red(self) -> bool:
        return self.close < self.open


@dataclass
class TokenSnapshot:
    mint: str
    symbol: str
    price_usd: float
    liquidity_usd: float
    volume_24h_usd: float
    pair_created_ms: int | None
    pool_address: str | None
    dex: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def age_days(self) -> float | None:
        if not self.pair_created_ms:
            return None
        return (time.time() * 1000 - self.pair_created_ms) / 86_400_000


class DexScreener:
    def __init__(self, http: HttpClient, base_url: str):
        self.http, self.base = http, base_url.rstrip("/")

    def snapshots(self, mints: list[str]) -> dict[str, TokenSnapshot]:
        """Beste (mest likvide) par per mint. Maks 30 mints per kall."""
        out: dict[str, TokenSnapshot] = {}
        for i in range(0, len(mints), 30):
            chunk = mints[i : i + 30]
            pairs = self.http.get(f"{self.base}/tokens/v1/solana/{','.join(chunk)}") or []
            for p in pairs:
                snap = parse_dexscreener_pair(p)
                if snap is None:
                    continue
                cur = out.get(snap.mint)
                if cur is None or snap.liquidity_usd > cur.liquidity_usd:
                    out[snap.mint] = snap
        return out

    def sol_price_usd(self) -> float:
        pairs = self.http.get(f"{self.base}/tokens/v1/solana/{SOL_MINT}") or []
        best = max(
            (p for p in pairs if p.get("baseToken", {}).get("address") == SOL_MINT and p.get("priceUsd")),
            key=lambda p: (p.get("liquidity") or {}).get("usd") or 0,
            default=None,
        )
        if best is None:
            raise RuntimeError("fant ikke SOL-pris")
        return float(best["priceUsd"])


def parse_dexscreener_pair(p: dict) -> TokenSnapshot | None:
    try:
        base = p["baseToken"]
        if p.get("chainId") != "solana" or base["address"] in (SOL_MINT, USDC_MINT):
            return None
        # Eldste par gir riktigst alder: DexScreener gir alder per par, vi bruker paret vi får.
        return TokenSnapshot(
            mint=base["address"],
            symbol=base.get("symbol", "?"),
            price_usd=float(p.get("priceUsd") or 0),
            liquidity_usd=float((p.get("liquidity") or {}).get("usd") or 0),
            volume_24h_usd=float((p.get("volume") or {}).get("h24") or 0),
            pair_created_ms=p.get("pairCreatedAt"),
            pool_address=p.get("pairAddress"),
            dex=p.get("dexId"),
            extra={"fdv": p.get("fdv"), "priceChange": p.get("priceChange")},
        )
    except (KeyError, TypeError, ValueError):
        return None


class GeckoTerminal:
    def __init__(self, http: HttpClient, base_url: str):
        self.http, self.base = http, base_url.rstrip("/")
        # GeckoTerminal sin gratis-API har lav rate limit (~30/min)
        self.http.min_interval_s = max(self.http.min_interval_s, 2.1)

    def daily_candles(self, pool_address: str, limit: int = 120) -> list[Candle]:
        url = f"{self.base}/networks/solana/pools/{pool_address}/ohlcv/day"
        data = self.http.get(url, params={"limit": limit, "currency": "usd"})
        rows = data["data"]["attributes"]["ohlcv_list"]
        return parse_ohlcv(rows)

    def trending_base_mints(self, pages: int = 2) -> list[str]:
        mints: list[str] = []
        for page in range(1, pages + 1):
            data = self.http.get(f"{self.base}/networks/solana/trending_pools", params={"page": page})
            for pool in data.get("data", []):
                rel = pool.get("relationships", {}).get("base_token", {}).get("data", {})
                token_id = rel.get("id", "")  # format "solana_<mint>"
                if token_id.startswith("solana_"):
                    mints.append(token_id.removeprefix("solana_"))
        return list(dict.fromkeys(m for m in mints if m not in (SOL_MINT, USDC_MINT)))


def parse_ohlcv(rows: list[list]) -> list[Candle]:
    candles = [Candle(int(r[0]), *map(float, r[1:6])) for r in rows]
    return sorted(candles, key=lambda c: c.ts)


class SolanaRPC:
    def __init__(self, http: HttpClient, url: str):
        self.http, self.url = http, url

    def call(self, method: str, params: list) -> Any:
        res = self.http.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in res:
            raise RuntimeError(f"RPC {method}: {res['error']}")
        return res["result"]

    def mint_info(self, mint: str) -> dict:
        res = self.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        value = res["value"]
        info = value["data"]["parsed"]["info"]
        return {
            "mint_authority": info.get("mintAuthority"),
            "freeze_authority": info.get("freezeAuthority"),
            "decimals": info.get("decimals"),
            "supply": int(info.get("supply", 0)),
            "program": value.get("owner"),
            "extensions": info.get("extensions") or [],
        }

    def largest_holders(self, mint: str) -> list[dict]:
        """Topp 20 tokenkontoer: [{address, amount(raw int)}]."""
        res = self.call("getTokenLargestAccounts", [mint])
        return [{"address": a["address"], "amount": int(a["amount"])} for a in res["value"]]

    def token_account_owner(self, token_account: str) -> str | None:
        res = self.call("getAccountInfo", [token_account, {"encoding": "jsonParsed"}])
        try:
            return res["value"]["data"]["parsed"]["info"]["owner"]
        except (TypeError, KeyError):
            return None

    def get_balance_lamports(self, pubkey: str) -> int:
        return int(self.call("getBalance", [pubkey])["value"])

    def token_balance_raw(self, owner: str, mint: str) -> int:
        res = self.call("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        total = 0
        for acc in res["value"]:
            total += int(acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
        return total
