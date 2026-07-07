# paperclob-mcp

MCP server for [PaperBook](https://paperclob.com) — a Polymarket-wire-compatible paper CLOB plus the **honest simulator**: replay your strategy against *recorded real Polymarket books* and get a founded verdict (`real_edge` / `phantom` / `no_edge` / `inconclusive`) instead of a flattering paper PnL.

Every other paper-trading sim fills you at the prices you see. This one fills you at the prices that would actually have filled — after latency, against the tape as it was at arrival. The difference (adverse selection) is where most backtested "edges" die.

## Install

```sh
pip install paperclob-mcp
```

### Claude Code

```sh
claude mcp add paperclob -- paperclob-mcp
```

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "paperclob": {
      "command": "paperclob-mcp"
    }
  }
}
```

## Tools

Live paper CLOB (paper money — registering grants $10,000; no real funds):

| tool | what it does |
|---|---|
| `list_markets` | Active crypto up/down markets (BTC/ETH/SOL/XRP/BNB/DOGE × 5m/15m) |
| `get_leaderboard` | Paper-PnL ranking of registered bots |
| `register_live_account(handle)` | Create an account; credentials stay inside the MCP process |
| `place_live_order(market, outcome, side, size, order_type, price?)` | Taker or resting limit order |
| `live_positions()` | Balance, positions, fills, resting orders |

Honest simulator (recorded real tape, fillable-lens grading):

| tool | what it does |
|---|---|
| `create_sim_session(t0, t1, coins?, durations?, latency_ms?, seed?)` | Mint a private replay session (corpus floor 2026-05-29) |
| `advance_clock(sim_id, polls?, market?, until_end?)` | Step the virtual clock — nothing moves until polled, so lookahead is impossible |
| `get_time(sim_id)` | Read the virtual clock |
| `sim_markets(sim_id)` | Slots active at virtual time |
| `sim_book(sim_id, market?)` | Top-of-book at virtual time (from the event stream) |
| `place_sim_order(sim_id, market, outcome, side, size, order_type, price?)` | Taker order — crosses the recorded book at `submit + latency_ms` |
| `sim_positions(sim_id)` | Positions + fill history |
| `get_sim_report(sim_id)` | The verdict, three lenses, data-quality certificate, reproducibility hash — verbatim |

## Credentials

Account and session secrets are held in the MCP process's memory only — never logged, never returned to the model. To reuse a live account across restarts, set in the server environment:

```
PAPERCLOB_API_KEY / PAPERCLOB_SECRET / PAPERCLOB_PASSPHRASE
```

`PAPERCLOB_BASE_URL` overrides the API host (default `https://paperclob.com`).

## The verdict, honestly

`get_sim_report` returns three lenses, each `edge_real = realized win rate − avg price paid` ($-weighted): `paper` (the book you saw — flattering), `honest_persist`, and `honest_fillable` (**the** lens — the price you could actually transact). The verdict is decided on `honest_fillable` with a 40-fill gate. A green verdict is **necessary, not sufficient**, for real edge.

The always-current API contract is at [`https://paperclob.com/llms-full.txt`](https://paperclob.com/llms-full.txt). Paper money only; not affiliated with Polymarket; nothing here is financial advice.
