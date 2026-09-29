"""Portefølje avledet fra handelsloggen.

Strategien bruker gjennomsnittskost per posisjon. Skatterapporten (tax.py) bruker FIFO separat.
Bøttekapital = startkapital * vekt + realisert resultat i bøtta. Den vokser altså bare
fra realisert gevinst – aldri fra urealisert.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class Position:
    mint: str
    symbol: str
    bucket: str
    qty: float = 0.0
    cost_usd: float = 0.0      # gjenværende kostbasis (inkl. kjøpsgebyr)

    @property
    def avg_price(self) -> float:
        return self.cost_usd / self.qty if self.qty > 0 else 0.0


class Portfolio:
    def __init__(self, start_capital: float, weights: dict[str, float], trades: list[dict]):
        self.start_capital = start_capital
        self.weights = weights
        self.positions: dict[str, Position] = {}
        self.realized: dict[str, float] = {b: 0.0 for b in weights}
        self.realized_by_day: dict[str, float] = {}
        for t in trades:
            self.apply(t)

    def apply(self, t: dict) -> None:
        pos = self.positions.get(t["mint"])
        if pos is None:
            pos = self.positions[t["mint"]] = Position(t["mint"], t.get("symbol") or "?", t["bucket"])
        fee = t.get("fee_usd", 0.0)
        if t["side"] == "buy":
            pos.qty += t["qty"]
            pos.cost_usd += t["usd"] + fee
        else:
            qty = min(t["qty"], pos.qty)
            if qty <= 0:
                return
            cost_part = pos.avg_price * qty
            pnl = t["usd"] - fee - cost_part
            pos.qty -= qty
            pos.cost_usd -= cost_part
            if pos.qty <= 1e-12:
                pos.qty, pos.cost_usd = 0.0, 0.0
            self.realized[pos.bucket] = self.realized.get(pos.bucket, 0.0) + pnl
            day = time.strftime("%Y-%m-%d", time.gmtime(t.get("ts", time.time())))
            self.realized_by_day[day] = self.realized_by_day.get(day, 0.0) + pnl

    # --- kapital --------------------------------------------------------
    def bucket_capital(self, bucket: str) -> float:
        return max(0.0, self.start_capital * self.weights[bucket] + self.realized.get(bucket, 0.0))

    def bucket_invested(self, bucket: str) -> float:
        return sum(p.cost_usd for p in self.positions.values() if p.bucket == bucket and p.qty > 0)

    def bucket_cash(self, bucket: str) -> float:
        return max(0.0, self.bucket_capital(bucket) - self.bucket_invested(bucket))

    def open_positions(self, bucket: str | None = None) -> list[Position]:
        return [p for p in self.positions.values() if p.qty > 0 and (bucket is None or p.bucket == bucket)]

    def realized_today(self, now: float | None = None) -> float:
        return self.realized_by_day.get(time.strftime("%Y-%m-%d", time.gmtime(now)), 0.0)

    def equity(self, prices: dict[str, float]) -> float:
        total_capital = sum(self.bucket_capital(b) for b in self.weights)
        unreal = sum(p.qty * prices.get(p.mint, p.avg_price) - p.cost_usd for p in self.open_positions())
        return total_capital + unreal
