"""SQLite-backed persistence for the Regime Dashboard.

Every panel stores a single JSON payload plus metadata (timestamp / status /
error), so reload-on-startup is trivial: the frontend asks for the full state and
renders whatever was saved last session before any new fetch runs. Ported from
market_almanack's store, which has the same contract.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).with_name("regime.db")

# Panel keys are the contract shared with the frontend.
# `regime` is derived from the others and is always computed last.
#
# The `t2_*` keys hold whichever ticker was analysed most recently, rather than
# one row per ticker. Per-ticker keys would make `get_all()` unbounded and force
# the frontend to discover keys dynamically, for no benefit: the tab shows one
# ticker at a time, and cross-ticker data belongs in `ticker_history` below.
# Every t2 payload carries a "symbol" field so the UI can label what it is
# showing — and so a failed refresh can tell whether the cached payload belongs
# to the ticker being asked about.
PANEL_KEYS = [
    "gamma_spx",
    "gamma_spy",
    "vix_structure",
    "correlation",
    "volume_profile",
    "calendar",
    "cftc_positioning",
    "regime",
    # Tab 2 — ticker sentiment
    "t2_positioning",
    "t2_vol",
    "t2_squeeze",
    "t2_social",
    "t2_news",
    "t2_events",
    "t2_sentiment",
]

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS panels (
                panel_key   TEXT PRIMARY KEY,
                payload     TEXT,           -- JSON
                status      TEXT,           -- 'ok' | 'error' | 'empty'
                error       TEXT,           -- last error message, if any
                updated_at  REAL            -- epoch seconds (UTC)
            )
            """
        )
        # One flat snapshot per ticker per day. This is what makes the
        # trend-dependent metrics possible at all: IV rank, social mention
        # velocity against a baseline, short-interest drift and the composite
        # sparkline are all meaningless as a single point reading.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ticker_history (
                ticker      TEXT NOT NULL,
                date        TEXT NOT NULL,  -- 'YYYY-MM-DD' (UTC)
                snapshot    TEXT NOT NULL,  -- JSON, flat scalars only
                updated_at  REAL NOT NULL,
                PRIMARY KEY (ticker, date)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_ticker_history "
            "ON ticker_history(ticker, date)"
        )
        # Small persistent cache for slow-changing remote lookups (the SEC's
        # 10k-entry ticker->CIK map), so a restart does not refetch them.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at REAL
            )
            """
        )


def save_panel(
    panel_key: str,
    payload: Any,
    status: str = "ok",
    error: str | None = None,
    updated_at: float | None = None,
) -> dict:
    """Persist a panel. Returns the stored record (as the API exposes it)."""
    if updated_at is None:
        updated_at = time.time()
    record = {
        "payload": payload,
        "status": status,
        "error": error,
        "updated_at": updated_at,
    }
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO panels (panel_key, payload, status, error, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(panel_key) DO UPDATE SET
                payload=excluded.payload,
                status=excluded.status,
                error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (panel_key, json.dumps(payload), status, error, updated_at),
        )
    return {"panel_key": panel_key, **record}


def update_status(panel_key: str, status: str, error: str | None) -> None:
    """Mark a panel's status/error without touching its cached payload.

    Used on fetch failure so the stale payload stays visible behind an
    error badge.
    """
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE panels SET status=?, error=?, updated_at=? WHERE panel_key=?",
            (status, error, time.time(), panel_key),
        )


def get_panel(panel_key: str) -> dict | None:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM panels WHERE panel_key=?", (panel_key,)
        ).fetchone()
    if row is None:
        return None
    return {
        "panel_key": row["panel_key"],
        "payload": json.loads(row["payload"]) if row["payload"] else None,
        "status": row["status"],
        "error": row["error"],
        "updated_at": row["updated_at"],
    }


def get_payload(panel_key: str):
    """Just the cached payload (or None). Used by the regime classifier."""
    rec = get_panel(panel_key)
    return (rec or {}).get("payload")


def get_all() -> dict[str, dict | None]:
    """Return every known panel keyed by panel_key (None if never fetched)."""
    return {key: get_panel(key) for key in PANEL_KEYS}


# --------------------------------------------------------------------------- #
# Per-ticker daily history (Tab 2)
# --------------------------------------------------------------------------- #

def save_snapshot(ticker: str, snapshot: dict, day: str | None = None) -> None:
    """Upsert one flat daily snapshot for a ticker.

    Keyed on (ticker, date), so pressing ANALYSE repeatedly in a day overwrites
    rather than accumulating — the series stays one point per session.
    """
    if day is None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO ticker_history (ticker, date, snapshot, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ticker, date) DO UPDATE SET
                snapshot=excluded.snapshot,
                updated_at=excluded.updated_at
            """,
            (ticker.upper(), day, json.dumps(snapshot), time.time()),
        )


def get_history(ticker: str, days: int = 365) -> list[dict]:
    """Daily snapshots for a ticker, oldest first, each flattened to {date, **snapshot}."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT date, snapshot FROM ticker_history WHERE ticker=? "
            "ORDER BY date DESC LIMIT ?",
            (ticker.upper(), days),
        ).fetchall()

    out = []
    for row in reversed(rows):  # back to ascending
        try:
            payload = json.loads(row["snapshot"])
        except (TypeError, ValueError):
            continue
        out.append({"date": row["date"], **payload})
    return out


def kv_get(key: str, max_age: float | None = None):
    """Cached value, or None when absent or older than ``max_age`` seconds."""
    with _lock, _connect() as conn:
        row = conn.execute("SELECT value, updated_at FROM kv WHERE key=?",
                           (key,)).fetchone()
    if row is None:
        return None
    if max_age is not None and (time.time() - (row["updated_at"] or 0)) > max_age:
        return None
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return None


def kv_put(key: str, value) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, json.dumps(value), time.time()),
        )
