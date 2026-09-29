"""Kopitrading: speilet kjøp/salg, konsensus, sikkerhetssjekk, exits, grenser og wallet-scoring."""
from pathlib import Path

import pytest

from memebot.config import load_config
from memebot.copytrade import CopyTrader, PaperCopyExecutor, parse_trade, wallet_stats
from memebot.filters import token_safety
from memebot.notify import Notifier
from memebot.risk import RiskManager
from memebot.storage import Store

L1 = "Lead1111111111111111111111111111111111111111"
L2 = "Lead2222222222222222222222222222222222222222"
MINT = "Mint1111111111111111111111111111111111111111"


class Clock:
    t = 1_800_000_000

    def __call__(self):
        return self.t


def make(tmp_path, mode="mirror", check=lambda m: []):
    cfg = load_config(Path("/nonexistent.yaml"))
    cfg["buckets"] = {"core": 0.5, "rotation": 0.2, "shots": 0.1, "copy": 0.2}
    cfg["copytrade"].update(enabled=True, mode=mode, wallets=[{"address": L1, "label": "Ola"}, {"address": L2, "label": "Kari"}])
    store, clock = Store(":memory:"), Clock()
    ct = CopyTrader(cfg, store, PaperCopyExecutor(1.0, 3.0), RiskManager(cfg, store, "paper", tmp_path, now_fn=clock),
                    Notifier(False), token_check=check, sol_price=lambda: 100.0, now_fn=clock)
    return ct, store, clock


def ev(wallet, side, sol, tokens, new_balance=None, mint=MINT, ts=1_800_000_000):
    return parse_trade({"traderPublicKey": wallet, "mint": mint, "txType": side, "solAmount": sol,
                        "tokenAmount": tokens, "newTokenBalance": new_balance, "signature": f"{wallet}{side}{sol}{ts}"}, now=ts)


def test_parse_trade_ignores_non_trades():
    assert parse_trade({"message": "Successfully subscribed"}) is None
    assert parse_trade({"txType": "create", "mint": "x"}) is None
    e = ev(L1, "buy", 1.0, 1_000_000)
    assert e.price_sol == pytest.approx(1e-6)


def test_mirror_buy_then_mirror_partial_and_full_sell(tmp_path):
    ct, store, _ = make(tmp_path)
    out = ct.on_event(ev(L1, "buy", 1.0, 1_000_000, 1_000_000))
    assert out and out[0].startswith("buy")
    buy = store.trades("paper")[0]
    assert buy["bucket"] == "copy" and buy["usd"] + buy["fee_usd"] == pytest.approx(25)
    assert buy["price_usd"] == pytest.approx(1e-6 * 100 * 1.03)      # dårligere pris enn lederen

    ct.on_event(ev(L1, "buy", 2.0, 1_000_000))                          # snitter ikke opp
    assert len(store.trades("paper")) == 1

    ct.on_event(ev(L1, "sell", 1.0, 500_000, new_balance=1_500_000))   # lederen selger 25 %
    t = store.trades("paper")
    assert t[-1]["side"] == "sell" and t[-1]["qty"] == pytest.approx(buy["qty"] * 0.25)

    ct.on_event(ev(L1, "sell", 3.0, 1_500_000, new_balance=0))         # lederen selger resten
    pf = ct.portfolio()
    assert pf.positions[MINT].qty == pytest.approx(0)
    assert store.get_state(MINT) is None


def test_other_leader_selling_does_not_trigger_our_sell(tmp_path):
    ct, store, _ = make(tmp_path)
    ct.on_event(ev(L1, "buy", 1.0, 1_000_000))
    ct.on_event(ev(L2, "sell", 1.0, 1_000_000, new_balance=0))
    assert [t["side"] for t in store.trades("paper")] == ["buy"]


def test_dust_and_safety_rejects(tmp_path):
    ct, store, _ = make(tmp_path, check=lambda m: ["permanent delegate"])
    assert "ignorert" in ct.on_event(ev(L1, "buy", 0.1, 1000))[0]
    assert "avvist" in ct.on_event(ev(L1, "buy", 1.0, 1000))[0]
    assert store.trades("paper") == []


def test_consensus_needs_two_leaders(tmp_path):
    ct, store, clock = make(tmp_path, mode="consensus")
    assert "1 av 2" in ct.on_event(ev(L1, "buy", 1.0, 1_000_000))[0]
    clock.t += 600
    out = ct.on_event(ev(L2, "buy", 1.0, 900_000, ts=clock.t))
    assert out[0].startswith("buy") and "2 ledere" in out[0]


def test_exits_tp_stop_and_max_hold(tmp_path):
    ct, store, clock = make(tmp_path)
    ct.on_event(ev(L1, "buy", 1.0, 1_000_000))
    qty = ct.portfolio().positions[MINT].qty
    ct.last_price_sol[MINT] = 3e-6                                        # ~ +190 %
    ct.check_exits()
    assert store.trades("paper")[-1]["qty"] == pytest.approx(qty * 0.5)
    ct.last_price_sol[MINT] = 0.4e-6                                      # -60 %
    ct.check_exits()
    assert ct.portfolio().positions[MINT].qty == pytest.approx(0)

    ct.on_event(ev(L2, "buy", 1.0, 1_000_000, mint="Mint2222222222222222222222222222222222222222"))
    clock.t += 49 * 3600
    ct.last_price_sol["Mint2222222222222222222222222222222222222222"] = 1e-6
    out = ct.check_exits()
    assert "maks holdetid" in out[0]


def test_copy_has_own_daily_cap_and_ignores_monthly(tmp_path):
    ct, store, _ = make(tmp_path)
    ct.cfg["risk"]["max_trades_per_month"] = 0
    ct.risk.copy_cap = 1
    ct.on_event(ev(L1, "buy", 1.0, 1_000_000))
    assert len(store.trades("paper")) == 1                               # månedsgrense gjelder ikke
    ct.on_event(ev(L1, "buy", 1.0, 1_000_000, mint="Mint3333333333333333333333333333333333333333"))
    assert len(store.trades("paper")) == 1                               # dagsgrense nådd
    ct.on_event(ev(L1, "sell", 1.0, 1_000_000, new_balance=0))
    assert store.trades("paper")[-1]["side"] == "sell"                   # salg går alltid gjennom


def test_disabled_only_logs(tmp_path):
    ct, store, _ = make(tmp_path)
    ct.cfg["copytrade"]["enabled"] = False
    ct.on_event(ev(L1, "buy", 1.0, 1_000_000))
    assert store.trades("paper") == [] and len(store.wallet_trades(L1)) == 1


def test_wallet_stats_and_verdicts():
    trades = []
    for i in range(12):
        m = f"m{i}"
        trades += [{"mint": m, "side": "buy", "sol": 1.0, "tokens": 100, "ts": i * 1000},
                   {"mint": m, "side": "sell", "sol": 1.5 if i % 2 else 0.8, "tokens": 100, "ts": i * 1000 + 3600}]
    s = wallet_stats(trades)
    assert s["closed"] == 12 and s["win_rate"] == pytest.approx(50)
    assert s["realized_sol"] == pytest.approx(6 * 0.5 - 6 * 0.2)
    assert s["verdict"].startswith("Ser jevnt")
    one_hit = [{"mint": "x", "side": "buy", "sol": 1, "tokens": 1, "ts": 0}, {"mint": "x", "side": "sell", "sol": 50, "tokens": 1, "ts": 1}]
    assert "For lite data" in wallet_stats(one_hit)["verdict"]


def test_token2022_extensions():
    ok = {"mint_authority": None, "freeze_authority": None, "extensions": [{"extension": "metadataPointer"}]}
    assert token_safety(ok) == []
    bad = {"mint_authority": None, "freeze_authority": None, "extensions": [
        {"extension": "permanentDelegate", "state": {}},
        {"extension": "transferFeeConfig", "state": {"newerTransferFee": {"transferFeeBasisPoints": 500},
                                                     "olderTransferFee": {"transferFeeBasisPoints": 0}}}]}
    r = token_safety(bad)
    assert len(r) == 2 and "5.00 %" in r[1]
