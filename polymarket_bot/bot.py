"""Polymarket AI Trading Bot — Main Loop"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone

import httpx

from .analyst import analyze_market, pre_screen_markets
from .config import Config
from .db import get_conn
from .executor import calculate_bet, execute_trade, redeem_positions, sell_position
from .portfolio import Portfolio
from .risk import RiskManager
from .scanner import scan_markets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")


async def run(config: Config) -> None:
    """Main trading loop."""
    log.info("=" * 60)
    log.info("Polymarket AI Trading Bot starting")
    log.info(f"  Dry run: {config.dry_run}")
    log.info(f"  Max bet: ${config.max_bet_usdc}")
    log.info(f"  Min edge: {config.min_edge:.0%}")
    log.info(f"  Kelly fraction: {config.kelly_fraction}")
    log.info(f"  Max drawdown: {config.max_drawdown_pct:.0%}")
    log.info(f"  Scan interval: {config.scan_interval}s")
    log.info("=" * 60)

    conn = get_conn(config.db_path)
    portfolio = Portfolio(config)
    risk = RiskManager(config=config)

    cycle = 0
    while True:
        cycle += 1
        log.info(f"\n--- Cycle {cycle} @ {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} ---")

        try:
            # 1. Sync portfolio state
            await portfolio.sync_balance()
            await portfolio.sync_prices()
            risk.update_drawdown(portfolio)

            if risk.is_halted():
                log.warning("Bot is HALTED — skipping cycle")
                await asyncio.sleep(config.scan_interval)
                continue

            # 2. Scan markets
            markets = await scan_markets()
            log.info(f"Found {len(markets)} candidate markets")

            # 3. Pre-screen with Haiku, then deep-analyze with Opus
            existing_market_ids = {p.market_id for p in portfolio.positions.values()}
            markets = [m for m in markets if m.get("id") not in existing_market_ids]
            shortlist = await pre_screen_markets(markets, config)
            log.info(f"Opus deep-analyzing {len(shortlist)} markets")

            # 4. Analyze and trade
            trades_this_cycle = 0
            for market in shortlist:
                signal = await analyze_market(market, config)
                if not signal:
                    continue

                if not risk.pre_trade_ok(signal, portfolio):
                    continue

                bankroll = portfolio.bankroll()
                exposure = portfolio.exposure()
                bet = calculate_bet(signal, bankroll, exposure, config)
                bet = risk.scale_bet(bet)

                if bet > 0:
                    result = await execute_trade(signal, bet, config)
                    portfolio.record_trade(result, signal)
                    trades_this_cycle += 1

                    if trades_this_cycle >= 3:
                        log.info("Max 3 trades per cycle — moving on")
                        break

            # 5. Check resolved markets and redeem winnings
            await check_resolutions(config)

            # 6. Check exits
            await check_exits(portfolio, config, risk)

            # 7. Log status
            log_status(portfolio, risk, cycle)

            # 8. Save snapshot
            portfolio.snapshot()

        except Exception as e:
            log.error(f"Cycle {cycle} error: {e}", exc_info=True)

        await asyncio.sleep(config.scan_interval)


async def check_resolutions(config: Config) -> None:
    """Check if any traded markets have resolved and record win/loss."""
    conn = get_conn(config.db_path)

    unresolved = conn.execute(
        "SELECT DISTINCT market_id, question, side, price, size_usdc FROM trades WHERE result IS NULL"
    ).fetchall()
    if not unresolved:
        return

    market_ids = {row[0] for row in unresolved}

    async with httpx.AsyncClient(timeout=15) as client:
        for mid in market_ids:
            try:
                resp = await client.get(f"https://gamma-api.polymarket.com/markets/{mid}")
                if resp.status_code != 200:
                    continue
                market = resp.json()
            except Exception:
                continue

            if not market.get("closed"):
                continue

            outcome_prices = market.get("outcomePrices")
            outcomes = market.get("outcomes")
            resolved_yes = False
            if outcome_prices and outcomes:
                try:
                    prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                    names = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                    for name, price in zip(names, prices):
                        if name in ("Yes", "yes") and float(price) >= 0.99:
                            resolved_yes = True
                        elif name in ("No", "no") and float(price) >= 0.99:
                            resolved_yes = False
                except (ValueError, TypeError):
                    continue

            has_win = False
            for row in unresolved:
                if row[0] != mid:
                    continue
                trade_side = row[2]
                trade_price = row[3]
                trade_size = row[4]

                if trade_side == "YES":
                    won = resolved_yes
                else:
                    won = not resolved_yes

                if won:
                    payout = trade_size / trade_price
                    pnl = payout - trade_size
                    result_str = "WIN"
                    has_win = True
                else:
                    pnl = -trade_size
                    result_str = "LOSS"

                conn.execute(
                    "UPDATE trades SET result = ?, pnl = ? WHERE market_id = ? AND result IS NULL",
                    (result_str, round(pnl, 2), mid),
                )
                log.info(f"RESOLVED: {result_str} on '{row[1]}' — P&L: ${pnl:+.2f}")

            conn.commit()

            # Redeem winning conditional tokens back to USDC.e
            if has_win:
                condition_id = market.get("conditionId", market.get("condition_id", ""))
                neg_risk = market.get("negRisk", market.get("neg_risk", False))
                if condition_id:
                    log.info(f"Redeeming tokens for '{market.get('question', mid)[:50]}'...")
                    await redeem_positions(condition_id, neg_risk, config)


async def check_exits(portfolio: Portfolio, config: Config, risk: RiskManager) -> None:
    """Check open positions for stop-loss or take-profit exits."""
    exits = portfolio.positions_needing_exit()
    for pos in exits:
        log.info(f"Exiting: {pos.question} ({pos.unrealized_pnl_pct:.1%} P&L)")
        result = await sell_position(
            token_id=pos.token_id,
            price=pos.current_price,
            size_shares=pos.size_shares,
            config=config,
        )
        if result.success:
            pnl = pos.unrealized_pnl
            risk.record_loss(pnl)
            portfolio.record_trade(result)


def log_status(portfolio: Portfolio, risk: RiskManager, cycle: int) -> None:
    """Print portfolio status summary."""
    bankroll = portfolio.bankroll()
    exposure = portfolio.exposure()
    u_pnl = portfolio.unrealized_pnl()
    r_pnl = portfolio.realized_pnl
    dd = portfolio.drawdown()
    n_positions = len(portfolio.positions)

    log.info(
        f"Status: bankroll=${bankroll:.2f} | exposure=${exposure:.2f} | "
        f"positions={n_positions} | unrealized={u_pnl:+.2f} | "
        f"realized={r_pnl:+.2f} | drawdown={dd:.1%}"
    )


def main():
    config = Config()
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        log.info("Bot stopped by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
