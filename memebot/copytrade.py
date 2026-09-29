"""Kopitrading: følg utvalgte wallets på pump.fun og handle når de handler.

Kjøres som egen prosess:  python -m memebot copy

Flyt per hendelse fra PumpPortal (subscribeAccountTrade):
  lederen kjøper  → rask sikkerhetssjekk (mint/freeze/Token-2022) → risikolag → kjøp
  lederen selger  → selg samme andel av vår posisjon (speilet salg, alltid tillatt)
I tillegg sjekkes egne exits hvert 20. sekund: gevinstsikring, stop-loss og maks holdetid.

Alle handler fra walletene du følger loggføres, slik at dashboardet kan vise hvem som
faktisk tjener penger (ikke bare hvem som ser flinke ut på en leaderboard).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

from .portfolio import Portfolio
from .strategy import Order

log = logging.getLogger("memebot.copy")


# ----------------------------------------------------------------- parsing
@dataclass
class TradeEvent:
    wallet: str
    mint: str
    side: str          # buy | sell
    sol: float
    tokens: float
    new_balance: float | None
    signature: str | None
    pool: str | None
    ts: int

    @property
    def price_sol(self) -> float:
        return self.sol / self.tokens if self.tokens else 0.0


def parse_trade(msg: dict, now: float | None = None) -> TradeEvent | None:
    """Tolker en handel fra PumpPortal defensivt. Returnerer None for bekreftelser o.l."""
    side = (msg.get("txType") or "").lower()
    if side not in ("buy", "sell"):
        return None
    try:
        return TradeEvent(
            wallet=msg["traderPublicKey"], mint=msg["mint"], side=side,
            sol=float(msg.get("solAmount") or 0), tokens=float(msg.get("tokenAmount") or 0),
            new_balance=float(msg["newTokenBalance"]) if msg.get("newTokenBalance") is not None else None,
            signature=msg.get("signature"), pool=msg.get("pool"), ts=int(now or time.time()),
        )
    except (KeyError, TypeError, ValueError):
        return None


# ----------------------------------------------------------- wallet-scoring
def wallet_stats(trades: list[dict]) -> dict:
    """Statistikk for én wallet ut fra loggførte handler (i SOL)."""
    per_mint: dict[str, dict] = defaultdict(lambda: {"sol_in": 0.0, "sol_out": 0.0, "tok_in": 0.0,
                                                     "tok_out": 0.0, "first": None, "last_sell": None})
    for t in trades:
        m = per_mint[t["mint"]]
        if t["side"] == "buy":
            m["sol_in"] += t["sol"]
            m["tok_in"] += t["tokens"]
            m["first"] = m["first"] or t["ts"]
        else:
            m["sol_out"] += t["sol"]
            m["tok_out"] += t["tokens"]
            m["last_sell"] = t["ts"]

    closed, wins, pnl_total, pnls, holds = 0, 0, 0.0, [], []
    for m in per_mint.values():
        if m["tok_in"] <= 0 or m["tok_out"] <= 0:
            continue
        sold_frac = min(1.0, m["tok_out"] / m["tok_in"])
        pnl = m["sol_out"] - m["sol_in"] * sold_frac
        pnl_total += pnl
        pnls.append(pnl)
        if sold_frac >= 0.95:
            closed += 1
            wins += pnl > 0
            if m["first"] and m["last_sell"]:
                holds.append((m["last_sell"] - m["first"]) / 3600)
    positive = sum(p for p in pnls if p > 0)
    top_share = max(pnls) / positive if positive > 0 else 0.0
    holds.sort()
    stats = {
        "trades": len(trades), "coins": len(per_mint), "closed": closed,
        "win_rate": wins / closed * 100 if closed else None,
        "realized_sol": round(pnl_total, 3),
        "top_coin_share": round(top_share * 100, 1),
        "median_hold_h": round(holds[len(holds) // 2], 1) if holds else None,
    }
    stats["verdict"] = verdict(stats)
    return stats


def verdict(s: dict) -> str:
    if s["closed"] < 10:
        return "For lite data ennå – følg med i minst 1–2 uker"
    if s["realized_sol"] <= 0:
        return "Taper penger på det vi har sett"
    if s["top_coin_share"] > 60:
        return "Nesten all gevinst kommer fra én coin – flaks eller innsidekunnskap, vanskelig å kopiere"
    if (s["win_rate"] or 0) < 25:
        return "Tjener penger, men de fleste handlene taper – krever at du tåler mange tap"
    return "Ser jevnt lønnsom ut på det vi har sett"


# --------------------------------------------------------------- kjernen
class CopyTrader:
    def __init__(self, cfg: dict, store, executor, risk, notifier, token_check, sol_price, now_fn=time.time):
        self.cfg, self.store, self.executor, self.risk, self.notifier = cfg, store, executor, risk, notifier
        self.token_check = token_check          # mint -> liste med avvisningsgrunner
        self.sol_price = sol_price              # () -> USD per SOL
        self.now = now_fn
        self.mode = executor.mode
        self.last_price_sol: dict[str, float] = {}
        self._checked: dict[str, list[str]] = {}
        self._bought_once: set[str] = set()     # konsensus: kjøp hver coin bare én gang

    @property
    def c(self) -> dict:
        return self.cfg["copytrade"]

    def labels(self) -> dict[str, str]:
        return {w["address"]: w.get("label") or w["address"][:6] for w in self.c["wallets"]}

    def portfolio(self) -> Portfolio:
        return Portfolio(self.cfg["start_capital_usd"], self.cfg["buckets"], self.store.trades(self.mode))

    # ------------------------------------------------------------ hendelser
    def on_event(self, ev: TradeEvent) -> list[str]:
        """Behandler én handel. Returnerer en liste med hva som skjedde (for logg/test)."""
        if ev.tokens > 0:
            self.last_price_sol[ev.mint] = ev.price_sol
        leaders = self.labels()
        if ev.wallet not in leaders:
            return []          # handel fra noen andre på en coin vi eier – brukt kun til pris
        self.store.add_wallet_trade(ev.ts, ev.wallet, ev.mint, ev.side, ev.sol, ev.tokens, ev.signature)
        if not self.c["enabled"]:
            return ["logget (kopitrading er av)"]
        return self._on_leader_buy(ev, leaders) if ev.side == "buy" else self._on_leader_sell(ev, leaders)

    def _on_leader_buy(self, ev: TradeEvent, leaders: dict) -> list[str]:
        name = leaders[ev.wallet]
        if ev.sol < self.c["min_leader_sol"]:
            return [f"ignorert: {name} kjøpte bare {ev.sol:.2f} SOL"]
        pf = self.portfolio()
        pos = pf.positions.get(ev.mint)
        if pos and pos.qty > 0:
            self.store.upsert_state(ev.mint, bucket="copy", symbol=pos.symbol)
            self._link(ev.mint, ev.wallet)
            return ["har allerede posisjon – snitter ikke opp"]
        if len(pf.open_positions("copy")) >= self.c["max_open"]:
            return [f"hoppet over: allerede {self.c['max_open']} åpne kopiposisjoner"]

        reason = f"kopi: {name} kjøpte for {ev.sol:.2f} SOL"
        if self.c["mode"] == "consensus":
            if ev.mint in self._bought_once:
                return ["konsensus: allerede kjøpt denne"]
            since = ev.ts - self.c["consensus_window_minutes"] * 60
            buyers = {t["wallet"] for t in self.store.wallet_trades(since=since)
                      if t["mint"] == ev.mint and t["side"] == "buy" and t["sol"] >= self.c["min_leader_sol"]}
            if len(buyers) < self.c["consensus_min_wallets"]:
                return [f"konsensus: {len(buyers)} av {self.c['consensus_min_wallets']} ledere har kjøpt"]
            reason = f"kopi: {len(buyers)} ledere kjøpte innen {self.c['consensus_window_minutes']} min"

        if ev.mint not in self._checked:
            try:
                self._checked[ev.mint] = self.token_check(ev.mint)
            except Exception as e:
                self._checked[ev.mint] = [f"sikkerhetssjekk feilet: {e}"]
        if self._checked[ev.mint]:
            msg = f"{short(ev.mint)} avvist: {'; '.join(self._checked[ev.mint])}"
            self.store.log("reject", msg)
            return [msg]

        price_usd = ev.price_sol * self.sol_price()
        order = Order("copy", ev.mint, short(ev.mint), "buy", usd=self.c["buy_usd"], reason=reason,
                      state={"tp_hits": 0, "breakeven_armed": 0, "dead": 0})
        done = self._run([order], pf, price_usd)
        if done:
            self._bought_once.add(ev.mint)
            self._link(ev.mint, ev.wallet)
        return done

    def _on_leader_sell(self, ev: TradeEvent, leaders: dict) -> list[str]:
        if not self.c["mirror_sells"]:
            return []
        pf = self.portfolio()
        pos = pf.positions.get(ev.mint)
        if not pos or pos.qty <= 0 or pos.bucket != "copy" or ev.wallet not in self._links(ev.mint):
            return []
        if ev.new_balance is not None and ev.tokens + ev.new_balance > 0:
            frac = ev.tokens / (ev.tokens + ev.new_balance)
        else:
            frac = 1.0
        qty = pos.qty if frac >= 0.95 else pos.qty * frac
        order = Order("copy", ev.mint, pos.symbol, "sell", qty=qty, risk_exit=True,
                      reason=f"kopi: {leaders[ev.wallet]} solgte {frac * 100:.0f} %")
        return self._run([order], pf, ev.price_sol * self.sol_price())

    # ---------------------------------------------------------- egne exits
    def check_exits(self) -> list[str]:
        pf = self.portfolio()
        out = []
        sol_usd = None
        for pos in pf.open_positions("copy"):
            p_sol = self.last_price_sol.get(pos.mint)
            if not p_sol:
                continue
            sol_usd = sol_usd or self.sol_price()
            price = p_sol * sol_usd
            gain = (price / pos.avg_price - 1) * 100
            st = self.store.get_state(pos.mint) or {"tp_hits": 0, "initial_qty": pos.qty}
            first_buy = min((t["ts"] for t in self.store.trades(self.mode, pos.mint) if t["side"] == "buy"), default=self.now())
            order = None
            tps = self.c["take_profits"]
            if st["tp_hits"] < len(tps) and gain >= tps[st["tp_hits"]][0]:
                frac = tps[st["tp_hits"]][1]
                order = Order("copy", pos.mint, pos.symbol, "sell", qty=min(pos.qty, (st["initial_qty"] or pos.qty) * frac),
                              risk_exit=True, reason=f"TP{st['tp_hits'] + 1} +{gain:.0f}%",
                              state={"tp_hits": st["tp_hits"] + 1})
            elif gain <= -self.c["stop_loss_pct"]:
                order = Order("copy", pos.mint, pos.symbol, "sell", qty=pos.qty, risk_exit=True, reason=f"stop {gain:.0f}%")
            elif self.now() - first_buy >= self.c["max_hold_hours"] * 3600:
                order = Order("copy", pos.mint, pos.symbol, "sell", qty=pos.qty, risk_exit=True,
                              reason=f"maks holdetid {self.c['max_hold_hours']} t")
            if order:
                out += self._run([order], pf, price)
        return out

    # ------------------------------------------------------------ utførelse
    def _run(self, orders: list[Order], pf: Portfolio, price_usd: float) -> list[str]:
        approved, blocked = self.risk.check(orders, pf)
        for b in blocked:
            self.store.log("risk", b)
        done = []
        for o in approved:
            try:
                fill = self.executor.execute(o, price_usd)
            except Exception as e:
                self.store.log("error", f"kopi {o.side} {o.symbol} feilet: {e}")
                self.notifier.send(f"❗ Kopi-{o.side} {o.symbol} feilet: {e}")
                continue
            trade = dict(mode=self.mode, bucket="copy", mint=o.mint, symbol=o.symbol, side=o.side, qty=fill.qty,
                         price_usd=fill.price_usd, usd=fill.usd, fee_usd=fill.fee_usd, reason=o.reason,
                         tx_sig=fill.tx_sig, ts=int(self.now()))
            self.store.add_trade(**trade)
            pf.apply(trade)
            pos = pf.positions.get(o.mint)
            if o.side == "sell" and (pos is None or pos.qty <= 0):
                self.store.delete_state(o.mint)
                self.store.db.execute("DELETE FROM copy_links WHERE mint=?", (o.mint,))
                self.store.db.commit()
            else:
                state = dict(o.state)
                if o.side == "buy":
                    state["initial_qty"] = fill.qty
                if state:
                    self.store.upsert_state(o.mint, bucket="copy", symbol=o.symbol, **state)
            emoji = "🟢" if o.side == "buy" else "🔴"
            self.notifier.send(f"{emoji} [{self.mode}] KOPI {o.side.upper()} {o.symbol} ${fill.usd:,.2f}\n{o.reason}"
                               + (f"\nhttps://solscan.io/tx/{fill.tx_sig}" if fill.tx_sig else ""))
            done.append(f"{o.side} {o.symbol}: {o.reason}")
        return done

    def _link(self, mint: str, wallet: str) -> None:
        self.store.db.execute("INSERT OR IGNORE INTO copy_links (mint, wallet) VALUES (?,?)", (mint, wallet))
        self.store.db.commit()

    def _links(self, mint: str) -> set[str]:
        return {r[0] for r in self.store.db.execute("SELECT wallet FROM copy_links WHERE mint=?", (mint,))}

    def held_mints(self) -> list[str]:
        return [p.mint for p in self.portfolio().open_positions("copy")]


def short(mint: str) -> str:
    return f"{mint[:4]}…{mint[-3:]}"


class PaperCopyExecutor:
    """Papirmodus for kopitrading: vi får dårligere pris enn lederen (forsinkelse + slippage)."""

    mode = "paper"

    def __init__(self, fee_pct: float, latency_penalty_pct: float):
        self.fee = fee_pct / 100
        self.penalty = latency_penalty_pct / 100

    def execute(self, o: Order, price: float, **_):
        from .execution import Fill

        if o.side == "buy":
            p = price * (1 + self.penalty)
            fee = o.usd * self.fee
            return Fill(qty=(o.usd - fee) / p, price_usd=p, usd=o.usd - fee, fee_usd=fee)
        p = price * (1 - self.penalty)
        usd = o.qty * p
        return Fill(qty=o.qty, price_usd=p, usd=usd, fee_usd=usd * self.fee)


# ------------------------------------------------------------- websocket
async def run(trader: CopyTrader, api_key: str, reload_cfg) -> None:
    """Én websocket-forbindelse (PumpPortal-regel). Abonnerer på lederwallets og på coins vi eier."""
    import websockets

    from .pumpportal import WS_URL

    url = f"{WS_URL}?api-key={api_key}"
    backoff = 5
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, max_queue=2048) as ws:
                wallets: set[str] = set()
                tokens: set[str] = set()

                async def sync_subs():
                    nonlocal wallets, tokens
                    want_w = {w["address"] for w in trader.c["wallets"]}
                    want_t = set(trader.held_mints())
                    if want_w - wallets:
                        await ws.send(json.dumps({"method": "subscribeAccountTrade", "keys": sorted(want_w - wallets)}))
                    if wallets - want_w:
                        await ws.send(json.dumps({"method": "unsubscribeAccountTrade", "keys": sorted(wallets - want_w)}))
                    if want_t - tokens:
                        await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": sorted(want_t - tokens)}))
                    if tokens - want_t:
                        await ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": sorted(tokens - want_t)}))
                    wallets, tokens = want_w, want_t

                await sync_subs()
                log.info("Kopitrading tilkoblet – følger %d wallets", len(wallets))
                trader.notifier.send(f"👀 Kopitrading startet ({trader.mode}) – følger {len(wallets)} wallets")
                backoff = 5

                async def housekeeping():
                    n = 0
                    while True:
                        await asyncio.sleep(20)
                        await asyncio.to_thread(trader.check_exits)
                        n += 1
                        if n % 3 == 0:          # hvert minutt: les config på nytt (endringer fra dashboardet)
                            try:
                                reload_cfg(trader)
                            except Exception as e:
                                log.warning("kunne ikke lese config: %s", e)
                        await sync_subs()

                hk = asyncio.create_task(housekeeping())
                try:
                    async for raw in ws:
                        try:
                            ev = parse_trade(json.loads(raw))
                        except ValueError:
                            continue
                        if ev is None:
                            continue
                        result = await asyncio.to_thread(trader.on_event, ev)
                        for r in result:
                            log.info(r)
                        if ev.side == "buy" and result:
                            await sync_subs()      # abonner på pris for ny posisjon
                finally:
                    hk.cancel()
        except Exception as e:
            log.warning("Websocket brutt (%s). Ny tilkobling om %ss", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)
