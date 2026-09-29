"""Backtest på daglige candles med nøyaktig samme strategi- og risikokode som live.

Data: CSV-filer (ts,open,high,low,close,volume) eller GeckoTerminal pool-adresser.
Hver dag i: prisen = dagens close, candles = alt t.o.m. dag i. Kjøp/salg fylles til close
med simulert gebyr/slippage (execution.paper_fee_pct).

Begrensning: shots-backtest bruker bare tokens du oppgir – den kan ikke gjenskape
hvilke tokens filteret ville funnet historisk (survivorship bias!).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .data import Candle
from .engine import Engine
from .execution import PaperExecutor
from .notify import Notifier
from .portfolio import Portfolio
from .risk import RiskManager
from .storage import Store
from .strategy import Market, entry_signal


@dataclass
class Series:
    symbol: str
    bucket: str           # core | rotation | shots
    candles: list[Candle]

    @property
    def mint(self) -> str:
        return f"BT_{self.symbol}"


def load_csv(path: str | Path) -> list[Candle]:
    out = []
    with open(path) as f:
        for row in csv.DictReader(f):
            out.append(Candle(int(float(row["ts"])), float(row["open"]), float(row["high"]),
                              float(row["low"]), float(row["close"]), float(row.get("volume") or 0)))
    return sorted(out, key=lambda c: c.ts)


def run_backtest(cfg: dict, series: list[Series], warmup: int = 30) -> dict:
    store = Store(":memory:")
    clock = {"now": 0}
    cfg = {**cfg, "core_tokens": [{"mint": s.mint, "symbol": s.symbol} for s in series if s.bucket == "core"],
           "rotation_tokens": [{"mint": s.mint, "symbol": s.symbol} for s in series if s.bucket == "rotation"]}
    executor = PaperExecutor(cfg["execution"]["paper_fee_pct"])
    executor.mode = "backtest"
    risk = RiskManager(cfg, store, "backtest", Path("/nonexistent"), now_fn=lambda: clock["now"])
    engine = Engine(cfg, store, executor, risk, Notifier(False), None, None, None, now_fn=lambda: clock["now"])

    days = sorted({c.ts for s in series for c in s.candles})
    by_ts = {s.mint: {c.ts: i for i, c in enumerate(s.candles)} for s in series}
    core = [s.mint for s in series if s.bucket == "core"]
    rot = [s.mint for s in series if s.bucket == "rotation"]
    curve, blocked = [], 0
    first_close: dict[str, float] = {}

    for d in days[warmup:]:
        clock["now"] = d + 86399  # slutten av dagen
        markets: dict[str, Market] = {}
        for s in series:
            i = by_ts[s.mint].get(d)
            if i is None or i < 7:
                continue
            m = Market(s.mint, s.symbol, s.candles[i].close, s.candles[: i + 1])
            markets[s.mint] = m
            first_close.setdefault(s.mint, m.price)

        pf = Portfolio(cfg["start_capital_usd"], cfg["buckets"], store.trades("backtest"))
        held_shots = {p.mint for p in pf.open_positions("shots")}
        candidates = [markets[s.mint] for s in series
                      if s.bucket == "shots" and s.mint in markets and s.mint not in held_shots
                      and entry_signal(markets[s.mint], cfg["shots_entry"])]
        fixed = {k: v for k, v in markets.items() if k in core or k in rot or k in held_shots}
        res = engine.step(fixed, core, rot, candidates, pf=pf)
        blocked += len(res["blocked"])
        pf = Portfolio(cfg["start_capital_usd"], cfg["buckets"], store.trades("backtest"))
        curve.append((d, pf.equity({k: v.price for k, v in markets.items()})))

    trades = store.trades("backtest")
    start = cfg["start_capital_usd"]
    end = curve[-1][1] if curve else start
    peak, mdd = start, 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100 if peak else 0)

    # Referanse: kjøp og hold likt fordelt fra første handelsdag
    last = {s.mint: s.candles[-1].close for s in series}
    hodl = start * sum(last[m] / p for m, p in first_close.items()) / max(1, len(first_close))

    fees = sum(t["fee_usd"] for t in trades)
    return {
        "start": start, "end": round(end, 2), "return_pct": round((end / start - 1) * 100, 1),
        "max_drawdown_pct": round(mdd, 1), "trades": len(trades), "fees_usd": round(fees, 2),
        "blocked_by_risk": blocked, "buy_and_hold_end": round(hodl, 2),
        "curve": curve, "trade_log": trades,
    }
