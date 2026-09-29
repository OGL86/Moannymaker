"""Kommandolinje: python -m memebot <kommando>

  once        én runde (skann → strategi → risiko → utfør)
  run         kjør i løkke hvert loop_minutes
  listen      lytt på PumpPortal-migrasjoner (egen prosess)
  copy        kopitrading: følg wallets og handle når de handler (egen prosess)
  check MINT  kjør sikkerhetsfilteret på én token og vis resultatet
  status      posisjoner, bøttekapital og siste hendelser
  backtest    backtest på CSV-filer eller GeckoTerminal-pools
  tax         FIFO-rapport (CSV) over realiserte gevinster/tap
  wallet-new  lag en ny, egen bot-wallet (nøkkelfil med rettigheter 600)
  wallet      vis SOL-saldo for bot-walleten
  dashboard   åpne web-dashboardet på http://127.0.0.1:8080

Live-handel krever BÅDE mode: live i config.yaml OG flagget --live.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from .config import ROOT, load_config, load_env
from .data import DexScreener, GeckoTerminal, HttpClient, SolanaRPC
from .storage import Store


def _setup(args):
    load_env()
    cfg = load_config(args.config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = Store(ROOT / cfg["storage"]["db_path"])
    http = HttpClient()
    dex = DexScreener(http, cfg["data"]["dexscreener_base_url"])
    gecko = GeckoTerminal(HttpClient(), cfg["data"]["geckoterminal_base_url"])
    rpc_url = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    rpc = SolanaRPC(HttpClient(min_interval_s=0.15), rpc_url)
    return cfg, store, http, dex, gecko, rpc


def _engine(args, cfg, store, http, dex, gecko, rpc):
    from .engine import Engine
    from .execution import LiveExecutor, PaperExecutor
    from .notify import Notifier
    from .risk import RiskManager

    live = cfg["mode"] == "live" and args.live
    if cfg["mode"] == "live" and not args.live:
        print("config sier live, men --live mangler → kjører i papirmodus")
    if args.live and cfg["mode"] != "live":
        sys.exit("--live gitt, men config.yaml har mode: paper. Endre begge bevisst.")
    executor = LiveExecutor(cfg, rpc, http, dex.sol_price_usd) if live else PaperExecutor(cfg["execution"]["paper_fee_pct"])
    if live:
        print(f"LIVE-MODUS – wallet {executor.pubkey}")
    risk = RiskManager(cfg, store, executor.mode, ROOT)
    return Engine(cfg, store, executor, risk, Notifier(cfg["notify"]["telegram"]), dex, gecko, rpc)


def cmd_once(args):
    env = _setup(args)
    res = _engine(args, *env).run_once()
    print(json.dumps({k: v for k, v in res.items() if k != "rejected"}, indent=2, default=str))
    if args.verbose and res["rejected"]:
        print("\nAvviste kandidater:\n  " + "\n  ".join(res["rejected"]))


def cmd_run(args):
    env = _setup(args)
    cfg, store = env[0], env[1]
    engine = _engine(args, *env)
    engine.notifier.send(f"🤖 memebot startet ({engine.mode}), runde hvert {cfg['loop_minutes']}. min")
    while True:
        try:
            # Les config på nytt hver runde, så endringer fra dashboardet tas i bruk uten omstart.
            # Modus (papir/live) kan IKKE endres uten omstart.
            fresh = load_config(args.config)
            fresh["mode"] = cfg["mode"]
            engine.cfg = fresh
            engine.risk.r = fresh["risk"]
            engine.run_once()
        except Exception as e:
            store.log("error", f"runde feilet: {e}")
            engine.notifier.send(f"❗ Runde feilet: {e}")
        time.sleep(engine.cfg["loop_minutes"] * 60)


def cmd_listen(args):
    from .pumpportal import run_listener

    cfg, store, *_ = _setup(args)
    run_listener(store, os.environ.get("PUMPPORTAL_API_KEY"))


def cmd_check(args):
    from . import filters

    cfg, store, http, dex, gecko, rpc = _setup(args)
    snap = dex.snapshots([args.mint]).get(args.mint)
    if not snap:
        sys.exit("fant ikke token på DexScreener")
    candles = gecko.daily_candles(snap.pool_address) if snap.pool_address else []
    mig = store.migration_ts(args.mint)
    age = max((time.time() - mig) / 86400, snap.age_days or 0) if mig else None
    res = filters.evaluate(snap, candles, rpc, cfg["filters"], age_days_override=age)
    print(f"{snap.symbol} ({snap.mint}) pris ${snap.price_usd:.6g} dex={snap.dex}")
    print(json.dumps(res.metrics, indent=2))
    print("GODKJENT" if res.ok else "AVVIST:\n  - " + "\n  - ".join(res.reasons))


def cmd_status(args):
    from .portfolio import Portfolio

    cfg, store, *_ = _setup(args)
    mode = "live" if args.live else "paper"
    pf = Portfolio(cfg["start_capital_usd"], cfg["buckets"], store.trades(mode))
    print(f"Modus: {mode}")
    for b in cfg["buckets"]:
        print(f"  {b:9s} kapital ${pf.bucket_capital(b):>9,.2f}  investert ${pf.bucket_invested(b):>9,.2f}"
              f"  realisert ${pf.realized.get(b, 0):>+9,.2f}")
    print("Åpne posisjoner:")
    for p in pf.open_positions():
        print(f"  {p.symbol:10s} {p.bucket:8s} {p.qty:,.2f} @ ${p.avg_price:.6g}  (kost ${p.cost_usd:,.2f})")
    print("Siste hendelser:")
    for r in store.db.execute("SELECT ts, level, msg FROM events ORDER BY id DESC LIMIT 10"):
        print(f"  {time.strftime('%m-%d %H:%M', time.localtime(r[0]))} [{r[1]}] {r[2]}")


def cmd_backtest(args):
    from .backtest import Series, load_csv, run_backtest

    load_env()
    cfg = load_config(args.config)
    series = []
    for spec in args.series:
        # format: bucket:SYMBOL=kilde   (kilde = sti til .csv eller pool-adresse)
        bucket, rest = spec.split(":", 1)
        sym, src = rest.split("=", 1)
        if src.endswith(".csv"):
            candles = load_csv(src)
        else:
            gecko = GeckoTerminal(HttpClient(), cfg["data"]["geckoterminal_base_url"])
            candles = gecko.daily_candles(src, limit=1000)
        series.append(Series(sym, bucket, candles))
    res = run_backtest(cfg, series, warmup=args.warmup)
    for k in ("start", "end", "return_pct", "max_drawdown_pct", "trades", "fees_usd", "blocked_by_risk", "buy_and_hold_end"):
        print(f"{k:18s} {res[k]}")
    if args.out:
        from .tax import write_csv

        write_csv(res["trade_log"], args.out)
        print(f"handelslogg → {args.out}")


def cmd_tax(args):
    from .tax import add_nok, fetch_usdnok, fifo_realizations, write_csv

    cfg, store, *_ = _setup(args)
    rows = fifo_realizations(store.trades("live" if args.live else "paper"))
    rows = [r for r in rows if r["dato"].startswith(str(args.year))]
    if rows and not args.no_nok:
        try:
            rows = add_nok(rows, fetch_usdnok(f"{args.year - 1}-12-20", f"{args.year}-12-31"))
        except Exception as e:
            print(f"kunne ikke hente USD/NOK fra Norges Bank: {e}")
    write_csv(rows, args.out)
    total = sum(r["gevinst_usd"] for r in rows)
    print(f"{len(rows)} realisasjoner i {args.year}, netto ${total:,.2f} → {args.out}")


def cmd_wallet_new(args):
    from solders.keypair import Keypair

    path = args.path
    if os.path.exists(path):
        sys.exit(f"{path} finnes allerede – overskriver ikke")
    kp = Keypair()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(list(bytes(kp)), f)
    print(f"Ny bot-wallet: {kp.pubkey()}\nNøkkelfil: {path} (kun lesbar for deg)\n"
          f"Sett SOLANA_KEYPAIR_PATH={os.path.abspath(path)} i .env. Ta sikker backup av fila.")


def cmd_wallet(args):
    from solders.keypair import Keypair

    cfg, store, http, dex, gecko, rpc = _setup(args)
    with open(os.environ["SOLANA_KEYPAIR_PATH"]) as f:
        pub = str(Keypair.from_bytes(bytes(json.load(f))).pubkey())
    sol = rpc.get_balance_lamports(pub) / 1e9
    print(f"{pub}: {sol:.4f} SOL (≈ ${sol * dex.sol_price_usd():,.2f})")


def cmd_dashboard(args):
    from pathlib import Path

    from .dashboard import serve

    cfg, store, http, dex, gecko, rpc = _setup(args)

    def lookup(mint):
        s = dex.snapshots([mint]).get(mint)
        if not s:
            raise RuntimeError("ingen handelspar funnet")
        return {"symbol": s.symbol, "price_usd": s.price_usd, "volume_24h_usd": s.volume_24h_usd,
                "liquidity_usd": s.liquidity_usd}

    config_path = Path(args.config) if args.config else ROOT / "config.yaml"
    serve(config_path, args.host, args.port, lookup)


def cmd_copy(args):
    import asyncio

    from . import copytrade
    from .execution import LiveExecutor
    from .filters import token_safety
    from .notify import Notifier
    from .risk import RiskManager

    cfg, store, http, dex, gecko, rpc = _setup(args)
    api_key = os.environ.get("PUMPPORTAL_API_KEY")
    if not api_key:
        sys.exit("Kopitrading krever PUMPPORTAL_API_KEY i .env (se pumpportal.fun). "
                 "Nøkkelens wallet må ha minst 0,02 SOL.")
    if not cfg["copytrade"]["wallets"]:
        sys.exit("Ingen wallets å følge. Legg dem til i dashboardet eller under copytrade.wallets i config.yaml.")
    live = cfg["mode"] == "live" and args.live
    if args.live and cfg["mode"] != "live":
        sys.exit("--live gitt, men config.yaml har mode: paper.")
    if live:
        executor = LiveExecutor(cfg, rpc, http, dex.sol_price_usd)
        print(f"LIVE-MODUS – wallet {executor.pubkey}")
    else:
        executor = copytrade.PaperCopyExecutor(cfg["execution"]["paper_fee_pct"],
                                               cfg["copytrade"]["paper_latency_penalty_pct"])
    if cfg["buckets"].get("copy", 0) <= 0:
        print("Merk: buckets.copy er 0 – boten logger lederne, men har ingen penger å kopiere med. "
              "Gi kopipotten en andel i config.yaml.")

    sol_cache = {"t": 0.0, "p": 0.0}

    def sol_price():
        if time.time() - sol_cache["t"] > 60:
            sol_cache.update(t=time.time(), p=dex.sol_price_usd())
        return sol_cache["p"]

    trader = copytrade.CopyTrader(cfg, store, executor, RiskManager(cfg, store, executor.mode, ROOT),
                                  Notifier(cfg["notify"]["telegram"]),
                                  token_check=lambda mint: token_safety(rpc.mint_info(mint)),
                                  sol_price=sol_price)

    def reload_cfg(t):
        fresh = load_config(args.config)
        fresh["mode"] = cfg["mode"]
        t.cfg = fresh
        t.risk.r = fresh["risk"]
        t.risk.copy_cap = fresh["copytrade"]["max_trades_per_day"]

    asyncio.run(copytrade.run(trader, api_key, reload_cfg))


def main(argv=None):
    p = argparse.ArgumentParser(prog="memebot")
    p.add_argument("--config", default=None)
    p.add_argument("--live", action="store_true", help="tillat ekte handel (krever også mode: live)")
    sub = p.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("once"); o.add_argument("-v", "--verbose", action="store_true"); o.set_defaults(fn=cmd_once)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    sub.add_parser("listen").set_defaults(fn=cmd_listen)
    sub.add_parser("copy").set_defaults(fn=cmd_copy)
    c = sub.add_parser("check"); c.add_argument("mint"); c.set_defaults(fn=cmd_check)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    b = sub.add_parser("backtest")
    b.add_argument("series", nargs="+", help="bucket:SYMBOL=fil.csv eller bucket:SYMBOL=<pool-adresse>")
    b.add_argument("--warmup", type=int, default=30)
    b.add_argument("--out", default=None)
    b.set_defaults(fn=cmd_backtest)
    t = sub.add_parser("tax")
    t.add_argument("--year", type=int, default=time.gmtime().tm_year)
    t.add_argument("--out", default="fifo_rapport.csv")
    t.add_argument("--no-nok", action="store_true")
    t.set_defaults(fn=cmd_tax)
    w = sub.add_parser("wallet-new"); w.add_argument("--path", default="bot-wallet.json"); w.set_defaults(fn=cmd_wallet_new)
    sub.add_parser("wallet").set_defaults(fn=cmd_wallet)
    d = sub.add_parser("dashboard")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8080)
    d.set_defaults(fn=cmd_dashboard)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
