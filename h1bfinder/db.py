from __future__ import annotations
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings(
  fy INTEGER, norm TEXT, name TEXT, tax_id TEXT, naics TEXT, city TEXT, state TEXT, zip TEXT,
  new_app INTEGER, new_den INTEGER, cont_app INTEGER, cont_den INTEGER,
  other_app INTEGER, other_den INTEGER, source TEXT
);
CREATE INDEX IF NOT EXISTS ix_filings_norm ON filings(norm);
CREATE INDEX IF NOT EXISTS ix_filings_source ON filings(source);
CREATE TABLE IF NOT EXISTS datasets(
  source TEXT PRIMARY KEY, label TEXT, url TEXT, rows INTEGER, fys TEXT, added_at REAL
);
CREATE TABLE IF NOT EXISTS boards(
  norm TEXT, provider TEXT, slug TEXT, found INTEGER, checked_at REAL,
  PRIMARY KEY(norm, provider)
);
CREATE TABLE IF NOT EXISTS jobs(
  uid TEXT PRIMARY KEY, norm TEXT, provider TEXT, slug TEXT, title TEXT, location TEXT,
  is_us INTEGER, url TEXT, description TEXT, department TEXT, posted TEXT, fetched_at REAL
);
CREATE INDEX IF NOT EXISTS ix_jobs_norm ON jobs(norm);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path: Path | str | None = None, init: bool = True) -> sqlite3.Connection:
    p = Path(path) if path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    if init:  # schema/WAL setup only once; concurrent re-init from worker threads causes "database is locked"
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    return conn


def get_meta(conn, k, default=None):
    r = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def set_meta(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v))
    conn.commit()


import threading
_tl = threading.local()


def path_of(conn) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2]


def thread_conn(main_conn):
    """One connection per thread for the same database file."""
    p = path_of(main_conn)
    c = getattr(_tl, "conn", None)
    if c is None or getattr(_tl, "path", None) != p:
        _tl.conn, _tl.path = connect(p, init=False), p
    return _tl.conn
