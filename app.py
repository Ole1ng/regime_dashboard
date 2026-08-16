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

import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import store
from panels import calendar_context, cftc_positioning, correlation, gamma_engine
from panels import regime, spy_positioning, vix_structure, volume_profile
from panels import (_finviz, ticker_events, ticker_news, ticker_positioning,
                    ticker_sentiment, ticker_social, ticker_squeeze,
                    vol_sentiment)
from panels import _feeds, news_screener

BASE = Path(__file__).parent
STATIC = BASE / "static"
PORT = 8020

# Only SEC_USER_AGENT lives here today — see .env.example. Safe to load after
# the panels are imported because ticker_events reads the variable at call time
# rather than binding it at import.
load_dotenv(BASE / ".env")

# Tickers are 1-5 letters, optionally with a dot or dash class suffix (BRK.B).
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z.\-]{0,5}$")

# Tab 2 writes seven panel keys; two concurrent presses would interleave their
# writes and leave a mix of two tickers on screen.
_TAB2_LOCK = threading.Lock()

# Panel keys refreshed by the Tab 2 endpoint, in render order.
_TAB2_KEYS = ("t2_positioning", "t2_vol", "t2_squeeze", "t2_social",
              "t2_news", "t2_events", "t2_sentiment")

# Tab 3's eight panels all come out of a single fetch, so a second concurrent
# press would duplicate ~40 third-party requests for no benefit.
_TAB3_LOCK = threading.Lock()
_TAB3_KEYS = _feeds.PANEL_KEYS


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.init_db()
    # Let the EDGAR ticker->CIK map (~1 MB, 10k entries, changes weekly)
    # survive restarts. Injected rather than imported so the panels stay
    # free of any dependency on the store.
    ticker_events.CACHE_GET = store.kv_get
    ticker_events.CACHE_PUT = store.kv_put
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

    One button refreshes the whole tab — there are no per-panel refresh
    endpoints, so every panel added here must be cheap enough to sit inside a
    single press.
    """
    out: dict = {}

    _run(out, "gamma_spx", lambda: gamma_engine.refresh(gamma_engine.SPX))
    _run(out, "gamma_spy", spy_positioning.refresh)
    _run(out, "vix_structure", vix_structure.refresh)
    _run(out, "correlation", correlation.refresh)

    # Reuse the SPX spot we just fetched, when it is available.
    spx_payload = (out.get("gamma_spx") or {}).get("payload") or {}
    spx_spot = spx_payload.get("spot")
    _run(out, "volume_profile", lambda: volume_profile.refresh(spx_spot=spx_spot))

    _run(out, "calendar", calendar_context.refresh)

    # CFTC runs last of the fetching panels: it is the slowest (three Socrata
    # queries plus three yfinance histories) and the only one the regime does
    # not consume, so a hang there leaves every other panel already persisted.
    _run(out, "cftc_positioning", cftc_positioning.refresh)

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


# --------------------------------------------------------------------------- #
# Tab 2 — ticker sentiment
# --------------------------------------------------------------------------- #

def _run_t2(out: dict, key: str, sym: str, fn) -> None:
    """Refresh one Tab 2 panel, never showing a DIFFERENT ticker's cached data.

    Tab 1's `_run` deliberately keeps a stale payload visible behind an error
    badge, which is right when the subject never changes: yesterday's SPX gamma
    is still SPX gamma. Tab 2's subject changes every press, so the same
    behaviour would render NVDA's short interest under a WEN header after a
    failed Finviz fetch. The cached payload is therefore only preserved when it
    belongs to the ticker actually being asked about.
    """
    try:
        out[key] = store.save_panel(key, fn(), status="ok")
        return
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"

    cached = store.get_panel(key)
    same_symbol = ((cached or {}).get("payload") or {}).get("symbol") == sym
    if same_symbol:
        store.update_status(key, "error", message)
        out[key] = store.get_panel(key)
    else:
        out[key] = store.save_panel(
            key, {"symbol": sym, "unavailable": True, "message": message},
            status="error", error=message)


def _t2_placeholder(out: dict, sym: str, message: str) -> None:
    """Write the same tidy 'nothing to show' payload to every Tab 2 panel."""
    for key in _TAB2_KEYS:
        out[key] = store.save_panel(
            key, {"symbol": sym, "not_found": True, "message": message},
            status="ok")


@app.post("/api/refresh/tab2")
def api_refresh_tab2(symbol: str) -> JSONResponse:
    """Refresh every Tab 2 panel for one ticker, then derive the composite.

    Ordering is deliberate, following the same reasoning as Tab 1: the shared,
    cheap and reliable fetches run first so that a hang in a fragile source
    leaves everything before it already persisted. Three network fetches feed
    seven panels — the CBOE chain (positioning, volatility, implied move), the
    Finviz page (squeeze, news rows, earnings date) and one yfinance history
    (realised vol, post-earnings moves).
    """
    sym = (symbol or "").strip().upper()
    out: dict = {}

    if not _SYMBOL_RE.fullmatch(sym):
        _t2_placeholder(out, sym, f"'{symbol}' is not a valid ticker symbol.")
        return JSONResponse(out)

    if not _TAB2_LOCK.acquire(blocking=False):
        return JSONResponse(
            {"detail": "A Tab 2 refresh is already running."}, status_code=409)

    try:
        history = store.get_history(sym)

        # --- shared fetches ------------------------------------------------- #
        chain = chain_error = None
        try:
            chain = ticker_positioning.fetch_chain(sym)
        except ticker_positioning.NotFound as exc:
            chain_error = exc
        except Exception as exc:
            chain_error = exc

        def _need_chain():
            if chain is None:
                raise chain_error
            return chain

        prices = None
        try:
            import yfinance as yf
            prices = yf.Ticker(sym).history(period="1y")
        except Exception:
            pass  # realised vol and post-earnings moves are each optional

        quote = quote_error = None
        try:
            quote = _finviz.fetch_quote(sym)
        except Exception as exc:
            quote_error = exc

        def _need_quote():
            if quote is None:
                raise quote_error
            return quote

        # --- panels ---------------------------------------------------------- #
        # A name with no listed options is still worth analysing on news,
        # chatter and short interest, so a missing chain fails only the two
        # panels that genuinely need it.
        if chain is None and isinstance(chain_error, ticker_positioning.NotFound):
            for key in ("t2_positioning", "t2_vol"):
                out[key] = store.save_panel(
                    key, {"symbol": sym, "not_found": True,
                          "message": f"No listed options found for {sym}."},
                    status="ok")
        else:
            _run_t2(out, "t2_positioning", sym,
                    lambda: ticker_positioning.compute(_need_chain(), sym))
            _run_t2(out, "t2_vol", sym,
                    lambda: vol_sentiment.compute(_need_chain(), sym,
                                                  prices=prices, history=history))

        _run_t2(out, "t2_squeeze", sym,
                lambda: ticker_squeeze.compute(_need_quote(), sym))

        squeeze = (out.get("t2_squeeze") or {}).get("payload") or {}
        company = squeeze.get("company") or (quote or {}).get("company")

        _run_t2(out, "t2_news", sym,
                lambda: ticker_news.refresh(sym, company=company,
                                            finviz_news=(quote or {}).get("news")))
        _run_t2(out, "t2_social", sym,
                lambda: ticker_social.refresh(sym, history=history))

        # EDGAR runs last of the fetching panels: it is the slowest and the
        # only one nothing else depends on.
        security_type = ((chain or {}).get("data") or {}).get("security_type")
        _run_t2(out, "t2_events", sym,
                lambda: ticker_events.refresh(sym, chain_json=chain,
                                              snapshot=squeeze, prices=prices,
                                              security_type=security_type))

        # The composite is a pure function of the six above, read back from the
        # store so it sees stale payloads still cached behind an error badge.
        def _composite():
            panels = {k: store.get_panel(k) for k in _TAB2_KEYS[:-1]}
            return ticker_sentiment.compute(panels, sym, history=history)

        _run_t2(out, "t2_sentiment", sym, _composite)

        _save_t2_snapshot(sym, out)
        return JSONResponse(out)
    finally:
        _TAB2_LOCK.release()


def _save_t2_snapshot(sym: str, out: dict) -> None:
    """Persist one flat daily row so the trend metrics have something to read.

    Deliberately flat scalars only — this feeds sparklines, IV rank and mention
    velocity, none of which want nested structures.
    """
    def payload(key: str) -> dict:
        return ((out.get(key) or {}).get("payload") or {})

    pos, vol = payload("t2_positioning"), payload("t2_vol")
    squeeze, social = payload("t2_squeeze"), payload("t2_social")
    news, comp = payload("t2_news"), payload("t2_sentiment")

    snapshot = {
        "spot": pos.get("spot") or squeeze.get("spot"),
        "net_gex": pos.get("net_gex"),
        "zero_gamma": pos.get("zero_gamma"),
        "iv30": vol.get("iv30"),
        "atm_iv": vol.get("atm_iv"),
        "skew_25d": vol.get("skew_25d"),
        "pcr_oi": vol.get("pcr_oi"),
        "pcr_vol": vol.get("pcr_vol"),
        "rv20": vol.get("rv20"),
        "ivrv_spread": vol.get("ivrv_spread"),
        "max_pain": vol.get("max_pain"),
        "short_float": squeeze.get("short_float"),
        "days_to_cover": squeeze.get("days_to_cover"),
        "rsi": squeeze.get("rsi"),
        "target_upside": squeeze.get("target_upside"),
        "recom": squeeze.get("recom"),
        "st_msgs": social.get("n"),
        "st_bull_pct": social.get("bull_pct"),
        "social_blended": social.get("blended"),
        "news_mean": news.get("mean"),
        "news_n": news.get("count"),
        "composite": comp.get("composite"),
        "confidence": comp.get("confidence"),
    }
    # Never write a row of pure Nones — it would pollute the baselines.
    if any(v is not None for v in snapshot.values()):
        try:
            store.save_snapshot(sym, snapshot)
        except Exception:
            pass  # history is a nice-to-have; never fail a refresh over it


# --------------------------------------------------------------------------- #
# Tab 3 — news screener
# --------------------------------------------------------------------------- #

@app.post("/api/refresh/tab3")
def api_refresh_tab3() -> JSONResponse:
    """Refresh all eight news panels from one fetch.

    Unlike Tabs 1 and 2 there is nothing to sequence: ``news_screener.refresh()``
    makes every request in parallel internally and returns all eight payloads
    together, because they are eight views of one shared pool of headlines
    rather than eight independent panels.

    Failure handling follows Tab 1's ``_run``, not Tab 2's ``_run_t2``: each
    panel's subject is fixed, so yesterday's US Macro headlines are still US
    Macro headlines and a stale payload behind an error badge is the right thing
    to show. Tab 2's symbol guard exists only because its subject changes on
    every press.
    """
    if not _TAB3_LOCK.acquire(blocking=False):
        return JSONResponse(
            {"detail": "A Tab 3 refresh is already running."}, status_code=409)

    out: dict = {}
    try:
        try:
            payloads = news_screener.refresh()
        except Exception as exc:
            # A total fetch failure (no network) must not blank eight panels.
            message = f"{type(exc).__name__}: {exc}"
            for key in _TAB3_KEYS:
                store.update_status(key, "error", message)
                out[key] = store.get_panel(key)
            return JSONResponse(out)

        for key in _TAB3_KEYS:
            _run(out, key, lambda k=key: payloads[k])
        return JSONResponse(out)
    finally:
        _TAB3_LOCK.release()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT)
