"""Strategiregler (rene funksjoner – ingen nettverk, ingen lagring).

Tar inn markedsdata + porteføljestatus og returnerer ordreforslag. Risikolaget
(risk.py) avgjør deretter hva som faktisk får gå gjennom.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .data import Candle
from .portfolio import Portfolio


@dataclass
class Market:
    mint: str
    symbol: str
    price: float
    candles: list[Candle]            # daglige, eldste først. Siste kan være dagen i dag (uferdig).

    def range(self, days: int) -> tuple[float, float]:
        window = self.candles[-days:]
        return min(c.low for c in window), max(c.high for c in window)

    def range_pos_pct(self, days: int) -> float:
        lo, hi = self.range(days)
        if hi <= lo:
            return 50.0
        return max(0.0, min(100.0, (self.price - lo) / (hi - lo) * 100))

    def red_today(self) -> bool:
        return bool(self.candles) and self.price < self.candles[-1].open

    def ath(self) -> float:
        return max((c.high for c in self.candles), default=self.price)


@dataclass
class Order:
    bucket: str
    mint: str
    symbol: str
    side: str                 # buy | sell
    usd: float = 0.0          # for kjøp
    qty: float = 0.0          # for salg
    reason: str = ""
    risk_exit: bool = False   # salg som beskytter kapital slippes alltid gjennom
    state: dict = field(default_factory=dict)   # statusendring som lagres etter utført ordre


def _state(states: dict, mint: str) -> dict:
    return states.get(mint) or {"tranches": 0, "tp_hits": 0, "breakeven_armed": 0, "dead": 0, "initial_qty": 0.0}


# ------------------------------------------------------------------ core
def core_orders(markets: list[Market], pf: Portfolio, states: dict, cfg: dict,
                last_buy_ts: dict[str, int] | None = None, now: float | None = None) -> list[Order]:
    r = cfg["core_rules"]
    last_buy_ts = last_buy_ts or {}
    now = now if now is not None else time.time()
    orders: list[Order] = []
    n = max(1, len(markets))
    per_tranche = pf.bucket_capital("core") / n / r["tranches"]

    for m in markets:
        if len(m.candles) < min(7, r["range_days"]):
            continue
        st = _state(states, m.mint)
        pos = pf.positions.get(m.mint)
        held = pos.qty if pos and pos.bucket == "core" else 0.0
        range_pos = m.range_pos_pct(r["range_days"])

        if held > 0:
            gain = (m.price / pos.avg_price - 1) * 100
            next_tp = r["take_profit_pct"] * (st["tp_hits"] + 1)
            if gain >= next_tp:
                orders.append(Order("core", m.mint, m.symbol, "sell", qty=held * r["take_profit_fraction"],
                                    reason=f"TP +{gain:.0f}% (terskel +{next_tp:.0f}%)",
                                    state={"tp_hits": st["tp_hits"] + 1, "tranches": max(0, st["tranches"] - 1)}))
                continue
            if r.get("stop_loss_pct") and gain <= -r["stop_loss_pct"]:
                orders.append(Order("core", m.mint, m.symbol, "sell", qty=held, risk_exit=True,
                                    reason=f"stop {gain:.0f}%", state={"tranches": 0, "tp_hits": 0}))
                continue

        if st["tranches"] < r["tranches"] and range_pos <= r["buy_zone_pct"]:
            if r["red_day_only"] and not m.red_today():
                continue
            if now - last_buy_ts.get(m.mint, 0) < r["min_days_between_tranches"] * 86400:
                continue
            orders.append(Order("core", m.mint, m.symbol, "buy", usd=per_tranche,
                                reason=f"tranche {st['tranches'] + 1}/{r['tranches']} i range {range_pos:.0f}%",
                                state={"tranches": st["tranches"] + 1}))
    return orders


def core_rotation_orders(markets: list[Market], pf: Portfolio, cfg: dict) -> list[Order]:
    """Flytt en andel fra core-coin på range-topp til core-coin på range-bunn."""
    rr, days = cfg["rotation_rules"], cfg["core_rules"]["range_days"]
    if not rr["enabled"]:
        return []
    held = [m for m in markets if (p := pf.positions.get(m.mint)) and p.qty > 0 and p.bucket == "core"]
    highs = sorted((m for m in held if m.range_pos_pct(days) >= rr["high_zone_pct"]),
                   key=lambda m: -m.range_pos_pct(days))
    lows = sorted((m for m in markets if m.range_pos_pct(days) <= rr["low_zone_pct"]),
                  key=lambda m: m.range_pos_pct(days))
    for a in highs:
        for b in lows:
            if a.mint == b.mint:
                continue
            qty = pf.positions[a.mint].qty * rr["move_fraction"]
            usd = qty * a.price
            return [
                Order("core", a.mint, a.symbol, "sell", qty=qty, reason=f"rotasjon → {b.symbol} (range-topp)"),
                Order("core", b.mint, b.symbol, "buy", usd=usd, reason=f"rotasjon ← {a.symbol} (range-bunn)"),
            ]
    return []


# -------------------------------------------------------------- rotation
def rotation_bucket_orders(markets: list[Market], pf: Portfolio, cfg: dict) -> list[Order]:
    """Rotasjonsbøtta (f.eks. launchpad-tokens): kjøp halv størrelse på range-bunn, selg på range-topp."""
    rr, days = cfg["rotation_rules"], cfg["core_rules"]["range_days"]
    if not markets:
        return []
    size = pf.bucket_capital("rotation") / len(markets) / 2
    orders = []
    for m in markets:
        if len(m.candles) < 7:
            continue
        pos = pf.positions.get(m.mint)
        rp = m.range_pos_pct(days)
        if pos and pos.qty > 0 and pos.bucket == "rotation":
            if rp >= rr["high_zone_pct"]:
                orders.append(Order("rotation", m.mint, m.symbol, "sell", qty=pos.qty, reason=f"range-topp {rp:.0f}%"))
        elif rp <= rr["low_zone_pct"] and m.red_today():
            orders.append(Order("rotation", m.mint, m.symbol, "buy", usd=size, reason=f"range-bunn {rp:.0f}%"))
    return orders


# ----------------------------------------------------------------- shots
def shot_exit_orders(markets: dict[str, Market], pf: Portfolio, states: dict, cfg: dict) -> list[Order]:
    s = cfg["shots_rules"]
    orders = []
    for pos in pf.open_positions("shots"):
        m = markets.get(pos.mint)
        if m is None:
            continue
        st = _state(states, pos.mint)
        gain = (m.price / pos.avg_price - 1) * 100
        initial = st["initial_qty"] or pos.qty

        if st["tp_hits"] < len(s["take_profits"]):
            tp_pct, frac = s["take_profits"][st["tp_hits"]]
            if gain >= tp_pct:
                orders.append(Order("shots", m.mint, m.symbol, "sell", qty=min(pos.qty, initial * frac),
                                    reason=f"TP{st['tp_hits'] + 1} +{gain:.0f}%",
                                    state={"tp_hits": st["tp_hits"] + 1,
                                           "breakeven_armed": int(s["breakeven_stop_after_first_tp"])}))
                continue

        if st["breakeven_armed"] and m.price <= pos.avg_price:
            orders.append(Order("shots", m.mint, m.symbol, "sell", qty=pos.qty, risk_exit=True,
                                reason="break-even-stop etter TP"))
            continue
        if s.get("stop_loss_pct") and gain <= -s["stop_loss_pct"]:
            orders.append(Order("shots", m.mint, m.symbol, "sell", qty=pos.qty, risk_exit=True,
                                reason=f"stop {gain:.0f}%"))
            continue
        if gain <= -s["dead_at_drawdown_pct"] and not st["dead"]:
            # Ingen handel – bare markér som død så den aldri snittes ned
            orders.append(Order("shots", m.mint, m.symbol, "mark", reason=f"død ({gain:.0f}%)", state={"dead": 1}))
    return orders


def shot_entry_orders(candidates: list[Market], pf: Portfolio, states: dict, cfg: dict) -> list[Order]:
    s, e = cfg["shots_rules"], cfg["shots_entry"]
    open_now = len(pf.open_positions("shots"))
    slots = s["max_open"] - open_now
    if slots <= 0:
        return []
    size = s["size_usd"] or pf.bucket_capital("shots") / s["max_open"]
    orders = []
    for m in candidates:
        if slots <= 0:
            break
        if m.mint in pf.positions and pf.positions[m.mint].qty > 0:
            continue
        if _state(states, m.mint)["dead"]:
            continue
        if not entry_signal(m, e):
            continue
        orders.append(Order("shots", m.mint, m.symbol, "buy", usd=size, reason="shot-inngang",
                            state={"tp_hits": 0, "breakeven_armed": 0, "dead": 0}))
        slots -= 1
    return orders


def entry_signal(m: Market, e: dict) -> bool:
    """Volum stiger mot 7-dagers snitt, dagens candle er grønn, og prisen er fortsatt langt under ATH."""
    if len(m.candles) < 8:
        return False
    prev = m.candles[-8:-1]
    avg_vol = sum(c.volume for c in prev) / len(prev)
    today = m.candles[-1]
    if avg_vol <= 0 or today.volume < avg_vol * e["min_volume_ratio"]:
        return False
    if e["require_green_day"] and m.price <= today.open:
        return False
    if m.price > m.ath() * (1 - e["min_below_ath_pct"] / 100):
        return False
    return True
