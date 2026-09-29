import math
import time
from pathlib import Path

import pytest

from memebot.config import load_config
from memebot.data import Candle, TokenSnapshot, parse_dexscreener_pair, parse_ohlcv
from memebot.execution import PaperExecutor
from memebot.filters import evaluate, max_drawdown_pct, top10_holder_pct
from memebot.portfolio import Portfolio
from memebot.pumpportal import parse_migration
from memebot.risk import RiskManager
from memebot.storage import Store
from memebot.strategy import Market, Order, core_orders, entry_signal, shot_exit_orders
from memebot.tax import fifo_realizations, rate_for

DAY = 86400


@pytest.fixture
def cfg():
    return load_config(Path("/nonexistent.yaml"))


def candles_from(closes, start=1_700_000_000, vol=1000.0):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(Candle(start + i * DAY, o, max(o, c) * 1.02, min(o, c) * 0.98, c, vol))
        prev = c
    return out


def trade(side, qty, usd, bucket="core", mint="A", fee=0.0, ts=None, i=[0]):
    i[0] += 1
    return dict(id=i[0], ts=ts or int(time.time()), mode="paper", bucket=bucket, mint=mint, symbol=mint,
                side=side, qty=qty, price_usd=usd / qty, usd=usd, fee_usd=fee)


# ---------------------------------------------------------------- portefølje
def test_portfolio_capital_grows_only_from_realized(cfg):
    pf = Portfolio(1000, cfg["buckets"], [trade("buy", 100, 100)])
    assert pf.bucket_capital("core") == pytest.approx(600)
    assert pf.bucket_cash("core") == pytest.approx(500)
    pf.apply(trade("sell", 50, 150))            # halvparten solgt til 3x
    assert pf.realized["core"] == pytest.approx(100)
    assert pf.bucket_capital("core") == pytest.approx(700)
    assert pf.positions["A"].qty == pytest.approx(50)
    assert pf.positions["A"].avg_price == pytest.approx(1.0)


# ------------------------------------------------------------------ strategi
def test_core_buys_only_at_range_low_on_red_day(cfg):
    pf = Portfolio(1000, cfg["buckets"], [])
    closes = [2.0] * 20 + [1.0] * 10          # range 0.98..2.04
    c = candles_from(closes)
    red = Market("A", "A", 1.0, c[:-1] + [Candle(c[-1].ts, 1.1, 1.12, 0.98, 1.0, 1)])
    green = Market("A", "A", 1.0, c[:-1] + [Candle(c[-1].ts, 0.9, 1.02, 0.88, 1.0, 1)])
    top = Market("A", "A", 2.0, candles_from([1.0] * 20 + [2.0] * 10))

    o = core_orders([red], pf, {}, cfg)
    assert len(o) == 1 and o[0].side == "buy"
    assert o[0].usd == pytest.approx(600 / 1 / 3)
    assert core_orders([green], pf, {}, cfg) == []
    assert core_orders([top], pf, {}, cfg) == []


def test_core_take_profit_ladder(cfg):
    pf = Portfolio(1000, cfg["buckets"], [trade("buy", 100, 100)])
    m = Market("A", "A", 2.1, candles_from([1.0] * 29 + [2.1]))
    o = core_orders([m], pf, {"A": {"tranches": 1, "tp_hits": 0, "breakeven_armed": 0, "dead": 0, "initial_qty": 0}}, cfg)
    assert o[0].side == "sell" and o[0].qty == pytest.approx(25)
    assert o[0].state["tp_hits"] == 1
    # Etter første TP kreves +200 % for neste
    o2 = core_orders([m], pf, {"A": {"tranches": 1, "tp_hits": 1, "breakeven_armed": 0, "dead": 0, "initial_qty": 0}}, cfg)
    assert not any(x.side == "sell" for x in o2)


def test_shots_tp_breakeven_and_dead(cfg):
    pf = Portfolio(1000, cfg["buckets"], [trade("buy", 100, 100, bucket="shots", mint="S")])
    st = {"S": {"tranches": 0, "tp_hits": 0, "breakeven_armed": 0, "dead": 0, "initial_qty": 100}}
    up = {"S": Market("S", "S", 2.05, candles_from([1] * 10 + [2.05]))}
    o = shot_exit_orders(up, pf, st, cfg)
    assert o[0].qty == pytest.approx(25) and o[0].state["breakeven_armed"] == 1

    st["S"].update(tp_hits=1, breakeven_armed=1)
    back = {"S": Market("S", "S", 0.99, candles_from([1] * 11))}
    o = shot_exit_orders(back, pf, st, cfg)
    assert o[0].risk_exit and o[0].qty == pytest.approx(100)

    st["S"].update(tp_hits=0, breakeven_armed=0)
    dead = {"S": Market("S", "S", 0.25, candles_from([1] * 10 + [0.25]))}
    o = shot_exit_orders(dead, pf, st, cfg)
    assert o[0].side == "mark" and o[0].state["dead"] == 1


def test_entry_signal(cfg):
    e = cfg["shots_entry"]
    base = candles_from([10] + [3.0] * 8)                      # ATH 10.2, nå rundt 3
    today = Candle(base[-1].ts + DAY, 3.0, 3.4, 2.9, 3.3, 2000)  # grønn, 2x volum
    assert entry_signal(Market("X", "X", 3.3, base + [today]), e)
    weak = Candle(today.ts, 3.0, 3.4, 2.9, 3.3, 1000)
    assert not entry_signal(Market("X", "X", 3.3, base + [weak]), e)


# -------------------------------------------------------------------- risiko
def test_risk_limits_and_kill_switch(cfg, tmp_path):
    store = Store(":memory:")
    cfg["risk"]["max_trades_per_month"] = 2
    rm = RiskManager(cfg, store, "paper", tmp_path)
    pf = Portfolio(1000, cfg["buckets"], [])
    orders = [Order("core", "A", "A", "buy", usd=900), Order("core", "B", "B", "buy", usd=50),
              Order("core", "C", "C", "buy", usd=50)]
    ok, blocked = rm.check(orders, pf)
    assert ok[0].usd == pytest.approx(400)       # max_position_usd
    assert len(ok) == 2 and len(blocked) == 1     # månedsgrense

    for _ in range(2):
        store.add_trade(**{k: v for k, v in trade("buy", 1, 1).items() if k != "id"})
    ok, blocked = rm.check([Order("core", "A", "A", "sell", qty=1, risk_exit=True)], pf)
    assert len(ok) == 1                           # risikosalg går alltid gjennom

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "KILL").touch()
    ok, blocked = rm.check([Order("core", "A", "A", "buy", usd=10)], pf)
    assert ok == [] and "KILL" in blocked[0]


def test_daily_loss_blocks_buys(cfg, tmp_path):
    pf = Portfolio(1000, cfg["buckets"], [trade("buy", 100, 300), trade("sell", 100, 100)])
    rm = RiskManager(cfg, Store(":memory:"), "paper", tmp_path)
    ok, blocked = rm.check([Order("core", "B", "B", "buy", usd=50)], pf)
    assert ok == [] and "tapsgrense" in blocked[0]


# ------------------------------------------------------------------- filter
def test_drawdown_and_holders():
    c = candles_from([1, 2, 4, 1, 1.5])
    assert max_drawdown_pct(c) > 70
    holders = [{"address": "pool", "amount": 500}, {"address": "w1", "amount": 100}, {"address": "w2", "amount": 50}]
    assert top10_holder_pct(holders, 1000, {"pool"}) == pytest.approx(15)


class FakeRPC:
    def __init__(self, mint_auth=None, freeze=None, holders=None):
        self.mint_auth, self.freeze = mint_auth, freeze
        self.holders = holders or [{"address": f"h{i}", "amount": 10} for i in range(20)]

    def mint_info(self, mint):
        return {"mint_authority": self.mint_auth, "freeze_authority": self.freeze, "decimals": 6, "supply": 10_000}

    def largest_holders(self, mint):
        return self.holders

    def token_account_owner(self, acc):
        return "POOL" if acc == "h0" else "someone"


def test_filter_evaluate(cfg):
    snap = TokenSnapshot("M", "M", 1.0, 500_000, 400_000, int((time.time() - 30 * DAY) * 1000), "POOL")
    c = candles_from([1, 5, 1, 1.2])
    assert evaluate(snap, c, FakeRPC(), cfg["filters"]).ok
    r = evaluate(snap, c, FakeRPC(mint_auth="X", freeze="Y"), cfg["filters"])
    assert not r.ok and len(r.reasons) == 2
    young = TokenSnapshot("M", "M", 1.0, 500_000, 400_000, int((time.time() - 3 * DAY) * 1000), "POOL")
    assert any("ung" in x for x in evaluate(young, c, FakeRPC(), cfg["filters"]).reasons)


# -------------------------------------------------------------- parsing/misc
def test_parsers():
    snap = parse_dexscreener_pair({"chainId": "solana", "baseToken": {"address": "M", "symbol": "MEME"},
                                   "priceUsd": "0.01", "liquidity": {"usd": 1e5}, "volume": {"h24": 5e4},
                                   "pairCreatedAt": 1, "pairAddress": "P", "dexId": "pumpswap"})
    assert snap.symbol == "MEME" and snap.pool_address == "P"
    c = parse_ohlcv([[2, 1, 2, 0.5, 1.5, 10], [1, 1, 1, 1, 1, 1]])
    assert c[0].ts == 1
    assert parse_migration({"message": "Successfully subscribed"}) is None
    assert parse_migration({"signature": "x", "mint": "M", "txType": "migrate", "pool": "pump-amm"})["mint"] == "M"


def test_paper_executor():
    ex = PaperExecutor(1.0)
    f = ex.execute(Order("core", "A", "A", "buy", usd=100), 2.0)
    assert f.fee_usd == pytest.approx(1) and f.qty == pytest.approx(49.5)


def test_fifo():
    rows = fifo_realizations([trade("buy", 10, 10, ts=1), trade("buy", 10, 30, ts=2), trade("sell", 15, 60, ts=3)])
    assert rows[0]["kostpris_usd"] == pytest.approx(10 + 15)
    assert rows[0]["gevinst_usd"] == pytest.approx(35)
    assert rate_for("2026-01-04", {"2026-01-02": 11.0, "2026-01-05": 11.2}) == 11.0
