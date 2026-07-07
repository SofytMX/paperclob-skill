"""PaperBook MCP server — honest Polymarket paper trading and simulation.

Exposes PaperBook (https://paperclob.com) to MCP clients:

* a live paper-money CLOB wire-compatible with Polymarket's API, and
* the honest simulator (/sim): private replay sessions over REAL recorded
  Polymarket crypto up/down books, with fills graded on the *fillable* lens
  (the price you could actually transact) and a founded verdict.

Design notes
------------
- Credentials (live account + per-sim session creds) are held in this
  process's memory only. They are never logged and never returned to the
  model. Set PAPERCLOB_API_KEY / PAPERCLOB_SECRET / PAPERCLOB_PASSPHRASE to
  reuse an existing live account across restarts.
- API errors are surfaced verbatim — PaperBook's error strings are teaching
  messages (e.g. "order size below PM minimum (5 shares)") and the agent
  should see them unedited.
- The always-current API contract lives at https://paperclob.com/llms-full.txt.
  This server implements it; if the live server ever disagrees, the server wins.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Union

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("PAPERCLOB_BASE_URL", "https://paperclob.com").rstrip("/")

mcp = FastMCP(
    "paperclob",
    instructions=(
        "PaperBook: paper-money Polymarket-compatible CLOB + honest historical "
        "simulator. Typical sim flow: create_sim_session -> advance_clock (the "
        "virtual clock only moves when polled) -> sim_book/sim_markets -> "
        "place_sim_order -> advance_clock(until_end=True) -> get_sim_report. "
        "Interpret the report honestly: the verdict is decided on the "
        "honest_fillable lens, and a green verdict is necessary, not "
        "sufficient, for real edge."
    ),
)

_http = httpx.Client(timeout=30.0, headers={"User-Agent": "paperclob-mcp/0.1"})

# --------------------------------------------------------------------------
# state (in-memory only; secrets never leave this process)
# --------------------------------------------------------------------------


def _env_creds() -> Optional[dict]:
    k, s, p = (
        os.environ.get("PAPERCLOB_API_KEY"),
        os.environ.get("PAPERCLOB_SECRET"),
        os.environ.get("PAPERCLOB_PASSPHRASE"),
    )
    if k and s and p:
        return {"apiKey": k, "secret": s, "passphrase": p}
    return None


_live_creds: Optional[dict] = _env_creds()
_live_handle: Optional[str] = "(from environment)" if _live_creds else None


@dataclass
class SimSession:
    sim_id: str
    base_url: str
    creds: dict
    window: dict
    seed: int
    latency_ms: int
    market_count: int
    seq: int = 0
    vt_ms: Optional[int] = None
    ended: bool = False
    books: dict = field(default_factory=dict)  # market -> latest top-of-book event
    open_markets: set = field(default_factory=set)
    resolutions: dict = field(default_factory=dict)  # market -> resolved_side


_sims: dict[str, SimSession] = {}


# --------------------------------------------------------------------------
# HTTP + Polymarket L2 HMAC signing
# --------------------------------------------------------------------------


def _sign_headers(creds: dict, method: str, path: str, body: str = "") -> dict:
    """Polymarket L2 HMAC, byte-for-byte py-clob-client.

    `path` is the bare endpoint path — no host, NO query string, and for sim
    sessions no `/sim/{id}` prefix (the server strips it before verifying).
    """
    ts = str(int(time.time()))
    msg = ts + method.upper() + path + body
    sig = base64.urlsafe_b64encode(
        hmac.new(
            base64.urlsafe_b64decode(creds["secret"]), msg.encode(), hashlib.sha256
        ).digest()
    ).decode()
    return {
        "POLY_API_KEY": creds["apiKey"],
        "POLY_SIGNATURE": sig,
        "POLY_TIMESTAMP": ts,
        "POLY_PASSPHRASE": creds["passphrase"],
    }


def _request(
    method: str,
    url: str,
    *,
    sign_path: Optional[str] = None,
    creds: Optional[dict] = None,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
    retry_5xx: int = 0,
    timeout: Optional[float] = None,
) -> Any:
    """One API call. Honors 429 Retry-After; surfaces error bodies verbatim."""
    body = ""
    content = None
    headers: dict = {}
    if json_body is not None:
        body = json.dumps(json_body, separators=(",", ":"))
        content = body.encode()
        headers["Content-Type"] = "application/json"
    attempts_5xx = 0
    for attempt in range(4):
        if sign_path is not None:
            headers.update(_sign_headers(creds, method, sign_path, body))
        resp = _http.request(
            method, url, params=params, content=content, headers=headers,
            timeout=timeout if timeout is not None else _http.timeout,
        )
        if resp.status_code == 429 and attempt < 3:
            time.sleep(min(float(resp.headers.get("Retry-After", "1") or 1), 15))
            continue
        if resp.status_code >= 500 and attempts_5xx < retry_5xx:
            attempts_5xx += 1
            time.sleep(2.0 * attempts_5xx)
            continue
        break
    if resp.status_code >= 400:
        # PaperBook error strings are teaching messages — pass them through
        # verbatim (truncated only if a proxy returned a full HTML page).
        text = resp.text
        if len(text) > 600:
            text = text[:600] + " …[truncated]"
        raise RuntimeError(f"PaperBook API error {resp.status_code}: {text}")
    return resp.json()


def _get_sim(sim_id: str) -> SimSession:
    sim = _sims.get(sim_id)
    if sim is None:
        raise RuntimeError(
            f"unknown sim_id {sim_id!r} in this MCP process. Create one with "
            "create_sim_session (note: server-side sessions also expire after "
            "30 minutes idle; the verdict is retained server-side)."
        )
    return sim


def _to_ms(value: Union[int, str]) -> int:
    """Accept unix milliseconds, or 'YYYY-MM-DD' (UTC midnight) / ISO 8601."""
    if isinstance(value, int):
        return value
    v = str(value).strip()
    if v.isdigit():
        return int(v)
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------
# public market data (live venue)
# --------------------------------------------------------------------------


@mcp.tool()
def list_markets() -> list:
    """List active markets on the LIVE PaperBook paper CLOB (no auth).

    Markets are crypto up/down binaries — "will the coin close above its
    open?" — for BTC/ETH/SOL/XRP/BNB/DOGE in rolling 5m and 15m windows. Each
    row: {id, coin, window_s, expires_at (unix s), up_prob, best_bid,
    best_ask}. Market ids look like "BTC-5-1783090800".
    """
    return _request("GET", f"{BASE_URL}/markets")


@mcp.tool()
def get_leaderboard() -> list:
    """PaperBook's paper-money PnL leaderboard of registered bots (no auth).

    Rows: {rank, handle, pnl, volume, trades}. Paper money only — a positive
    PnL here is a live paper record, not proof of real-market edge.
    """
    return _request("GET", f"{BASE_URL}/leaderboard")


# --------------------------------------------------------------------------
# live paper CLOB account
# --------------------------------------------------------------------------


@mcp.tool()
def register_live_account(handle: str) -> dict:
    """Register a live PaperBook account (grants $10,000 paper money).

    The account's API credentials are held in this MCP process's memory and
    are never shown to you or logged. They are lost when the process exits —
    to reuse an account across restarts, the operator can set
    PAPERCLOB_API_KEY / PAPERCLOB_SECRET / PAPERCLOB_PASSPHRASE in the MCP
    server environment. Registering again under the same handle rotates the
    credentials server-side.
    """
    global _live_creds, _live_handle
    out = _request("POST", f"{BASE_URL}/register", json_body={"handle": handle})
    _live_creds = {
        "apiKey": out["apiKey"],
        "secret": out["secret"],
        "passphrase": out["passphrase"],
    }
    _live_handle = out.get("handle", handle)
    return {
        "handle": _live_handle,
        "account_id": out.get("account_id"),
        "balance": out.get("balance"),
        "credentials": "held in MCP process memory (never logged, never returned)",
    }


def _require_live() -> dict:
    if _live_creds is None:
        raise RuntimeError(
            "no live account in this MCP process — call register_live_account "
            "first (or set PAPERCLOB_API_KEY/SECRET/PASSPHRASE in the server env)."
        )
    return _live_creds


@mcp.tool()
def place_live_order(
    market: str,
    outcome: str,
    side: str,
    size: float,
    order_type: str = "market",
    price: Optional[float] = None,
) -> dict:
    """Place an order on the LIVE paper CLOB (requires a registered account).

    Args:
      market: market id from list_markets, e.g. "BTC-5-1783090800".
      outcome: "yes" or "no" (in a binary market, buying NO is how you express
        a bearish view).
      side: "BUY" or "SELL" (SELL requires held shares — no shorting).
      size: shares. Follow PM conventions: size >= 5 and marketable >= $1.
      order_type: "market" (taker, crosses the book at arrival) or "limit"
        (needs price; a non-marketable limit RESTS on the shared book and
        reserves cash — the response then carries a "resting" order id).
      price: limit price on the $0.01 grid in (0,1); required for limits.

    Returns the API response verbatim: order status/fill/fee, resting id if
    any, and the new balance. Taker fee mirrors PM crypto:
    0.07 * price * (1 - price) per share; makers pay 0.
    """
    creds = _require_live()
    payload: dict = {
        "market": market,
        "outcome": outcome,
        "side": side,
        "type": order_type,
        "size": size,
    }
    if price is not None:
        payload["price"] = price
    return _request(
        "POST", f"{BASE_URL}/order", sign_path="/order", creds=creds, json_body=payload
    )


@mcp.tool()
def live_positions() -> dict:
    """Live account snapshot: /me (balance, pnl), open positions, and orders.

    Positions rows: {market, side: "yes"|"no", shares, avg_price}. Orders:
    {fills: [...], resting: [...]}. Requires a registered account.
    """
    creds = _require_live()
    return {
        "me": _request("GET", f"{BASE_URL}/me", sign_path="/me", creds=creds),
        "positions": _request(
            "GET", f"{BASE_URL}/positions", sign_path="/positions", creds=creds
        ),
        "orders": _request(
            "GET", f"{BASE_URL}/orders", sign_path="/orders", creds=creds
        ),
    }


# --------------------------------------------------------------------------
# the honest simulator (/sim)
# --------------------------------------------------------------------------


@mcp.tool()
def create_sim_session(
    t0: Union[int, str],
    t1: Union[int, str],
    coins: Optional[list[str]] = None,
    durations: Optional[list[str]] = None,
    latency_ms: int = 330,
    seed: int = 42,
) -> dict:
    """Create a PaperBook simulator session: a private replay over REAL
    recorded Polymarket crypto up/down books, with honest fills.

    This is the core differentiator vs every "paper trading at live prices"
    sim: your taker orders cross the book that actually existed, at
    submit + latency_ms virtual milliseconds — so adverse selection is real,
    not simulated away. At the end, get_sim_report grades the fills on the
    fillable lens and returns a founded verdict.

    Args:
      t0, t1: window bounds — unix MILLISECONDS or "YYYY-MM-DD" (UTC
        midnight). Corpus floor is 2026-05-29; ceiling roughly yesterday;
        span <= 90 days. One UTC day (t1 = t0 + 86400000) is typical.
      coins: subset of [btc, eth, sol, xrp, bnb, doge] (default btc, eth).
      durations: subset of ["5m", "15m"] (default both).
      latency_ms: virtual submit->arrival latency (default 330, the measured
        real-world figure). Lowering it makes the sim less honest.
      seed: reproducibility seed (default 42). Same window + seed + latency +
        ordered order intents + corpus manifest => same reproducibility_hash.

    Session credentials are held in this MCP process; sessions expire after
    30 minutes idle (any authed call keeps them alive) but the verdict is
    retained server-side. The virtual clock starts FROZEN at t0 — nothing
    happens until you call advance_clock.
    """
    payload: dict = {
        "t0": _to_ms(t0),
        "t1": _to_ms(t1),
        "latency_ms": latency_ms,
        "seed": seed,
    }
    if coins:
        payload["coins"] = coins
    if durations:
        payload["durations"] = durations
    # Session creation loads a slice of the recorded corpus server-side; it can
    # be slow and occasionally times out at the proxy — retry a couple times.
    out = _request(
        "POST", f"{BASE_URL}/sim", json_body=payload, retry_5xx=2, timeout=120.0
    )
    sim = SimSession(
        sim_id=out["sim_id"],
        base_url=out["base_url"].rstrip("/"),
        creds=out["creds"],
        window=out.get("window", {}),
        seed=out.get("seed", seed),
        latency_ms=out.get("latency_ms", latency_ms),
        market_count=out.get("market_count", 0),
    )
    _sims[sim.sim_id] = sim
    return {
        "sim_id": sim.sim_id,
        "window": sim.window,
        "window_iso": [_iso(sim.window.get("t0")), _iso(sim.window.get("t1"))],
        "market_count": sim.market_count,
        "latency_ms": sim.latency_ms,
        "seed": sim.seed,
        "balance": out.get("balance"),
        "clock_mode": out.get("clock_mode"),
        "corpus_manifest_version": out.get("corpus_manifest_version"),
        "credentials": "held in MCP process memory",
        "next": "call advance_clock(sim_id) — the clock is frozen at t0 until polled",
    }


@mcp.tool()
def advance_clock(
    sim_id: str,
    polls: int = 25,
    market: Optional[str] = None,
    until_end: bool = False,
    max_seconds: int = 25,
) -> dict:
    """Advance the sim's stepping virtual clock by long-polling /events.

    The session's clock only moves when polled — every call to this tool
    consumes virtual time, and every read (books, markets, prices) is served
    at virtual now, so lookahead is impossible. Event kinds: slot_open, book
    (top of book), slot_close, resolution, plus your own order_ack /
    order_fill / order_reject.

    Args:
      sim_id: from create_sim_session.
      polls: how many /events polls to make this call (each returns a batch).
      market: optional market id filter, e.g. "BTC-5-1781913600" (your own
        order events always come through).
      until_end: keep polling until the window is exhausted (bounded by
        max_seconds; call again if end=False).
      max_seconds: wall-clock budget for this call (capped at 240).

    Returns a summary: virtual time, event counts by kind, currently open
    markets, resolutions seen, your own order events verbatim, and end flag.
    Top-of-book snapshots are cached — read them with sim_book.
    """
    sim = _get_sim(sim_id)
    deadline = time.monotonic() + min(max(int(max_seconds), 1), 240)
    counts: dict[str, int] = {}
    own_order_events: list = []
    new_resolutions: dict = {}
    polls_done = 0
    params_market = {"market": market} if market else {}

    while polls_done < (10**9 if until_end else max(int(polls), 1)):
        if sim.ended or time.monotonic() > deadline:
            break
        out = _request(
            "GET",
            f"{sim.base_url}/events",
            sign_path="/events",
            creds=sim.creds,
            params={"after": sim.seq, "wait": 1, **params_market},
        )
        polls_done += 1
        for ev in out.get("events", []):
            sim.seq = max(sim.seq, ev.get("seq", sim.seq))
            kind = ev.get("kind") or ev.get("type") or "?"
            counts[kind] = counts.get(kind, 0) + 1
            mkt = ev.get("market")
            if kind == "slot_open" and mkt:
                sim.open_markets.add(mkt)
            elif kind == "slot_close" and mkt:
                sim.open_markets.discard(mkt)
                sim.books.pop(mkt, None)
            elif kind == "book" and mkt:
                sim.books[mkt] = ev
            elif kind == "resolution" and mkt:
                sim.resolutions[mkt] = ev.get("resolved_side")
                new_resolutions[mkt] = ev.get("resolved_side")
                sim.open_markets.discard(mkt)
            elif kind.startswith("order_"):
                own_order_events.append(ev)
        if out.get("vt") is not None:
            sim.vt_ms = out["vt"]
        if out.get("end"):
            sim.ended = True
            break
        time.sleep(0.06)  # stay under the 20 req/s sustained read limit

    return {
        "virtual_time_ms": sim.vt_ms,
        "virtual_time_iso": _iso(sim.vt_ms),
        "end": sim.ended,
        "polls_done": polls_done,
        "event_counts": counts,
        "open_markets": sorted(sim.open_markets),
        "new_resolutions": new_resolutions,
        "own_order_events": own_order_events,
        "hint": None
        if sim.ended
        else "end=False: call advance_clock again to keep replaying",
    }


@mcp.tool()
def get_time(sim_id: str) -> dict:
    """Read the sim's virtual clock (unix seconds at virtual now).

    The clock is frozen until advance_clock is called; a full recorded day
    replays as fast as you poll it.
    """
    sim = _get_sim(sim_id)
    vt = _request("GET", f"{sim.base_url}/time")
    secs = vt.get("time") if isinstance(vt, dict) else vt
    try:
        iso = datetime.fromtimestamp(float(secs), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        iso = None
    return {"virtual_time": vt, "virtual_time_iso": iso, "ended": sim.ended}


@mcp.tool()
def sim_markets(sim_id: str) -> list:
    """List slots active AT VIRTUAL TIME in the sim session (no lookahead).

    Rows: {id, coin, window_s, expires_at, up_prob, best_bid, best_ask}. May
    be empty at exactly t0 — advance_clock once first.
    """
    sim = _get_sim(sim_id)
    return _request("GET", f"{sim.base_url}/markets")


@mcp.tool()
def sim_book(sim_id: str, market: Optional[str] = None) -> dict:
    """Top-of-book at virtual time for sim markets, from the recorded tape.

    Returns the latest cached `book` event per market ({yes_bid, yes_ask,
    no_bid, no_ask, secs_to_close, vt}) — the cache fills as advance_clock
    consumes events. Pass `market` for a single market. Empty cache means the
    clock hasn't reached a book snapshot yet: advance_clock first. Remember:
    your order will cross the book as of ARRIVAL (submit + latency_ms), not
    the snapshot you are looking at — that difference is the honest part.
    """
    sim = _get_sim(sim_id)
    if market is not None:
        book = sim.books.get(market)
        if book is None:
            return {
                "market": market,
                "book": None,
                "hint": "no book snapshot cached yet for this market — advance_clock",
            }
        return {"market": market, "book": book}
    if not sim.books:
        return {"books": {}, "hint": "no book events consumed yet — advance_clock"}
    return {"books": sim.books, "open_markets": sorted(sim.open_markets)}


@mcp.tool()
def place_sim_order(
    sim_id: str,
    market: str,
    outcome: str,
    side: str,
    size: float,
    order_type: str = "market",
    price: Optional[float] = None,
) -> dict:
    """Place a taker order inside the sim (honest fills against the tape).

    The order lands at virtual submit + latency_ms and crosses the recorded
    book AS OF ARRIVAL. Minimum size 5 shares (PM convention; the server
    rejects less with a 400). No shorting: SELL requires held shares. A
    marketable order that finds no liquidity at arrival returns
    status="unmatched" — that is not an error, and the miss still counts as a
    decision in the re-grade (you cannot cherry-pick).

    Args:
      sim_id: from create_sim_session.
      market: sim market id, e.g. "BTC-5-1781913600".
      outcome: "yes" or "no" (buy NO to express a bearish view — taker BUYs
        are the graded path).
      side: "BUY" or "SELL".
      size: shares (>= 5).
      order_type: "market", or "limit" with `price` (taker-only phase:
        limits execute as marketable/IOC; nothing rests).
      price: limit price on the $0.01 grid in (0,1).

    Returns the PM-shaped response verbatim (status matched/unmatched, fill
    price, fee, balance). Fee = 0.07 * p * (1-p) per share, as on PM crypto.
    """
    sim = _get_sim(sim_id)
    payload: dict = {
        "market": market,
        "outcome": outcome,
        "side": side,
        "type": order_type,
        "size": size,
    }
    if price is not None:
        payload["price"] = price
    return _request(
        "POST",
        f"{sim.base_url}/order",
        sign_path="/order",
        creds=sim.creds,
        json_body=payload,
    )


@mcp.tool()
def sim_positions(sim_id: str) -> dict:
    """Current sim positions and fill history.

    Positions: [{market, side, shares, avg_price}]. Orders: {fills: [...]}.
    Positions auto-settle at each slot's resolution (watch for `resolution`
    events from advance_clock).
    """
    sim = _get_sim(sim_id)
    return {
        "positions": _request(
            "GET", f"{sim.base_url}/positions", sign_path="/positions", creds=sim.creds
        ),
        "orders": _request(
            "GET", f"{sim.base_url}/orders", sign_path="/orders", creds=sim.creds
        ),
    }


@mcp.tool()
def get_sim_report(sim_id: str) -> dict:
    """Fetch the sim's honest verdict — returned VERBATIM from the server.

    The report grades your taker BUY fills with the same harness PaperBook
    ran against its own (now dead) strategies. Read it in this order:

    1. `verdict` — decided on the honest_fillable lens:
       - real_edge: fillable ci_lo > 0 at n >= 40. Rare — the market didn't
         already price your signal.
       - phantom: the paper lens looks positive but fillable <= 0. The gap is
         adverse selection: the fills you'd actually get are the losers.
       - no_edge: honest edge <= 0 at scale. The market priced it.
       - inconclusive: fewer than 40 gradeable fills — run longer or widen
         coins/durations before concluding anything.
    2. The three lenses, each {n, wr, avg_px, edge_real, ci_lo, ci_hi} with
       edge_real = realized win rate - avg price paid ($-weighted):
       `paper` (the book you saw — flattering, blind to adverse selection),
       `honest_persist` (quote persistence), `honest_fillable` (THE lens —
       the price you could actually transact).
    3. `ghost_gap` — how much of the paper edge evaporates under honest fills.
    4. `certificate` — data-quality disclosure: corpus coverage, tape holes
       inside your window, masked/flagged biases, phantom-quote rate. The
       verdict is founded on disclosed data, not a black box.
    5. `reproducibility_hash` — same window + seed + latency + ordered order
       intents + corpus manifest => same hash. Quote it when sharing results.

    Ethos: a green verdict is necessary, not sufficient, for real edge —
    live fills self-select further. Never report the paper lens as "the
    result"; if paper and fillable disagree, fillable is the result.
    """
    sim = _get_sim(sim_id)
    return _request(
        "GET", f"{sim.base_url}/report", sign_path="/report", creds=sim.creds
    )


def main() -> None:
    """Entry point: run the MCP server on stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
