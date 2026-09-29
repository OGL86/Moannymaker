"""Lokalt web-dashboard: python -m memebot dashboard  →  http://127.0.0.1:8080

Leser samme SQLite-database som boten. Kan:
  - vise status, egenkapital, bøtter, posisjoner, handler, avvisninger og logg
  - stoppe/starte handel (kill switch-fila)
  - endre enkle innstillinger i config.yaml (boten leser dem ved neste runde)

Kun standardbiblioteket. Lytter på 127.0.0.1 som standard. Sett DASHBOARD_TOKEN i .env
hvis du eksponerer den videre (f.eks. via VPN) – da kreves ?token=... i adressen.
"""
from __future__ import annotations

import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from .config import ROOT, load_config
from .portfolio import Portfolio
from .storage import Store

WEB = Path(__file__).parent / "web"
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
BUCKET_LABELS = {
    "core": ("Grunnmur", "Etablerte coins som kjøpes billig og holdes lenge"),
    "rotation": ("Rotasjon", "Kjøpes nederst i sitt vanlige prisområde, selges øverst"),
    "shots": ("Småsatsinger", "Små beløp i nyere coins. Regn med at de fleste går til null"),
    "copy": ("Kopitrading", "Kjøper og selger når walletene du følger gjør det"),
}


def build_state(cfg: dict, store: Store, config_path: Path) -> dict:
    runs_all = store.db.execute("SELECT mode FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    mode = runs_all[0] if runs_all else "paper"
    runs = store.runs(mode)
    trades = store.trades(mode)
    pf = Portfolio(cfg["start_capital_usd"], cfg["buckets"], trades)
    prices = store.prices()
    states = {s["mint"]: s for s in store.all_states()}
    now = time.time()

    last_ts = runs[-1]["ts"] if runs else None
    loop_s = cfg["loop_minutes"] * 60
    if last_ts is None:
        status = "never"
    elif now - last_ts <= loop_s * 2 + 120:
        status = "running"
    else:
        status = "stale"

    positions = []
    for p in pf.open_positions():
        price = prices.get(p.mint, {}).get("price_usd")
        value = p.qty * price if price else None
        st = states.get(p.mint) or {}
        positions.append({
            "symbol": p.symbol, "mint": p.mint, "bucket": p.bucket, "qty": p.qty,
            "avg_price": p.avg_price, "price": price, "cost": p.cost_usd, "value": value,
            "pnl_usd": value - p.cost_usd if value is not None else None,
            "pnl_pct": (value / p.cost_usd - 1) * 100 if value is not None and p.cost_usd else None,
            "tp_hits": st.get("tp_hits", 0), "dead": bool(st.get("dead")),
            "tranches": st.get("tranches", 0),
        })
    positions.sort(key=lambda x: -(x["value"] or x["cost"]))

    price_map = {m: v["price_usd"] for m, v in prices.items()}
    equity = pf.equity(price_map)
    buckets = []
    for b, w in cfg["buckets"].items():
        if w <= 0 and not any(x["bucket"] == b for x in positions) and not pf.realized.get(b):
            continue  # skjul potter som ikke er i bruk
        label, explain = BUCKET_LABELS.get(b, (b, ""))
        invested_value = sum((x["value"] if x["value"] is not None else x["cost"]) for x in positions if x["bucket"] == b)
        buckets.append({
            "key": b, "label": label, "explain": explain, "weight": w,
            "start": cfg["start_capital_usd"] * w, "capital": pf.bucket_capital(b),
            "invested": pf.bucket_invested(b), "invested_value": invested_value,
            "cash": pf.bucket_cash(b), "realized": pf.realized.get(b, 0.0),
        })

    month_start = time.strftime("%Y-%m-01", time.gmtime())
    trades_month = sum(1 for t in trades if time.strftime("%Y-%m-%d", time.gmtime(t["ts"])) >= month_start)
    user_cfg = yaml.safe_load(config_path.read_text()) if config_path.exists() else None

    checklist = [
        {"key": "config", "done": user_cfg is not None,
         "text": "Lag innstillingsfila", "help": "Lagres automatisk første gang du trykker «Lagre» under Innstillinger."},
        {"key": "core", "done": bool(cfg.get("core_tokens")),
         "text": "Legg til minst én coin i grunnmuren", "help": "Lim inn adressen under Innstillinger."},
        {"key": "listener", "done": store.migration_count() > 0,
         "text": "Start lytteren som finner nye kandidater", "help": "Kjør: python -m memebot listen"},
        {"key": "bot", "done": bool(runs),
         "text": "Start boten", "help": "Kjør: python -m memebot run  (øvingsmodus – ingen ekte penger)"},
        {"key": "telegram", "done": bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")),
         "text": "Koble til Telegram (valgfritt)", "help": "Fyll inn TELEGRAM_BOT_TOKEN og TELEGRAM_CHAT_ID i .env"},
    ]

    ct = cfg["copytrade"]
    since = int(now) - 30 * 86400
    from .copytrade import wallet_stats
    wallets = []
    for w in ct["wallets"]:
        wt = store.wallet_trades(w["address"], since=since)
        st = wallet_stats(wt)
        st.update(address=w["address"], label=w.get("label") or w["address"][:6],
                  last_seen=wt[-1]["ts"] if wt else None)
        wallets.append(st)
    copy_live = runs_all is not None and store.db.execute(
        "SELECT MAX(ts) FROM wallet_trades").fetchone()[0]

    last = runs[-1] if runs else None
    return {
        "now": int(now), "mode": mode, "status": status, "last_run": last_ts, "loop_minutes": cfg["loop_minutes"],
        "killed": (ROOT / cfg["risk"]["kill_switch_file"]).exists(),
        "start_capital": cfg["start_capital_usd"], "equity": equity,
        "curve": [[r["ts"], round(r["equity"], 2)] for r in runs[-500:]],
        "buckets": buckets, "positions": positions,
        "trades": [dict(t) for t in reversed(trades[-150:])],
        "events": store.events(60),
        "last": {"executed": last["executed"], "blocked": last["blocked"], "candidates": last["candidates"],
                 "rejected": last["rejected"][:40]} if last else None,
        "limits": {"trades_month": trades_month, **{k: cfg["risk"][k] for k in
                   ("max_trades_per_month", "max_position_usd", "max_daily_loss_usd")}},
        "realized_today": pf.realized_today(),
        "checklist": checklist,
        "copy": {"enabled": ct["enabled"], "mode": ct["mode"], "weight": cfg["buckets"].get("copy", 0),
                 "wallets": wallets, "last_event": copy_live,
                 "has_api_key": bool(os.environ.get("PUMPPORTAL_API_KEY"))},
        "settings": {
            "start_capital_usd": cfg["start_capital_usd"],
            "loop_minutes": cfg["loop_minutes"],
            "risk": {k: cfg["risk"][k] for k in ("max_position_usd", "max_daily_loss_usd", "max_trades_per_month")},
            "core_tokens": cfg.get("core_tokens") or [],
            "rotation_tokens": cfg.get("rotation_tokens") or [],
            "buckets": {k: round(v * 100) for k, v in cfg["buckets"].items()},
            "copy_enabled": ct["enabled"], "copy_mode": ct["mode"], "copy_buy_usd": ct["buy_usd"],
            "copy_wallets": [{"address": w["address"], "label": w.get("label") or ""} for w in ct["wallets"]],
        },
    }


def save_settings(config_path: Path, body: dict) -> dict:
    """Validerer og skriver de enkle innstillingene inn i config.yaml. Returnerer feil per felt."""
    errors: dict[str, str] = {}
    user = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
    user = user or {}

    def num(key, val, lo, hi, integer=False):
        try:
            v = int(val) if integer else float(val)
        except (TypeError, ValueError):
            errors[key] = "Må være et tall"
            return None
        if not lo <= v <= hi:
            errors[key] = f"Må være mellom {lo:g} og {hi:g}"
            return None
        return v

    cap = num("start_capital_usd", body.get("start_capital_usd"), 10, 10_000_000)
    loop = num("loop_minutes", body.get("loop_minutes"), 5, 1440, integer=True)
    risk = body.get("risk") or {}
    mp = num("max_position_usd", risk.get("max_position_usd"), 1, 10_000_000)
    ml = num("max_daily_loss_usd", risk.get("max_daily_loss_usd"), 1, 10_000_000)
    mt = num("max_trades_per_month", risk.get("max_trades_per_month"), 1, 1000, integer=True)

    buckets = {}
    if "buckets" in body:
        for k in ("core", "rotation", "shots", "copy"):
            v = num(f"bucket_{k}", (body["buckets"] or {}).get(k, 0), 0, 100)
            buckets[k] = v
        if not any(f"bucket_{k}" in errors for k in buckets) and abs(sum(buckets.values()) - 100) > 0.01:
            errors["buckets"] = f"Pottene må til sammen bli 100 % (nå {sum(buckets.values()):g} %)"

    copy = {}
    if "copy_enabled" in body:
        copy["enabled"] = bool(body.get("copy_enabled"))
        mode = body.get("copy_mode")
        if mode not in ("mirror", "consensus"):
            errors["copy_mode"] = "Velg en av modusene"
        copy["mode"] = mode
        copy["buy_usd"] = num("copy_buy_usd", body.get("copy_buy_usd"), 1, 100_000)
        wl = []
        for w in body.get("copy_wallets") or []:
            addr = str(w.get("address", "")).strip()
            if not MINT_RE.match(addr):
                errors["copy_wallets"] = f"Ugyldig walletadresse: {addr[:12]}…"
                continue
            if addr not in {x["address"] for x in wl}:
                wl.append({"address": addr, "label": str(w.get("label") or "").strip()[:30]})
        copy["wallets"] = wl
        if copy["enabled"] and not wl:
            errors["copy_wallets"] = "Legg til minst én wallet før du slår på kopitrading"

    tokens = {}
    for key in ("core_tokens", "rotation_tokens"):
        clean = []
        for t in body.get(key) or []:
            mint = str(t.get("mint", "")).strip()
            if not MINT_RE.match(mint):
                errors[key] = f"Ugyldig adresse: {mint[:12]}…"
                continue
            clean.append({"mint": mint, "symbol": str(t.get("symbol") or "?").strip()[:20]})
        tokens[key] = clean

    if errors:
        return errors
    user["start_capital_usd"] = cap
    user["loop_minutes"] = loop
    user.setdefault("risk", {}).update({"max_position_usd": mp, "max_daily_loss_usd": ml, "max_trades_per_month": mt})
    user.update(tokens)
    if buckets:
        user["buckets"] = {k: round(v / 100, 4) for k, v in buckets.items()}
    if copy:
        user.setdefault("copytrade", {}).update(copy)
    tmp = config_path.with_name(config_path.name + ".tmp")
    tmp.write_text("# Sist lagret fra dashboardet. Andre innstillinger: se config.example.yaml\n"
                   + yaml.safe_dump(user, allow_unicode=True, sort_keys=False))
    try:
        load_config(tmp)      # valider hele resultatet FØR den ekte fila byttes ut
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return {"general": f"Ugyldig oppsett: {e}"}
    tmp.replace(config_path)
    return {}


def make_handler(config_path: Path, token: str | None, lookup):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # stille
            pass

        def _auth(self) -> bool:
            if not token:
                return True
            q = parse_qs(urlparse(self.path).query)
            return q.get("token", [""])[0] == token or self.headers.get("X-Token") == token

        def _json(self, code: int, data) -> None:
            body = json.dumps(data, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _cfg_store(self):
            cfg = load_config(config_path)
            return cfg, Store(ROOT / cfg["storage"]["db_path"])

        def do_GET(self):
            if not self._auth():
                return self._json(401, {"error": "Feil eller manglende token i adressen (?token=...)"})
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                body = (WEB / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/state":
                try:
                    cfg, store = self._cfg_store()
                    self._json(200, build_state(cfg, store, config_path))
                except Exception as e:
                    self._json(500, {"error": f"Kunne ikke lese data: {e}"})
            elif path == "/api/lookup":
                mint = parse_qs(urlparse(self.path).query).get("mint", [""])[0].strip()
                if not MINT_RE.match(mint):
                    return self._json(400, {"error": "Det ser ikke ut som en Solana-adresse"})
                try:
                    self._json(200, lookup(mint))
                except Exception as e:
                    self._json(502, {"error": f"Fant ikke coinen: {e}"})
            else:
                self._json(404, {"error": "finnes ikke"})

        def do_POST(self):
            # Egendefinert header hindrer at andre nettsider kan sende skjema hit (CSRF)
            if not self._auth() or self.headers.get("X-Memebot") != "1":
                return self._json(403, {"error": "ikke tillatt"})
            length = min(int(self.headers.get("Content-Length") or 0), 100_000)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "ugyldig JSON"})
            path = urlparse(self.path).path
            cfg, store = self._cfg_store()
            if path == "/api/kill":
                kill = ROOT / cfg["risk"]["kill_switch_file"]
                if body.get("on"):
                    kill.parent.mkdir(parents=True, exist_ok=True)
                    kill.write_text(f"stoppet fra dashboardet {time.ctime()}\n")
                    store.log("risk", "Handel STOPPET fra dashboardet")
                else:
                    kill.unlink(missing_ok=True)
                    store.log("risk", "Handel STARTET igjen fra dashboardet")
                self._json(200, {"killed": kill.exists()})
            elif path == "/api/settings":
                errors = save_settings(config_path, body)
                if errors:
                    return self._json(400, {"errors": errors})
                store.log("info", "Innstillinger endret fra dashboardet")
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "finnes ikke"})

    return H


def serve(config_path: Path, host: str, port: int, lookup) -> None:
    token = os.environ.get("DASHBOARD_TOKEN") or None
    if host not in ("127.0.0.1", "localhost") and not token:
        print("ADVARSEL: dashboardet lytter på et åpent nettverksgrensesnitt uten DASHBOARD_TOKEN.")
    srv = ThreadingHTTPServer((host, port), make_handler(config_path, token, lookup))
    url = f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}/" + (f"?token={token}" if token else "")
    print(f"Dashboard: {url}")
    srv.serve_forever()
