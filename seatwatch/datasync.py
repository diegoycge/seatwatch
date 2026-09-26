"""Sync through a git branch ("data") that holds config (written by the Mac) and state (written by the cloud).

The branch always holds a single commit: each push builds a fresh parentless commit and pushes it
with --force-with-lease against the commit it was built from, so history (and the SQLite
snapshot inside it) never piles up, and a concurrent push from the other side is retried, not
clobbered.

  config/{watches,windows,balances,settings}.json   Mac-owned
  state/seatwatch.db                                cloud-owned (compacted SQLite snapshot)
  secrets/seats_aero_key                            added by the user, read by the cloud
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from . import config, db

BRANCH = "data"
CONFIG_TABLES = ("watches", "windows")
SYNC_FILE = config.DATA_DIR / "sync.json"


class SyncError(Exception):
    pass


def _git(repo: Path, *args, check=True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        raise SyncError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def sync_settings() -> dict:
    try:
        return json.loads(SYNC_FILE.read_text())
    except (OSError, ValueError):
        return {}


def enabled() -> bool:
    return bool(sync_settings().get("remote"))


def clone_dir() -> Path:
    return config.DATA_DIR / "sync"


def ensure_clone(remote: str = None, path: Path = None) -> Path:
    path = path or clone_dir()
    remote = remote or sync_settings().get("remote")
    if not remote:
        raise SyncError("sync is not set up (seatwatch cloud setup --remote ...)")
    if not (path / ".git").exists():
        path.mkdir(parents=True, exist_ok=True)
        _git(path, "init", "-q")
        _git(path, "remote", "add", "origin", remote)
    _git(path, "config", "user.name", "seatwatch")
    _git(path, "config", "user.email", "seatwatch@localhost")
    return path


def pull(path: Path = None) -> str:
    """Make the working tree match origin/data. Returns the remote commit sha ('' if no branch yet)."""
    path = path or clone_dir()
    out = _git(path, "ls-remote", "origin", f"refs/heads/{BRANCH}")
    if not out:
        return ""
    _git(path, "fetch", "-q", "--depth", "1", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}")
    _git(path, "checkout", "-q", "-B", BRANCH, f"origin/{BRANCH}")
    _git(path, "reset", "-q", "--hard", f"origin/{BRANCH}")
    _git(path, "clean", "-qfd")
    return _git(path, "rev-parse", "HEAD")


def push(writer, message: str, path: Path = None, attempts: int = 6) -> bool:
    """Pull, let writer(path) change files, then push a fresh single commit. Returns True if pushed."""
    path = path or clone_dir()
    for i in range(attempts):
        base = pull(path)
        writer(path)
        _git(path, "add", "-A")
        if base and not _git(path, "status", "--porcelain"):
            return False
        tree = _git(path, "write-tree")
        commit = _git(path, "commit-tree", tree, "-m", message)
        lease = f"--force-with-lease=refs/heads/{BRANCH}:{base}" if base else f"--force-with-lease=refs/heads/{BRANCH}:"
        r = subprocess.run(["git", "-C", str(path), "push", "-q", lease, "origin", f"{commit}:refs/heads/{BRANCH}"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            _git(path, "checkout", "-q", "-B", BRANCH, commit)
            return True
        time.sleep(2 + i * 3)
    raise SyncError(f"push kept losing the race: {r.stderr.strip()}")


# ------------------------------------------------------------------ config <-> JSON

def export_config(conn, root: Path) -> None:
    d = root / "config"
    d.mkdir(parents=True, exist_ok=True)
    for t in CONFIG_TABLES:
        (d / f"{t}.json").write_text(json.dumps(db.rows(conn, f"SELECT * FROM {t} ORDER BY id"), indent=1, sort_keys=True))
    (d / "balances.json").write_text(json.dumps(db.get_balances(conn), indent=1, sort_keys=True))
    s = {r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT key, value FROM settings")}
    (d / "settings.json").write_text(json.dumps(s, indent=1, sort_keys=True))


def import_config(conn, root: Path) -> None:
    d = root / "config"
    if not d.exists():
        return
    conn.execute("BEGIN")
    for t in CONFIG_TABLES:
        f = d / f"{t}.json"
        if not f.exists():
            continue
        rows = json.loads(f.read_text())
        conn.execute(f"DELETE FROM {t}")
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        for r in rows:
            keys = [k for k in r if k in cols]
            conn.execute(f"INSERT INTO {t}({','.join(keys)}) VALUES({','.join('?' * len(keys))})", [r[k] for k in keys])
    f = d / "balances.json"
    if f.exists():
        conn.execute("DELETE FROM balances")
        for k, v in json.loads(f.read_text()).items():
            conn.execute("INSERT INTO balances(account, balance, updated_at) VALUES(?,?,?)", (k, int(v), time.time()))
    f = d / "settings.json"
    if f.exists():
        conn.execute("DELETE FROM settings")
        for k, v in json.loads(f.read_text()).items():
            conn.execute("INSERT INTO settings(key, value) VALUES(?,?)", (k, json.dumps(v)))
    conn.execute("COMMIT")


# ------------------------------------------------------------------ Mac side

STATE_TABLES = ("availability", "matches", "notified", "job_state", "events", "verdicts", "pending")


def publish_config(conn) -> bool:
    """Push the local config (watches, windows, balances, settings) to the data branch."""
    if not enabled():
        return False
    ensure_clone()
    return push(lambda p: export_config(conn, p), "config from Mac")


def pull_state(conn) -> dict:
    """Refresh the local DB's result tables from the cloud's snapshot. Config stays local."""
    if not enabled():
        return {"enabled": False}
    path = ensure_clone()
    sha = pull(path)
    snap = path / "state/seatwatch.db"
    if not snap.exists():
        return {"enabled": True, "sha": sha, "state": False}
    conn.execute("ATTACH DATABASE ? AS c", (str(snap),))
    try:
        conn.execute("BEGIN")
        for t in STATE_TABLES:
            if not conn.execute("SELECT 1 FROM c.sqlite_master WHERE name = ?", (t,)).fetchone():
                continue
            cols = [r[1] for r in conn.execute(f"PRAGMA c.table_info({t})")]
            local = {r[1] for r in conn.execute(f"PRAGMA main.table_info({t})")}
            cols = [x for x in cols if x in local]
            conn.execute(f"DELETE FROM main.{t}")
            conn.execute(f"INSERT INTO main.{t}({','.join(cols)}) SELECT {','.join(cols)} FROM c.{t}")
        # quota: keep every call so the header shows the true remaining count
        conn.execute("INSERT INTO main.api_calls(ts, endpoint, purpose, status, remaining, reset_at, rows, ms, error) "
                     "SELECT ts, endpoint, 'cloud:' || purpose, status, remaining, reset_at, rows, ms, error FROM c.api_calls "
                     "WHERE ts > coalesce((SELECT max(ts) FROM main.api_calls WHERE purpose LIKE 'cloud:%'), 0)")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("DETACH DATABASE c")
    return {"enabled": True, "sha": sha, "state": True, "pulled_at": time.time()}
