# Polymarket AI Trading Bot

Autonomous trading bot for [Polymarket](https://polymarket.com) prediction markets. Uses Claude to analyze markets, estimate true probabilities, and place bets when it finds mispricings.

> **Warning**: This bot trades real money. Use at your own risk. Past performance does not guarantee future results.

## How It Works

1. **Scan** — Fetches top markets from Polymarket's Gamma API, filtered by volume, liquidity, and expiry
2. **Pre-screen** — Claude Haiku batch-screens ~30 markets in one cheap API call, flagging up to 5 worth analyzing
3. **Analyze** — Claude Opus deep-analyzes each flagged market, estimating true probability via structured tool output
4. **Size** — Kelly criterion with fractional scaling and confidence weighting determines bet size
5. **Execute** — Places GTC limit orders via Polymarket's CLOB API
6. **Manage** — Monitors positions for stop-loss (-30%) and take-profit (+50%), tracks drawdown with circuit breakers
7. **Redeem** — Automatically redeems winning conditional tokens back to USDC.e on-chain
8. **Learn** — Feeds past win/loss history back into the analysis prompt for calibration

## Architecture

```
polymarket_bot/
├── bot.py          # Main trading loop and resolution checker
├── analyst.py      # Two-tier LLM analysis (Haiku screening + Opus deep analysis)
├── scanner.py      # Market discovery via Gamma API
├── executor.py     # Order execution, Kelly sizing, on-chain redemption
├── portfolio.py    # Position tracking, balance sync, P&L
├── risk.py         # Circuit breakers, drawdown limits, exposure caps
├── config.py       # Environment-driven configuration
└── db.py           # SQLite persistence (trades, analyses, snapshots)

scripts/
├── status.py       # CLI dashboard for trade results and portfolio status
└── dry_run.py      # Full pipeline without real orders
```

## Setup

### Prerequisites

- Python 3.11+
- A Polygon wallet funded with USDC.e (bridged USDC) and POL/MATIC for gas
- [Anthropic API key](https://console.anthropic.com)
- Polymarket approvals (the bot's CLOB client handles this)

### Install

```bash
git clone https://github.com/thryec/polymarket-bot.git
cd polymarket-bot
pip install -e .
```

### Configure

```bash
cp .env.example .env
# Edit .env with your keys
```

### Run

```bash
# Dry run (no real orders)
python -m scripts.dry_run

# Live trading
python -m polymarket_bot

# Check status
python -m scripts.status
```

### Deploy (VPS)

The bot is designed to run 24/7 on a non-US VPS (Polymarket blocks US IPs).

```bash
# Example: systemd service
sudo cp trading-bot.service /etc/systemd/system/
sudo systemctl enable trading-bot
sudo systemctl start trading-bot

# View logs
journalctl -u trading-bot -f
```

## Configuration

All configuration via environment variables (see `.env.example`):

| Variable | Description | Default |
|---|---|---|
| `PRIVATE_KEY` | Polygon wallet private key | Required |
| `WALLET_ADDRESS` | Wallet address | Required |
| `ANTHROPIC_API_KEY` | Claude API key | Required |
| `POLYGON_RPC_URL` | Polygon RPC endpoint | `https://polygon-bor-rpc.publicnode.com` |
| `SCAN_INTERVAL` | Seconds between scan cycles | `300` |
| `MIN_EDGE` | Minimum edge to trade | `0.05` (5%) |
| `KELLY_FRACTION` | Kelly criterion fraction | `0.6` |
| `MAX_BET_USDC` | Maximum bet size | `50` |
| `MAX_DRAWDOWN_PCT` | Halt trading at this drawdown | `0.35` (35%) |
| `DRY_RUN` | Skip real order execution | `false` |

## Risk Management

- **Position limits**: Max 20% of bankroll per position, max 85% total exposure
- **Bet sizing**: Fractional Kelly criterion scaled by confidence
- **Circuit breaker**: 10% drawdown halves bets, 20% pauses 30min, 35% halts trading
- **Daily loss limit**: 15% of bankroll
- **Stop-loss**: -30% per position
- **Take-profit**: +50% per position

## Cost Optimization

The two-tier screening reduces API costs ~6x compared to analyzing every market with Opus:

- **Haiku pre-screen**: ~$0.001 per batch of 30 markets
- **Opus deep analysis**: ~$0.15 per market, only called for ~5 pre-screened candidates
- **Analysis cache**: 30-minute TTL prevents re-analyzing the same markets

## License

MIT
