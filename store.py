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
PANEL_KEYS = [
    "gamma_spx",
    "gamma_spy",
    "vix_structure",
    "correlation",
    "volume_profile",
    "calendar",
    "regime",
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
