"""Risikolaget. Ren kode som alltid har siste ord – strategien (eller en AI) kan ikke overstyre det.

Salg som beskytter kapital (risk_exit) slippes alltid gjennom. Alle andre ordre må
passere samtlige sjekker.
"""
from __future__ import annotations

import calendar
import time
from pathlib import Path

from .portfolio import Portfolio
from .storage import Store
from .strategy import Order


class RiskManager:
    def __init__(self, cfg: dict, store: Store, mode: str, root: Path, now_fn=time.time):
        self.r = cfg["risk"]
        self.store = store
        self.mode = mode
        self.kill_file = root / self.r["kill_switch_file"]
        self.now = now_fn
        self.copy_cap = (cfg.get("copytrade") or {}).get("max_trades_per_day", 40)

    def killed(self) -> bool:
        return self.kill_file.exists()

    def trades_this_month(self) -> int:
        """Kopihandler teller ikke her – de har egen dagsgrense."""
        t = time.gmtime(self.now())
        start = calendar.timegm((t.tm_year, t.tm_mon, 1, 0, 0, 0))
        return self.store.trades_since(start, self.mode, exclude_bucket="copy")

    def copy_trades_today(self) -> int:
        t = time.gmtime(self.now())
        return self.store.trades_since(calendar.timegm((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0)), self.mode, bucket="copy")

    def check(self, orders: list[Order], pf: Portfolio) -> tuple[list[Order], list[str]]:
        approved: list[Order] = []
        rejected: list[str] = []
        if self.killed():
            keep = [o for o in orders if o.side == "mark"]
            return keep, [f"KILL SWITCH aktiv ({self.kill_file.name}) – {len(orders) - len(keep)} ordre stoppet"]

        used = self.trades_this_month()
        copy_used = self.copy_trades_today()
        copy_cap = self.copy_cap
        daily_loss_hit = pf.realized_today(self.now()) <= -self.r["max_daily_loss_usd"]
        planned_cash = {b: pf.bucket_cash(b) for b in pf.weights}

        for o in orders:
            if o.side == "mark":
                approved.append(o)
                continue
            if o.bucket == "copy":
                # Salg slippes alltid gjennom (vi skal alltid kunne komme oss ut)
                if o.side == "buy":
                    if daily_loss_hit:
                        rejected.append(f"{o.symbol} kopikjøp: dagens tapsgrense nådd")
                        continue
                    if copy_used >= copy_cap:
                        rejected.append(f"{o.symbol} kopikjøp: dagsgrense {copy_cap} nådd")
                        continue
                    usd = min(o.usd, self.r["max_position_usd"], planned_cash.get("copy", 0.0))
                    if usd < 5:
                        rejected.append(f"{o.symbol} kopikjøp: for lite ledig kapital i kopipotten")
                        continue
                    o.usd = usd
                    planned_cash["copy"] = planned_cash.get("copy", 0.0) - usd
                copy_used += 1
                approved.append(o)
                continue
            if o.side == "sell":
                if o.risk_exit or used < self.r["max_trades_per_month"]:
                    approved.append(o)
                    used += 1
                else:
                    rejected.append(f"{o.symbol} salg: månedsgrense {self.r['max_trades_per_month']} nådd")
                continue

            # --- kjøp ---
            if daily_loss_hit:
                rejected.append(f"{o.symbol} kjøp: dagens tapsgrense nådd")
                continue
            if used >= self.r["max_trades_per_month"]:
                rejected.append(f"{o.symbol} kjøp: månedsgrense {self.r['max_trades_per_month']} nådd")
                continue
            is_rotation_leg = o.reason.startswith("rotasjon")
            usd = min(o.usd, self.r["max_position_usd"])
            if not is_rotation_leg:
                usd = min(usd, planned_cash.get(o.bucket, 0.0))
            if usd < 5:
                rejected.append(f"{o.symbol} kjøp: for lite ledig kapital i {o.bucket}")
                continue
            if usd < o.usd - 1:
                o.reason += f" (nedskalert ${o.usd:.0f}→${usd:.0f})"
            o.usd = usd
            if not is_rotation_leg:
                planned_cash[o.bucket] -= usd
            approved.append(o)
            used += 1
        return approved, rejected
