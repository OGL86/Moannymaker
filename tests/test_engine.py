"""Engine.run_once med falske datakilder – tester hele kjeden uten nettverk."""
import time
from pathlib import Path

from memebot.config import load_config
from memebot.data import Candle, TokenSnapshot
from memebot.engine import Engine
from memebot.execution import PaperExecutor
from memebot.notify import Notifier
from memebot.risk import RiskManager
from memebot.storage import Store

DAY = 86400


def series(closes, last_open=None, last_vol=None):
    out, prev, t0 = [], closes[0], int(time.time()) - len(closes) * DAY
    for i, c in enumerate(closes):
        o = prev
        out.append(Candle(t0 + i * DAY, o, max(o, c) * 1.01, min(o, c) * 0.99, c, 1000))
        prev = c
    if last_open is not None:
        c = out[-1]
        out[-1] = Candle(c.ts, last_open, max(last_open, c.close) * 1.01, min(last_open, c.close) * 0.99, c.close, last_vol or c.volume)
    return out


class FakeDex:
    def __init__(self, snaps):
        self.snaps = snaps

    def snapshots(self, mints):
        return {m: self.snaps[m] for m in mints if m in self.snaps}


class FakeGecko:
    def __init__(self, candles, trending):
        self.candles, self.trending = candles, trending

    def daily_candles(self, pool, limit=60):
        return self.candles[pool]

    def trending_base_mints(self):
        return self.trending


class FakeRPC:
    def mint_info(self, mint):
        return {"mint_authority": None, "freeze_authority": None, "decimals": 6, "supply": 10_000}

    def largest_holders(self, mint):
        return [{"address": f"h{i}", "amount": 10} for i in range(20)]

    def token_account_owner(self, acc):
        return "x"


def test_run_once_buys_core_and_shot(tmp_path):
    cfg = load_config(Path("/nonexistent.yaml"))
    cfg["core_tokens"] = [{"mint": "CORE", "symbol": "CORE"}]
    now_ms = int(time.time() * 1000)
    snaps = {
        "CORE": TokenSnapshot("CORE", "CORE", 1.0, 5e6, 1e6, now_ms - 200 * DAY * 1000, "P_CORE"),
        "SHOT": TokenSnapshot("SHOT", "SHOT", 3.3, 5e5, 5e5, now_ms - 40 * DAY * 1000, "P_SHOT"),
        "RUG": TokenSnapshot("RUG", "RUG", 1.0, 5e5, 5e5, now_ms - 2 * DAY * 1000, "P_RUG"),
    }
    candles = {
        "P_CORE": series([2.0] * 25 + [1.0] * 5, last_open=1.1),                   # range-bunn, rød dag
        "P_SHOT": series([10] + [3.0] * 10 + [3.3], last_open=3.0, last_vol=2500),  # overlevd -70 %, grønn, volum opp
        "P_RUG": series([1, 5, 1, 1] * 3),
    }
    store = Store(":memory:")
    cfg["data"]["scan_sources"] = ["geckoterminal_trending"]
    ex = PaperExecutor(1.0)
    eng = Engine(cfg, store, ex, RiskManager(cfg, store, "paper", tmp_path), Notifier(False),
                 FakeDex(snaps), FakeGecko(candles, ["SHOT", "RUG"]), FakeRPC())
    res = eng.run_once()
    sides = {(t["mint"], t["side"]) for t in store.trades("paper")}
    assert ("CORE", "buy") in sides
    assert ("SHOT", "buy") in sides
    assert ("RUG", "buy") not in sides
    assert any("RUG" in r and "ung" in r for r in res["rejected"])
    assert store.get_state("SHOT")["initial_qty"] > 0
    assert store.get_state("CORE")["tranches"] == 1

    # Andre runde samme dag: ingen dobbeltkjøp av shot, og ingen ny core-tranche før det har gått 5 dager
    eng.run_once()
    shot_buys = [t for t in store.trades("paper") if t["mint"] == "SHOT" and t["side"] == "buy"]
    assert len(shot_buys) == 1
    assert store.get_state("CORE")["tranches"] == 1
