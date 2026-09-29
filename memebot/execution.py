"""Utførelse av ordre.

PaperExecutor  – simulerer fyll med gebyr/slippage. Standard.
LiveExecutor   – ekte swaps på Solana via PumpPortal Local API eller Jupiter.
                 Transaksjonen signeres lokalt med keypair-fila di. Faktisk fyll
                 beregnes fra saldoendring før/etter, ikke fra antatt pris.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass

from .data import SOL_MINT, HttpClient, SolanaRPC
from .strategy import Order

log = logging.getLogger("memebot.exec")
LAMPORTS = 1_000_000_000


@dataclass
class Fill:
    qty: float
    price_usd: float
    usd: float
    fee_usd: float
    tx_sig: str | None = None


class PaperExecutor:
    mode = "paper"

    def __init__(self, fee_pct: float):
        self.fee = fee_pct / 100

    def execute(self, o: Order, price: float, **_) -> Fill:
        if o.side == "buy":
            fee = o.usd * self.fee
            usd = o.usd - fee
            return Fill(qty=usd / price, price_usd=price, usd=usd, fee_usd=fee)
        usd = o.qty * price
        return Fill(qty=o.qty, price_usd=price, usd=usd, fee_usd=usd * self.fee)


class LiveExecutor:
    mode = "live"

    def __init__(self, cfg: dict, rpc: SolanaRPC, http: HttpClient, sol_price_fn):
        from solders.keypair import Keypair  # krever: pip install solders

        path = os.environ.get("SOLANA_KEYPAIR_PATH")
        if not path or not os.path.exists(path):
            raise RuntimeError("SOLANA_KEYPAIR_PATH mangler eller peker ikke på en fil")
        with open(path) as f:
            self.kp = Keypair.from_bytes(bytes(json.load(f)))
        self.pubkey = str(self.kp.pubkey())
        self.cfg, self.rpc, self.http = cfg, rpc, http
        self._sol_fn = sol_price_fn
        self._sol_cache = (0.0, 0.0)          # (tid, pris) – spar et nettverkskall per handel
        self.ex = cfg["execution"]
        self.risk = cfg["risk"]
        self._decimals: dict[str, int] = {}

    # ---------------------------------------------------------------- helpers
    def sol_price_fn(self) -> float:
        t, p = self._sol_cache
        if time.time() - t > 60:
            p = self._sol_fn()
            self._sol_cache = (time.time(), p)
        return p

    def decimals(self, mint: str) -> int:
        if mint not in self._decimals:
            self._decimals[mint] = int(self.rpc.mint_info(mint)["decimals"])
        return self._decimals[mint]

    def _jupiter_quote(self, input_mint: str, output_mint: str, amount_raw: int) -> dict:
        return self.http.get(
            f"{self.ex['jupiter_base_url'].rstrip('/')}/quote",
            params={"inputMint": input_mint, "outputMint": output_mint, "amount": amount_raw,
                    "slippageBps": self.risk["slippage_bps"]},
        )

    def _check_impact(self, quote: dict) -> None:
        impact = float(quote.get("priceImpactPct") or 0) * 100
        limit = self.cfg["filters"]["max_price_impact_pct"]
        if impact > limit:
            raise RuntimeError(f"price impact {impact:.2f}% > {limit}%")

    def _sign_and_send(self, raw_tx: bytes) -> str:
        from solders.transaction import VersionedTransaction

        tx = VersionedTransaction.from_bytes(raw_tx)
        signed = VersionedTransaction(tx.message, [self.kp])
        b64 = base64.b64encode(bytes(signed)).decode()
        sig = self.rpc.call("sendTransaction", [b64, {"encoding": "base64", "maxRetries": 3,
                                                      "preflightCommitment": "confirmed"}])
        self._confirm(sig)
        return sig

    def _confirm(self, sig: str, timeout_s: int = 60) -> None:
        end = time.time() + timeout_s
        while time.time() < end:
            st = self.rpc.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])["value"][0]
            if st:
                if st.get("err"):
                    raise RuntimeError(f"transaksjon feilet on-chain: {st['err']}")
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return
            time.sleep(2)
        raise RuntimeError(f"ikke bekreftet innen {timeout_s}s: {sig}")

    # ---------------------------------------------------------------- execute
    def execute(self, o: Order, price: float, **_) -> Fill:
        sol_usd = self.sol_price_fn()
        dec = self.decimals(o.mint)
        tok_before = self.rpc.token_balance_raw(self.pubkey, o.mint)
        sol_before = self.rpc.get_balance_lamports(self.pubkey)

        if o.side == "buy":
            sol_amt = o.usd / sol_usd
            reserve = self.ex["min_sol_reserve"] * LAMPORTS
            if sol_before - sol_amt * LAMPORTS < reserve:
                raise RuntimeError("for lite SOL (reserve for gebyrer)")
            lamports = int(sol_amt * LAMPORTS)
            quote = self._quote_or_none(SOL_MINT, o.mint, lamports, o)
            if quote is not None:
                self._check_impact(quote)
            raw = self._build("buy", o.mint, sol_amt, lamports, quote, in_sol=True, bucket=o.bucket)
        else:
            qty_raw = min(int(o.qty * 10**dec), tok_before)
            if qty_raw <= 0:
                raise RuntimeError("ingen tokens å selge")
            quote = self._quote_or_none(o.mint, SOL_MINT, qty_raw, o)
            if quote is not None and not o.risk_exit:   # risikosalg skal gjennom selv med høy impact
                self._check_impact(quote)
            raw = self._build("sell", o.mint, qty_raw / 10**dec, qty_raw, quote, in_sol=False, bucket=o.bucket)

        sig = self._sign_and_send(raw)
        time.sleep(2)
        tok_after = self.rpc.token_balance_raw(self.pubkey, o.mint)
        sol_after = self.rpc.get_balance_lamports(self.pubkey)

        qty = abs(tok_after - tok_before) / 10**dec
        sol_delta = abs(sol_after - sol_before) / LAMPORTS
        usd = sol_delta * sol_usd  # inkluderer nettverks-/plattformgebyr (konservativt)
        if qty <= 0:
            raise RuntimeError(f"transaksjon {sig} bekreftet, men saldo endret seg ikke – sjekk manuelt")
        return Fill(qty=qty, price_usd=usd / qty, usd=usd, fee_usd=0.0, tx_sig=sig)

    def _quote_or_none(self, inp: str, out: str, amount: int, o: Order) -> dict | None:
        """Kopihandler bruker PumpPortal direkte (bonding-kurve), og skal ikke vente på / stoppes av Jupiter."""
        if o.bucket == "copy":
            return None
        return self._jupiter_quote(inp, out, amount)

    def _build(self, action: str, mint: str, ui_amount: float, raw_amount: int, quote: dict | None,
               in_sol: bool, bucket: str = "") -> bytes:
        if self.ex["provider"] == "pumpportal" or bucket == "copy" or quote is None:
            from .pumpportal import build_local_trade

            return build_local_trade(
                public_key=self.pubkey, action=action, mint=mint, amount=ui_amount,
                denominated_in_sol=in_sol, slippage_pct=self.risk["slippage_bps"] / 100,
                priority_fee_sol=self.risk["priority_fee_lamports"] / LAMPORTS,
                pool=self.ex["pumpportal_pool"],
            )
        res = self.http.post(
            f"{self.ex['jupiter_base_url'].rstrip('/')}/swap",
            json={"quoteResponse": quote, "userPublicKey": self.pubkey, "wrapAndUnwrapSol": True,
                  "dynamicComputeUnitLimit": True,
                  "prioritizationFeeLamports": self.risk["priority_fee_lamports"]},
        )
        return base64.b64decode(res["swapTransaction"])
