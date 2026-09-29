"""Ende-til-ende: backtest på syntetiske data, og signering i LiveExecutor uten nettverk."""
import json
import math
import random

import pytest

from memebot.backtest import Series, run_backtest
from memebot.config import load_config
from memebot.data import Candle

DAY = 86400


def synthetic(seed, n=200, start=1.0, vol=0.08, trend=0.0):
    rnd = random.Random(seed)
    out, p, ts = [], start, 1_700_000_000
    for i in range(n):
        o = p
        p = max(1e-6, p * math.exp(rnd.gauss(trend, vol)))
        hi, lo = max(o, p) * (1 + abs(rnd.gauss(0, vol / 2))), min(o, p) * (1 - abs(rnd.gauss(0, vol / 2)))
        out.append(Candle(ts + i * DAY, o, hi, lo, p, 1000 * (1 + rnd.random())))
    return out


def test_backtest_runs_and_respects_limits():
    cfg = load_config("/nonexistent.yaml")
    series = [Series("AAA", "core", synthetic(1)), Series("BBB", "core", synthetic(2)),
              Series("CCC", "core", synthetic(3)), Series("LP", "rotation", synthetic(4)),
              Series("SHOT", "shots", synthetic(5, vol=0.2))]
    res = run_backtest(cfg, series)
    assert res["trades"] > 0
    assert res["end"] > 0
    # aldri mer enn maks posisjon per kjøp
    assert all(t["usd"] + t["fee_usd"] <= cfg["risk"]["max_position_usd"] + 1e-6
               for t in res["trade_log"] if t["side"] == "buy" and not t["reason"].startswith("rotasjon"))
    # månedsgrense
    from collections import Counter
    import time
    per_month = Counter(time.strftime("%Y-%m", time.gmtime(t["ts"])) for t in res["trade_log"])
    assert max(per_month.values()) <= cfg["risk"]["max_trades_per_month"] + 10  # risikosalg kan gå over


def test_live_executor_signs_locally(tmp_path, monkeypatch):
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.signature import Signature
    from solders.system_program import TransferParams, transfer
    from solders.transaction import VersionedTransaction

    from memebot.execution import LiveExecutor

    kp = Keypair()
    key_file = tmp_path / "bot.json"
    key_file.write_text(json.dumps(list(bytes(kp))))
    monkeypatch.setenv("SOLANA_KEYPAIR_PATH", str(key_file))

    msg = MessageV0.try_compile(kp.pubkey(), [transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=Keypair().pubkey(), lamports=1))], [], Hash.default())
    unsigned = bytes(VersionedTransaction.populate(msg, [Signature.default()]))

    sent = {}

    class RPC:
        def call(self, method, params):
            if method == "sendTransaction":
                sent["tx"] = params[0]
                return "SIG"
            if method == "getSignatureStatuses":
                return {"value": [{"confirmationStatus": "confirmed", "err": None}]}

    ex = LiveExecutor(load_config("/nonexistent.yaml"), RPC(), None, lambda: 150.0)
    assert ex.pubkey == str(kp.pubkey())
    assert ex._sign_and_send(unsigned) == "SIG"

    import base64
    tx = VersionedTransaction.from_bytes(base64.b64decode(sent["tx"]))
    assert tx.verify_with_results() == [True]
