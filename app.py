"""Regime Dashboard — local SPX regime analysis.

Run:  python app.py     then open http://localhost:8020

FastAPI serves a single-page dashboard plus a small JSON API. All data is
persisted to SQLite so the last session renders immediately on reload. Nothing
auto-refreshes; Tab 1 updates when its REFRESH button is pressed.

Architecture follows market_almanack: one JSON payload per panel key, a failure
in one panel never aborts the others, and a failed fetch keeps the cached
payload visible behind an error badge rather than blanking the panel.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import store
from panels import calendar_context, correlation, gamma_engine, regime
from panels import vix_structure, volume_profile

BASE = Path(__file__).parent
STATIC = BASE / "static"
PORT = 8020


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.init_db()
    yield


app = FastAPI(title="Regime Dashboard", lifespan=lifespan)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def api_state() -> JSONResponse:
    """Full last-known state for initial render."""
    return JSONResponse(store.get_all())


def _run(out: dict, key: str, fn) -> None:
    """Refresh one panel. Never raises — a failure keeps the cached payload."""
    try:
        out[key] = store.save_panel(key, fn(), status="ok")
    except Exception as exc:
        store.update_status(key, "error", f"{type(exc).__name__}: {exc}")
        out[key] = store.get_panel(key)


@app.post("/api/refresh/tab1")
def api_refresh_tab1() -> JSONResponse:
    """Refresh every Tab 1 panel, then derive the regime from the fresh results.

    Panels run in sequence rather than in parallel: the SPX chain is 13 MB and
    the volume profile reuses its spot, so serialising keeps the CBOE request
    rate polite and avoids a duplicate quote.
    """
    out: dict = {}

    _run(out, "gamma_spx", lambda: gamma_engine.refresh(gamma_engine.SPX))
    _run(out, "gamma_spy", lambda: gamma_engine.refresh(gamma_engine.SPY))
    _run(out, "vix_structure", vix_structure.refresh)
    _run(out, "correlation", correlation.refresh)

    # Reuse the SPX spot we just fetched, when it is available.
    spx_payload = (out.get("gamma_spx") or {}).get("payload") or {}
    spx_spot = spx_payload.get("spot")
    _run(out, "volume_profile", lambda: volume_profile.refresh(spx_spot=spx_spot))

    _run(out, "calendar", calendar_context.refresh)

    # The regime panel is a pure function of the others, so it is computed last
    # from whatever they produced — including any stale payloads still cached
    # behind an error badge.
    def _regime():
        panels = {k: store.get_panel(k) for k in
                  ("gamma_spx", "gamma_spy", "vix_structure", "correlation",
                   "volume_profile", "calendar")}
        return regime.compute(panels)

    _run(out, "regime", _regime)
    return JSONResponse(out)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT)
