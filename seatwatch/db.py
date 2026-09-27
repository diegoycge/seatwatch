"""SQLite storage. Every thread/process opens its own connection via connect()."""
from __future__ import annotations

import json
import sqlite3
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS balances(account TEXT PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0, updated_at REAL);
CREATE TABLE IF NOT EXISTS watches(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT,
  origins TEXT NOT NULL, destinations TEXT NOT NULL,
  depart_start TEXT NOT NULL, depart_end TEXT NOT NULL,
  return_start TEXT, return_end TEXT,
  cabins TEXT NOT NULL DEFAULT 'business', max_miles INTEGER, pax INTEGER NOT NULL DEFAULT 1,
  direct_only INTEGER NOT NULL DEFAULT 0, sources TEXT,
  priority INTEGER NOT NULL DEFAULT 3, interval_min INTEGER NOT NULL DEFAULT 45,
  enabled INTEGER NOT NULL DEFAULT 1, notify INTEGER NOT NULL DEFAULT 1, created_at REAL,
  picks_only INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS windows(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  start_date TEXT NOT NULL, end_date TEXT NOT NULL,
  origins TEXT, destinations TEXT NOT NULL, cabins TEXT NOT NULL DEFAULT 'business',
  pax INTEGER NOT NULL DEFAULT 1, direct_only INTEGER NOT NULL DEFAULT 0,
  roundtrip INTEGER NOT NULL DEFAULT 1, explore INTEGER NOT NULL DEFAULT 1,
  min_score REAL NOT NULL DEFAULT 1.0, notify INTEGER NOT NULL DEFAULT 1,
  enabled INTEGER NOT NULL DEFAULT 1, created_at REAL);
CREATE TABLE IF NOT EXISTS job_state(
  key TEXT PRIMARY KEY, last_run REAL, last_calls INTEGER, last_rows INTEGER,
  last_matches INTEGER, last_error TEXT, truncated INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS availability(
  id TEXT PRIMARY KEY, source TEXT, origin TEXT, dest TEXT, origin_region TEXT, dest_region TEXT,
  distance INTEGER, date TEXT, cabins TEXT, currency TEXT, updated_at TEXT, fetched_at REAL);
CREATE INDEX IF NOT EXISTS av_date ON availability(date);
CREATE INDEX IF NOT EXISTS av_route ON availability(origin, dest);
CREATE TABLE IF NOT EXISTS matches(
  owner TEXT, avail_id TEXT, cabin TEXT, cost INTEGER, active INTEGER,
  first_seen REAL, last_seen REAL, PRIMARY KEY(owner, avail_id, cabin));
CREATE TABLE IF NOT EXISTS notified(avail_id TEXT, cabin TEXT, cost INTEGER, at REAL, PRIMARY KEY(avail_id, cabin));
CREATE TABLE IF NOT EXISTS api_calls(
  id INTEGER PRIMARY KEY, ts REAL, endpoint TEXT, purpose TEXT, status INTEGER,
  remaining INTEGER, reset_at REAL, rows INTEGER, ms INTEGER, error TEXT);
CREATE INDEX IF NOT EXISTS calls_ts ON api_calls(ts);
CREATE TABLE IF NOT EXISTS pending(
  avail_id TEXT, cabin TEXT, owner TEXT, cost INTEGER, kind TEXT, payload TEXT, created REAL,
  PRIMARY KEY(avail_id, cabin, owner));
CREATE TABLE IF NOT EXISTS verdicts(
  avail_id TEXT, cabin TEXT, cost INTEGER, verdict TEXT, reason TEXT, at REAL, PRIMARY KEY(avail_id, cabin));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, title TEXT, body TEXT, link TEXT);
"""


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(watches)")}
    if "picks_only" not in cols:  # added after first release
        conn.execute("ALTER TABLE watches ADD COLUMN picks_only INTEGER NOT NULL DEFAULT 0")
    return conn


def get_settings(conn) -> dict:
    s = json.loads(json.dumps(config.DEFAULT_SETTINGS))
    for r in conn.execute("SELECT key, value FROM settings"):
        s[r["key"]] = json.loads(r["value"])
    return s


def set_settings(conn, updates: dict) -> None:
    for k, v in updates.items():
        if k in config.DEFAULT_SETTINGS:
            conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (k, json.dumps(v)))


def get_balances(conn) -> dict:
    return {r["account"]: r["balance"] for r in conn.execute("SELECT account, balance FROM balances")}


def set_balances(conn, updates: dict) -> None:
    now = time.time()
    for acct, bal in updates.items():
        if acct not in config.ACCOUNT_NAMES:
            raise ValueError(f"unknown account '{acct}'")
        conn.execute("INSERT OR REPLACE INTO balances(account, balance, updated_at) VALUES(?, ?, ?)",
                     (acct, max(0, int(bal or 0)), now))


def log_event(conn, kind: str, title: str, body: str = "", link: str = "") -> None:
    conn.execute("INSERT INTO events(ts, kind, title, body, link) VALUES(?,?,?,?,?)",
                 (time.time(), kind, title, body, link))


def rows(conn, sql, args=()) -> list:
    return [dict(r) for r in conn.execute(sql, args)]
