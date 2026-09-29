"""Hovedløkka: hent data → filtrer → strategi → risiko → utfør → logg → varsle."""
from __future__ import annotations

import logging
import time

from . import filters
from .data import DexScreener, GeckoTerminal, SolanaRPC, TokenSnapshot
from .notify import Notifier
from .portfolio import Portfolio
from .risk import RiskManager
from .storage import Store
from .strategy import (Market, Order, core_orders, core_rotation_orders, rotation_bucket_orders,
                       shot_entry_orders, shot_exit_orders)

log = logging.getLogger("memebot.engine")


class Engine:
    def __init__(self, cfg: dict, store: Store, executor, risk: RiskManager, notifier: Notifier,
                 dex: DexScreener | None, gecko: GeckoTerminal | None, rpc: SolanaRPC | None, now_fn=time.time):
        self.cfg, self.store, self.executor, self.risk = cfg, store, executor, risk
        self.notifier, self.dex, self.gecko, self.rpc = notifier, dex, gecko, rpc
        self.mode = executor.mode
        self.now = now_fn

    # ------------------------------------------------------------- univers
    def _portfolio(self) -> Portfolio:
        return Portfolio(self.cfg["start_capital_usd"], self.cfg["buckets"], self.store.trades(self.mode))

    def _candidate_mints(self, exclude: set[str]) -> list[str]:
        d = self.cfg["data"]
        mints: list[str] = list(d.get("extra_watchlist") or [])
        if "pumpportal_migrations" in d["scan_sources"]:
            rows = self.store.migrations_between(self.cfg["filters"]["min_age_days"], d["max_candidate_age_days"])
            mints += [r["mint"] for r in sorted(rows, key=lambda r: -r["ts"])]
        if "geckoterminal_trending" in d["scan_sources"]:
            try:
                mints += self.gecko.trending_base_mints()
            except Exception as e:
                self.store.log("warn", f"trending feilet: {e}")
        uniq = [m for m in dict.fromkeys(mints) if m not in exclude]
        return uniq[: d["max_candidates_per_run"]]

    def _market(self, snap: TokenSnapshot) -> Market | None:
        if not snap.pool_address or snap.price_usd <= 0:
            return None
        try:
            candles = self.gecko.daily_candles(snap.pool_address, limit=max(60, self.cfg["core_rules"]["range_days"] + 5))
        except Exception as e:
            self.store.log("warn", f"candles {snap.symbol}: {e}")
            return None
        return Market(snap.mint, snap.symbol, snap.price_usd, candles)

    # --------------------------------------------------------------- én runde
    def run_once(self) -> dict:
        pf = self._portfolio()
        states = {s["mint"]: s for s in self.store.all_states()}
        core_mints = [t["mint"] for t in self.cfg.get("core_tokens") or []]
        rot_mints = [t["mint"] for t in self.cfg.get("rotation_tokens") or []]
        held = [p.mint for p in pf.open_positions()]
        fixed = set(core_mints) | set(rot_mints) | set(held)

        slots = self.cfg["shots_rules"]["max_open"] - len(pf.open_positions("shots"))
        cand_mints = self._candidate_mints(fixed) if slots > 0 else []

        snaps = self.dex.snapshots(list(fixed) + cand_mints)
        markets: dict[str, Market] = {}
        for mint in fixed:
            if mint in snaps and (m := self._market(snaps[mint])):
                markets[mint] = m

        # Kandidater: billig forfilter først, dyre on-chain-sjekker kun på de som gjenstår
        f = self.cfg["filters"]
        candidates: list[Market] = []
        rejected_log: list[str] = []
        for mint in cand_mints:
            s = snaps.get(mint)
            if s is None:
                continue
            if s.liquidity_usd < f["min_liquidity_usd"] or s.volume_24h_usd < f["min_volume_24h_usd"]:
                continue
            m = self._market(s)
            if m is None:
                continue
            mig_ts = self.store.migration_ts(mint)
            age = (time.time() - mig_ts) / 86400 if mig_ts else None
            if age is not None and s.age_days is not None:
                age = max(age, s.age_days)
            res = filters.evaluate(s, m.candles, self.rpc, f, age_days_override=age)
            if res.ok:
                candidates.append(m)
            else:
                rejected_log.append(f"{s.symbol}: {'; '.join(res.reasons)}")

        return self.step(markets, core_mints, rot_mints, candidates, pf, states, rejected_log, record=True)

    def step(self, markets: dict[str, Market], core_mints: list[str], rot_mints: list[str],
             candidates: list[Market], pf: Portfolio | None = None, states: dict | None = None,
             rejected_log: list[str] | None = None, record: bool = False) -> dict:
        """Strategi → risiko → utførelse for ferdig innhentede markedsdata (brukes også av backtest)."""
        pf = pf or self._portfolio()
        states = states if states is not None else {s["mint"]: s for s in self.store.all_states()}
        orders: list[Order] = []
        orders += shot_exit_orders(markets, pf, states, self.cfg)
        history = self.store.trades(self.mode)
        last_buy = {}
        for t in history:
            if t["side"] == "buy" and t["bucket"] == "core":
                last_buy[t["mint"]] = t["ts"]
        orders += core_orders([markets[m] for m in core_mints if m in markets], pf, states, self.cfg,
                              last_buy_ts=last_buy, now=self.now())
        rot = core_rotation_orders([markets[m] for m in core_mints if m in markets], pf, self.cfg)
        last_rot = max((t["ts"] for t in history if t["reason"] and t["reason"].startswith("rotasjon")), default=0)
        if self.now() - last_rot < self.cfg["rotation_rules"]["cooldown_days"] * 86400:
            rot = []
        if not {o.mint for o in rot} & {o.mint for o in orders}:   # begge bein eller ingen
            orders += rot
        orders += rotation_bucket_orders([markets[m] for m in rot_mints if m in markets], pf, self.cfg)
        orders += shot_entry_orders(candidates, pf, states, self.cfg)
        orders = _dedupe(orders)

        approved, blocked = self.risk.check(orders, pf)
        all_markets = {**markets, **{c.mint: c for c in candidates}}
        done = self._execute(approved, all_markets, pf, states)

        for b in blocked:
            self.store.log("risk", b)
        summary = {"orders": len(orders), "executed": done, "blocked": blocked,
                   "candidates": len(candidates), "rejected": rejected_log or [],
                   "equity": pf.equity({k: v.price for k, v in all_markets.items()})}
        self.store.log("info", f"runde: {summary['executed']} utført, {len(blocked)} blokkert, "
                               f"{len(candidates)} kandidater, egenkapital ${summary['equity']:,.0f}")
        if blocked:
            self.notifier.send("⛔ Blokkert av risikolaget:\n" + "\n".join(blocked))
        if record:
            self.store.add_run(self.mode, summary["equity"], done, blocked, len(candidates), rejected_log or [])
            self.store.set_prices({k: (v.symbol, v.price) for k, v in all_markets.items()})
        return summary

    # ------------------------------------------------------------- utførelse
    def _execute(self, orders: list[Order], markets: dict[str, Market], pf: Portfolio, states: dict) -> int:
        done, rotation_proceeds = 0, None
        for o in orders:
            if o.side == "mark":
                self._save_state(o, states)
                self.notifier.send(f"☠️ {o.symbol}: {o.reason} – snittes aldri ned")
                continue
            if o.side == "buy" and o.reason.startswith("rotasjon ←"):
                if not rotation_proceeds:        # salgsbeinet feilet/ble blokkert
                    continue
                o.usd, rotation_proceeds = rotation_proceeds, None
            m = markets.get(o.mint)
            if m is None:
                continue
            try:
                fill = self.executor.execute(o, m.price)
            except Exception as e:
                self.store.log("error", f"{o.side} {o.symbol} feilet: {e}")
                self.notifier.send(f"❗ {o.side.upper()} {o.symbol} feilet: {e}")
                continue
            if o.reason.startswith("rotasjon →"):
                rotation_proceeds = fill.usd - fill.fee_usd

            trade = dict(mode=self.mode, bucket=o.bucket, mint=o.mint, symbol=o.symbol, side=o.side,
                         qty=fill.qty, price_usd=fill.price_usd, usd=fill.usd, fee_usd=fill.fee_usd,
                         reason=o.reason, tx_sig=fill.tx_sig, ts=int(self.now()))
            trade["id"] = self.store.add_trade(**trade)
            pf.apply(trade)
            done += 1

            if o.side == "buy" and o.bucket == "shots":
                o.state["initial_qty"] = fill.qty
            pos = pf.positions.get(o.mint)
            if o.side == "sell" and (pos is None or pos.qty <= 0) and o.bucket != "core":
                self.store.delete_state(o.mint)
                states.pop(o.mint, None)
            else:
                self._save_state(o, states)

            emoji = "🟢" if o.side == "buy" else "🔴"
            self.notifier.send(f"{emoji} [{self.mode}] {o.side.upper()} {o.symbol} ({o.bucket})\n"
                               f"{fill.qty:,.2f} @ ${fill.price_usd:.6g} = ${fill.usd:,.2f}\n{o.reason}"
                               + (f"\nhttps://solscan.io/tx/{fill.tx_sig}" if fill.tx_sig else ""))
        return done

    def _save_state(self, o: Order, states: dict) -> None:
        if not o.state:
            return
        self.store.upsert_state(o.mint, bucket=o.bucket, symbol=o.symbol, **o.state)
        states[o.mint] = self.store.get_state(o.mint)


def _dedupe(orders: list[Order]) -> list[Order]:
    """Maks én ordre per mint og side per runde (første vinner – exits kommer først)."""
    seen, out = set(), []
    for o in orders:
        key = (o.mint, o.side)
        if key in seen:
            continue
        seen.add(key)
        out.append(o)
    return out
