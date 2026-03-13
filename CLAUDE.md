# CLAUDE.md — Polymarket Trading Bot

## Project Overview
Autonomous trading bot for Polymarket prediction markets. Uses Claude (Haiku screening + Opus deep analysis) to find mispricings, sizes bets via Kelly criterion, and executes on Polymarket's CLOB API.

## Key Commands
```bash
# Local
python -m polymarket_bot          # Live trading
python -m scripts.dry_run         # Dry run
python -m scripts.status          # CLI dashboard

# Deploy to VPS
ssh trading-bot "cd /root/polymarket-bot && git pull && systemctl restart trading-bot"

# VPS logs
ssh trading-bot "journalctl -u trading-bot -f"

# VPS status
ssh trading-bot "cd /root/polymarket-bot && /root/venv/bin/python -m scripts.status"

# Check on-chain positions
ssh trading-bot 'curl -s "https://data-api.polymarket.com/positions?user=$WALLET_ADDRESS"'
```

## VPS Details
- **SSH alias**: `ssh trading-bot` (Digital Ocean)
- **Repo path**: `/root/polymarket-bot/`
- **Venv**: `/root/venv/`
- **Systemd service**: `trading-bot.service` (WorkingDirectory=/root/polymarket-bot)
- **DB path**: `/root/polymarket-bot/data/bot.db`
- **Old DB (pre-git)**: `/root/data/bot.db` (migrated Feb 28 2026)

## Architecture
```
polymarket_bot/
  bot.py        — Main loop, resolution checker, exit manager
  analyst.py    — Two-tier LLM analysis (Haiku batch screen + Opus deep)
  scanner.py    — Market discovery via Gamma API with entropy scoring
  executor.py   — Kelly sizing, order execution, on-chain redemption
  portfolio.py  — Position tracking, balance sync, P&L
  risk.py       — Circuit breakers, drawdown limits, correlation guard
  config.py     — Environment-driven config (from .env)
  db.py         — SQLite persistence (trades, analyses, snapshots)
```

## Critical Discoveries

### Neg-Risk Redemption (Feb 2026)
Polymarket neg-risk markets require special handling for on-chain token redemption:
- **NegRiskAdapter** (`0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`) `redeemPositions(conditionId, amounts)`
- The `amounts` array MUST be length 2: `[yes_token_balance, no_token_balance]`
- Must query actual CTF token balances first — do NOT pass `2**128` or arbitrary max values
- Token IDs come from Gamma API field `clobTokenIds` (index 0 = YES, index 1 = NO)
- CTF contract (`0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`) holds the ERC-1155 tokens
- Gas: 500k limit with 1.2x gas price multiplier, 120s timeout
- Using CTF `redeemPositions` directly with `parentCollectionId=0x00` does NOT work for neg-risk

### Kelly Sizing
- Non-linear confidence scaling: `((confidence - 4) / 6.0) ** 1.5`
- Edge certainty dampening: `min(edge / 0.20, 1.0)`
- Much more conservative on weak signals (conf=5/edge=6% -> ~5x smaller bet)

### Scanner Entropy Scoring
- Shannon entropy normalized to [0,1] for binary markets (1.0 = 50/50)
- Composite score: 40% entropy + 40% volume + 20% expiry proximity
- Prefers mid-uncertainty markets where information edge is most valuable

### Correlation Guard
- Extracts keywords from question text (3+ char words minus stopwords)
- Blocks new trades when 2+ existing positions share >= 40% keyword overlap
- Prevents concentrated bets on correlated markets (e.g., multiple BTC range bets)

## Polymarket API Notes
- **Gamma API**: `https://gamma-api.polymarket.com` — market data, prices, metadata
- **Data API**: `https://data-api.polymarket.com` — wallet positions, PnL
- USDC.e on Polygon: `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` (6 decimals)
- Wallet address is in `.env` (EOA, not proxy)
