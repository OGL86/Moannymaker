"""SQLite-lagring: handler, posisjonsstatus, migrasjoner og hendelseslogg."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,            -- unix sekunder
    mode TEXT NOT NULL,             -- paper | live | backtest
    bucket TEXT NOT NULL,           -- core | rotation | shots
    mint TEXT NOT NULL,
    symbol TEXT,
    side TEXT NOT NULL,             -- buy | sell
    qty REAL NOT NULL,              -- antall tokens
    price_usd REAL NOT NULL,
    usd REAL NOT NULL,              -- brutto verdi
    fee_usd REAL NOT NULL DEFAULT 0,
    reason TEXT,
    tx_sig TEXT
);
CREATE TABLE IF NOT EXISTS position_state (
    mint TEXT PRIMARY KEY,
    bucket TEXT NOT NULL,
    symbol TEXT,
    tranches INTEGER NOT NULL DEFAULT 0,
    tp_hits INTEGER NOT NULL DEFAULT 0,
    breakeven_armed INTEGER NOT NULL DEFAULT 0,
    dead INTEGER NOT NULL DEFAULT 0,
    initial_qty REAL NOT NULL DEFAULT 0,
    updated INTEGER
);
CREATE TABLE IF NOT EXISTS migrations (
    mint TEXT PRIMARY KEY,
    symbol TEXT,
    pool TEXT,
    ts INTEGER NOT NULL,
    raw TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    level TEXT NOT NULL,
    msg TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    mode TEXT NOT NULL,
    equity REAL NOT NULL,
    executed INTEGER NOT NULL,
    blocked TEXT,                   -- JSON-liste
    candidates INTEGER NOT NULL,
    rejected TEXT                   -- JSON-liste
);
CREATE TABLE IF NOT EXISTS wallet_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    mint TEXT NOT NULL,
    side TEXT NOT NULL,
    sol REAL NOT NULL,
    tokens REAL NOT NULL,
    signature TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS wt_wallet ON wallet_trades(wallet, ts);
CREATE TABLE IF NOT EXISTS copy_links (
    mint TEXT NOT NULL,
    wallet TEXT NOT NULL,
    PRIMARY KEY (mint, wallet)
);
CREATE TABLE IF NOT EXISTS prices (
    mint TEXT PRIMARY KEY,
    symbol TEXT,
    price_usd REAL NOT NULL,
    ts INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # --- handler -------------------------------------------------------
    def add_trade(self, **t) -> int:
        t.setdefault("ts", int(time.time()))
        cols = ",".join(t)
        cur = self.db.execute(f"INSERT INTO trades ({cols}) VALUES ({','.join('?' * len(t))})", tuple(t.values()))
        self.db.commit()
        return cur.lastrowid

    def trades(self, mode: str | None = None, mint: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM trades WHERE 1=1", []
        if mode:
            q += " AND mode=?"
            args.append(mode)
        if mint:
            q += " AND mint=?"
            args.append(mint)
        return [dict(r) for r in self.db.execute(q + " ORDER BY ts, id", args)]

    def trades_since(self, ts: int, mode: str, bucket: str | None = None, exclude_bucket: str | None = None) -> int:
        q, args = "SELECT COUNT(*) FROM trades WHERE ts>=? AND mode=?", [ts, mode]
        if bucket:
            q += " AND bucket=?"
            args.append(bucket)
        if exclude_bucket:
            q += " AND bucket!=?"
            args.append(exclude_bucket)
        return self.db.execute(q, args).fetchone()[0]

    # --- lederwallets (kopitrading) ------------------------------------
    def add_wallet_trade(self, ts: int, wallet: str, mint: str, side: str, sol: float, tokens: float,
                         signature: str | None) -> None:
        self.db.execute("INSERT OR IGNORE INTO wallet_trades (ts, wallet, mint, side, sol, tokens, signature) "
                        "VALUES (?,?,?,?,?,?,?)", (ts, wallet, mint, side, sol, tokens, signature))
        self.db.commit()

    def wallet_trades(self, wallet: str | None = None, since: int = 0) -> list[dict]:
        q, args = "SELECT * FROM wallet_trades WHERE ts>=?", [since]
        if wallet:
            q += " AND wallet=?"
            args.append(wallet)
        return [dict(r) for r in self.db.execute(q + " ORDER BY ts, id", args)]

    # --- posisjonsstatus ----------------------------------------------
    def get_state(self, mint: str) -> dict | None:
        r = self.db.execute("SELECT * FROM position_state WHERE mint=?", (mint,)).fetchone()
        return dict(r) if r else None

    def upsert_state(self, mint: str, **fields) -> None:
        fields["updated"] = int(time.time())
        cur = self.get_state(mint)
        if cur is None:
            fields["mint"] = mint
            cols = ",".join(fields)
            self.db.execute(f"INSERT INTO position_state ({cols}) VALUES ({','.join('?' * len(fields))})", tuple(fields.values()))
        else:
            sets = ",".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE position_state SET {sets} WHERE mint=?", (*fields.values(), mint))
        self.db.commit()

    def delete_state(self, mint: str) -> None:
        self.db.execute("DELETE FROM position_state WHERE mint=?", (mint,))
        self.db.commit()

    def all_states(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM position_state")]

    # --- migrasjoner (fra PumpPortal) ---------------------------------
    def add_migration(self, mint: str, symbol: str | None, pool: str | None, raw: dict, ts: int | None = None) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO migrations (mint, symbol, pool, ts, raw) VALUES (?,?,?,?,?)",
            (mint, symbol, pool, ts or int(time.time()), json.dumps(raw)),
        )
        self.db.commit()

    def migrations_between(self, min_age_days: float, max_age_days: float) -> list[dict]:
        now = time.time()
        lo, hi = now - max_age_days * 86400, now - min_age_days * 86400
        return [dict(r) for r in self.db.execute("SELECT * FROM migrations WHERE ts BETWEEN ? AND ?", (lo, hi))]

    def migration_ts(self, mint: str) -> int | None:
        r = self.db.execute("SELECT ts FROM migrations WHERE mint=?", (mint,)).fetchone()
        return r[0] if r else None

    # --- logg -----------------------------------------------------------
    def log(self, level: str, msg: str) -> None:
        self.db.execute("INSERT INTO events (ts, level, msg) VALUES (?,?,?)", (int(time.time()), level, msg))
        self.db.commit()

    def events(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    # --- runder og priser (for dashboard) --------------------------------
    def add_run(self, mode: str, equity: float, executed: int, blocked: list, candidates: int,
                rejected: list, ts: int | None = None) -> None:
        self.db.execute(
            "INSERT INTO runs (ts, mode, equity, executed, blocked, candidates, rejected) VALUES (?,?,?,?,?,?,?)",
            (ts or int(time.time()), mode, equity, executed, json.dumps(blocked), candidates, json.dumps(rejected)),
        )
        self.db.commit()

    def runs(self, mode: str, limit: int = 2000) -> list[dict]:
        rows = self.db.execute("SELECT * FROM runs WHERE mode=? ORDER BY id DESC LIMIT ?", (mode, limit))
        out = []
        for r in rows:
            d = dict(r)
            d["blocked"] = json.loads(d["blocked"] or "[]")
            d["rejected"] = json.loads(d["rejected"] or "[]")
            out.append(d)
        return out[::-1]

    def set_prices(self, prices: dict[str, tuple[str, float]]) -> None:
        now = int(time.time())
        self.db.executemany(
            "INSERT INTO prices (mint, symbol, price_usd, ts) VALUES (?,?,?,?) "
            "ON CONFLICT(mint) DO UPDATE SET symbol=excluded.symbol, price_usd=excluded.price_usd, ts=excluded.ts",
            [(m, s, p, now) for m, (s, p) in prices.items()],
        )
        self.db.commit()

    def prices(self) -> dict[str, dict]:
        return {r["mint"]: dict(r) for r in self.db.execute("SELECT * FROM prices")}

    def migration_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
