"""Sikkerhetsfilter for shots-kandidater.

Returnerer (ok, grunner) slik at hver avvisning logges med forklaring.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .data import Candle, SolanaRPC, TokenSnapshot

# Kjente program-/pool-autoriteter som ofte eier LP-tokenkontoer. Utvides ved behov.
KNOWN_POOL_OWNERS = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM v4 authority
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",  # Raydium CPMM authority
}
# I tillegg ekskluderes kontoer eid av selve pool-adressen (PumpSwap-vaults o.l.), se evaluate().


@dataclass
class FilterResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


# Token-2022-utvidelser som kan gjøre at du ikke får solgt, eller mister tokens
DANGEROUS_EXTENSIONS = {
    "permanentDelegate": "permanent delegate (utsteder kan flytte/brenne dine tokens)",
    "transferHook": "transfer hook (egen kode kan blokkere salg)",
    "nonTransferable": "kan ikke overføres",
    "pausable": "utsteder kan pause all handel",
}


def token_safety(info: dict) -> list[str]:
    """Raske sjekker på mint-kontoen. Returnerer grunner til avvisning (tom liste = OK)."""
    reasons = []
    if info.get("mint_authority"):
        reasons.append("mint authority ikke fjernet (kan trykke nye tokens)")
    if info.get("freeze_authority"):
        reasons.append("freeze authority aktiv (kan fryse walleten din)")
    for ext in info.get("extensions") or []:
        name = ext.get("extension")
        if name in DANGEROUS_EXTENSIONS:
            reasons.append(DANGEROUS_EXTENSIONS[name])
        elif name == "transferFeeConfig":
            st = ext.get("state") or {}
            fee = max(int((st.get(k) or {}).get("transferFeeBasisPoints", 0))
                      for k in ("newerTransferFee", "olderTransferFee"))
            if fee > 0:
                reasons.append(f"skatt på overføring ({fee / 100:.2f} %)")
        elif name == "defaultAccountState" and (ext.get("state") or {}).get("accountState") == "frozen":
            reasons.append("nye kontoer fryses som standard")
    return reasons


def max_drawdown_pct(candles: list[Candle]) -> float:
    """Største fall fra en tidligere topp (high → senere low), i prosent."""
    peak, worst = 0.0, 0.0
    for c in candles:
        peak = max(peak, c.high)
        if peak > 0:
            worst = max(worst, (peak - c.low) / peak * 100)
    return worst


def top10_holder_pct(holders: list[dict], supply_raw: int, excluded_accounts: set[str]) -> float:
    if supply_raw <= 0:
        return 100.0
    top = [h for h in holders if h["address"] not in excluded_accounts][:10]
    return sum(h["amount"] for h in top) / supply_raw * 100


def evaluate(
    snap: TokenSnapshot,
    candles: list[Candle],
    rpc: SolanaRPC | None,
    f: dict,
    age_days_override: float | None = None,
) -> FilterResult:
    r = FilterResult(ok=True)

    age = age_days_override if age_days_override is not None else snap.age_days
    r.metrics["age_days"] = age
    if age is None or age < f["min_age_days"]:
        r.reasons.append(f"for ung ({age and round(age, 1)} d < {f['min_age_days']})")

    r.metrics["liquidity_usd"] = snap.liquidity_usd
    if snap.liquidity_usd < f["min_liquidity_usd"]:
        r.reasons.append(f"lav likviditet (${snap.liquidity_usd:,.0f})")

    r.metrics["volume_24h_usd"] = snap.volume_24h_usd
    if snap.volume_24h_usd < f["min_volume_24h_usd"]:
        r.reasons.append(f"lavt volum (${snap.volume_24h_usd:,.0f})")

    dd = max_drawdown_pct(candles) if candles else 0.0
    r.metrics["max_drawdown_pct"] = round(dd, 1)
    if dd < f["require_prior_drawdown_pct"]:
        r.reasons.append(f"har ikke overlevd -{f['require_prior_drawdown_pct']}% ennå (maks fall {dd:.0f}%)")

    if rpc is not None:
        try:
            info = rpc.mint_info(snap.mint)
            for reason in token_safety(info):
                if "mint authority" in reason and not f["require_mint_authority_revoked"]:
                    continue
                if "freeze authority" in reason and not f["require_freeze_authority_revoked"]:
                    continue
                r.reasons.append(reason)

            holders = rpc.largest_holders(snap.mint)
            excluded = _pool_accounts(rpc, holders[:5], extra_owners={snap.pool_address} if snap.pool_address else set())
            pct = top10_holder_pct(holders, info["supply"], excluded)
            r.metrics["top10_pct"] = round(pct, 1)
            if pct > f["max_top10_holder_pct"]:
                r.reasons.append(f"konsentrert eierskap (topp 10 = {pct:.0f}%)")
        except Exception as e:  # on-chain-sjekk feilet -> avvis heller enn å gjette
            r.reasons.append(f"on-chain-sjekk feilet: {e}")

    r.ok = not r.reasons
    return r


def _pool_accounts(rpc: SolanaRPC, top_holders: list[dict], extra_owners: set[str] = frozenset()) -> set[str]:
    """Finn tokenkontoer som eies av kjente pool-autoriteter, så de ikke telles som 'hvaler'."""
    owners = KNOWN_POOL_OWNERS | set(extra_owners)
    excluded = set()
    for h in top_holders:
        owner = rpc.token_account_owner(h["address"])
        if owner in owners:
            excluded.add(h["address"])
    return excluded
