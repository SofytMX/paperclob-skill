# PaperBook for AI agents — the honest Polymarket paper-trading sim

**Every other sim tells you what you'd make at the prices you see. This one tells you what would actually fill.**

[PaperBook](https://paperclob.com) grades your Polymarket-style strategy on **recorded real tape**: your taker orders cross the book that actually existed, after measured latency (330 ms by default), and the report scores your fills as `edge_real = realized win rate − avg price paid` on the **fillable lens** — the price you could actually transact, not the one you were looking at. The output is a founded verdict:

| verdict | meaning |
|---|---|
| `real_edge` | fillable `ci_lo > 0` at `n ≥ 40` — the market hadn't priced your signal. Rare. |
| `phantom` | positive on paper, ≤ 0 fillable — the gap is adverse selection; the fills you'd get are the losers |
| `no_edge` | honest edge ≤ 0 at scale — the market priced it |
| `inconclusive` | fewer than 40 gradeable fills — run longer before concluding anything |

Every report ships a **data-quality certificate** (corpus coverage, tape holes in your window, disclosed biases, phantom-quote rate) and a **reproducibility hash** (same window + seed + latency + order intents + corpus manifest ⇒ same hash). The verdict is founded, not vibes.

We falsified fifteen of our own strategies with this harness before pointing it at anyone else's. The post-mortems — each with the exact statistical reason the strategy died — are public: **[the strategy graveyard](https://paperclob.com/blog)**.

There's also a **live paper CLOB**: registering grants $10,000 of paper money on crypto up/down binaries (BTC/ETH/SOL/XRP/BNB/DOGE × 5m/15m), wire-compatible with Polymarket's CLOB API — same `POLY_*` headers, same L2 HMAC, same signed-order envelope. `py-clob-client` works by swapping the base URL.

## What's in this repo

```
skills/polymarket-honest-sim/   Claude Code skill (SKILL.md + reference.md)
mcp/                            MCP server (Python) — pip install paperclob-mcp
```

## Install — Claude Code skill

Copy the skill into your skills directory (project-level `.claude/skills/` or user-level `~/.claude/skills/`):

```sh
git clone https://github.com/PLACEHOLDER/paperclob-skill
cp -r paperclob-skill/skills/polymarket-honest-sim ~/.claude/skills/
```

Once published to the skill marketplaces:

```sh
npx clawhub@latest install polymarket-honest-sim   # placeholder — pending publication
```

The skill triggers when you ask Claude to build or test a Polymarket bot, paper trade, or backtest a prediction-market strategy. It teaches the workflow and the honest interpretation of the verdict, and instructs the agent to fetch the always-current API contract from [`llms-full.txt`](https://paperclob.com/llms-full.txt) — so it never drifts from the server.

## Install — MCP server

```sh
pip install paperclob-mcp
```

Claude Code:

```sh
claude mcp add paperclob -- paperclob-mcp
```

Claude Desktop / any MCP client (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "paperclob": { "command": "paperclob-mcp" }
  }
}
```

Tools: `create_sim_session`, `advance_clock` (the sim's stepping virtual clock — nothing moves until polled, so lookahead is impossible), `sim_markets`, `sim_book`, `place_sim_order`, `sim_positions`, `get_time`, `get_sim_report` (verdict + lenses + certificate, verbatim), plus `register_live_account`, `place_live_order`, `live_positions`, `list_markets`, `get_leaderboard`. Credentials stay inside the MCP process — never logged, never shown to the model. See [`mcp/README.md`](mcp/README.md).

## Zero-install path

Any agent that can fetch a URL can integrate directly — no skill, no MCP:

```
https://paperclob.com/llms-full.txt
```

That file is the complete, paste-ready contract (auth code, endpoints, order shapes, rate limits) with a validation checklist, generated from the same source as the in-app "Copy for AI" briefs.

## Why this exists

A paper-trading sim that fills at observed prices measures `corr(signal, price)`, not edge. Live, fills self-select toward losers — the quotes you can actually hit are the ones informed flow left behind. Measured on our own strategies, rejected orders would have won 6–19pp more often than the orders that filled. A backtest that can't see this will bless almost anything. PaperBook's sim is built so it can't not see it.

One line of ethos, printed on everything: **a green verdict is necessary, not sufficient, for real edge.**

## Disclaimers

- Paper money only. No real funds, no custody, no payouts.
- Not affiliated with Polymarket. Wire compatibility is for developer convenience.
- Nothing here is financial advice; a sim verdict is a statement about recorded data, not future markets.
