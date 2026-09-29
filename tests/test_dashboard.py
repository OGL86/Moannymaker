"""Dashboard: tilstand, innstillinger og kill switch via HTTP (uten nettverk ut)."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import yaml

from memebot import dashboard
from memebot.dashboard import build_state, make_handler, save_settings
from memebot.config import load_config
from memebot.storage import Store

MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def test_save_settings_validates(tmp_path):
    p = tmp_path / "config.yaml"
    errs = save_settings(p, {"start_capital_usd": "abc", "loop_minutes": 15,
                             "risk": {"max_position_usd": 100, "max_daily_loss_usd": 50, "max_trades_per_month": 30},
                             "core_tokens": [{"mint": "ikke-en-adresse", "symbol": "X"}]})
    assert "start_capital_usd" in errs and "core_tokens" in errs and not p.exists()

    ok = save_settings(p, {"start_capital_usd": 500, "loop_minutes": 15,
                           "risk": {"max_position_usd": 100, "max_daily_loss_usd": 50, "max_trades_per_month": 30},
                           "core_tokens": [{"mint": MINT, "symbol": "TEST"}]})
    assert ok == {}
    cfg = load_config(p)
    assert cfg["start_capital_usd"] == 500 and cfg["core_tokens"][0]["symbol"] == "TEST"
    assert cfg["mode"] == "paper"      # kan ikke endres fra dashboardet


def test_state_and_http(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "ROOT", tmp_path)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"storage": {"db_path": str(tmp_path / "db.sqlite")}}))
    cfg = load_config(cfg_path)
    store = Store(cfg["storage"]["db_path"])
    store.add_trade(mode="paper", bucket="core", mint=MINT, symbol="TEST", side="buy", qty=100, price_usd=1,
                    usd=100, fee_usd=1, reason="tranche 1/3 i range 20%")
    store.set_prices({MINT: ("TEST", 1.5)})
    store.add_run("paper", 1050, 1, [], 3, ["RUG: for ung"])

    s = build_state(cfg, store, cfg_path)
    assert s["status"] == "running" and s["positions"][0]["pnl_pct"] > 40
    assert {b["key"] for b in s["buckets"]} == {"core", "rotation", "shots"}

    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg_path, None, lambda m: {"symbol": "X"}))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    assert b"memebot" in urllib.request.urlopen(base + "/").read()
    assert json.load(urllib.request.urlopen(base + "/api/state"))["positions"][0]["symbol"] == "TEST"

    # uten X-Memebot-header avvises POST (CSRF-vern)
    req = urllib.request.Request(base + "/api/kill", data=b'{"on":true}', method="POST")
    try:
        urllib.request.urlopen(req)
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 403
    req = urllib.request.Request(base + "/api/kill", data=b'{"on":true}', method="POST",
                                 headers={"X-Memebot": "1", "Content-Type": "application/json"})
    assert json.load(urllib.request.urlopen(req))["killed"] is True
    assert (tmp_path / "data" / "KILL").exists()
    srv.shutdown()


def test_save_buckets_and_copytrade(tmp_path):
    p = tmp_path / "config.yaml"
    base = {"start_capital_usd": 1000, "loop_minutes": 15,
            "risk": {"max_position_usd": 100, "max_daily_loss_usd": 50, "max_trades_per_month": 30},
            "core_tokens": [], "rotation_tokens": []}
    bad = save_settings(p, {**base, "buckets": {"core": 60, "rotation": 25, "shots": 15, "copy": 10},
                            "copy_enabled": True, "copy_mode": "mirror", "copy_buy_usd": 20, "copy_wallets": []})
    assert "buckets" in bad and "copy_wallets" in bad and not p.exists()
    ok = save_settings(p, {**base, "buckets": {"core": 50, "rotation": 25, "shots": 15, "copy": 10},
                           "copy_enabled": True, "copy_mode": "consensus", "copy_buy_usd": 20,
                           "copy_wallets": [{"address": MINT, "label": "Ola"}]})
    assert ok == {}
    cfg = load_config(p)
    assert cfg["buckets"]["copy"] == 0.1 and cfg["copytrade"]["mode"] == "consensus"
    assert cfg["copytrade"]["wallets"][0]["label"] == "Ola"
    assert cfg["copytrade"]["max_trades_per_day"] == 40      # øvrige standardverdier beholdes
