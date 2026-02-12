from __future__ import annotations

import logging
from dataclasses import dataclass

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

from analyst import Signal
from config import Config

log = logging.getLogger(__name__)


@dataclass
class OrderResult:
    success: bool
    order_id: str
    market_id: str
    side: str
    price: float
    size_usdc: float
    error: str = ""


def calculate_bet(
    signal: Signal,
    bankroll: float,
    exposure: float,
    config: Config,
) -> float:
    """Kelly criterion sizing with guardrails."""
    p = signal.estimated_prob
    market_price = signal.market_price

    # For BUY_YES: we pay market_price, win (1 - market_price) if correct
    # For BUY_NO: we pay (1 - market_price), win market_price if correct
    if signal.side == "YES":
        cost = market_price
        win_prob = p
    else:
        cost = 1.0 - market_price
        win_prob = 1.0 - p

    if cost <= 0 or cost >= 1:
        return 0.0

    # Kelly fraction: f = (p*b - q) / b where b = (1-cost)/cost, q = 1-p
    b = (1.0 - cost) / cost  # odds ratio
    q = 1.0 - win_prob
    kelly_raw = (win_prob * b - q) / b

    if kelly_raw <= 0:
        return 0.0

    # Scale by fractional Kelly and confidence
    confidence_scale = signal.confidence / 10.0
    f = config.kelly_fraction * kelly_raw * confidence_scale

    bet = f * bankroll

    # Clamp
    bet = max(bet, 0.0)
    bet = min(bet, config.max_bet_usdc)
    bet = min(bet, bankroll * 0.15)  # Max 15% of bankroll per trade

    # Minimum viable bet
    if bet < 5.0:
        return 0.0

    # Check total exposure wouldn't exceed 85%
    if (exposure + bet) > bankroll * 0.85:
        bet = max(bankroll * 0.85 - exposure, 0.0)
        if bet < 5.0:
            return 0.0

    return round(bet, 2)


async def execute_trade(
    signal: Signal,
    size_usdc: float,
    config: Config,
) -> OrderResult:
    """Place a GTC limit order via py-clob-client."""
    if config.dry_run:
        log.info(
            f"[DRY RUN] Would place {signal.side} order on '{signal.question}' "
            f"@ {signal.market_price:.4f} for ${size_usdc:.2f}"
        )
        return OrderResult(
            success=True,
            order_id="dry-run",
            market_id=signal.market_id,
            side=signal.side,
            price=signal.market_price,
            size_usdc=size_usdc,
        )

    if not signal.token_id:
        return OrderResult(
            success=False,
            order_id="",
            market_id=signal.market_id,
            side=signal.side,
            price=signal.market_price,
            size_usdc=size_usdc,
            error="No token ID available",
        )

    try:
        client = ClobClient(
            config.clob_api_url,
            key=config.private_key,
            chain_id=config.chain_id,
        )

        # Derive API credentials
        client.set_api_creds(client.create_or_derive_api_creds())

        # Calculate size in shares: size_usdc / price
        size_shares = size_usdc / signal.market_price

        order_args = OrderArgs(
            price=round(signal.market_price, 2),
            size=round(size_shares, 2),
            side="BUY",
            token_id=signal.token_id,
        )

        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)

        order_id = ""
        if isinstance(resp, dict):
            order_id = resp.get("orderID", resp.get("id", ""))
            if resp.get("success") is False:
                error_msg = resp.get("errorMsg", "Unknown error")
                log.error(f"Order rejected: {error_msg}")
                return OrderResult(
                    success=False,
                    order_id="",
                    market_id=signal.market_id,
                    side=signal.side,
                    price=signal.market_price,
                    size_usdc=size_usdc,
                    error=error_msg,
                )

        log.info(
            f"ORDER PLACED: {signal.side} '{signal.question}' "
            f"@ {signal.market_price:.4f} for ${size_usdc:.2f} (id={order_id})"
        )

        return OrderResult(
            success=True,
            order_id=order_id,
            market_id=signal.market_id,
            side=signal.side,
            price=signal.market_price,
            size_usdc=size_usdc,
        )

    except Exception as e:
        log.error(f"Trade execution failed: {e}")
        return OrderResult(
            success=False,
            order_id="",
            market_id=signal.market_id,
            side=signal.side,
            price=signal.market_price,
            size_usdc=size_usdc,
            error=str(e),
        )


async def sell_position(
    token_id: str,
    price: float,
    size_shares: float,
    config: Config,
) -> OrderResult:
    """Place a SELL order to exit a position."""
    if config.dry_run:
        log.info(f"[DRY RUN] Would sell {size_shares:.2f} shares @ {price:.4f}")
        return OrderResult(
            success=True,
            order_id="dry-run-sell",
            market_id="",
            side="SELL",
            price=price,
            size_usdc=price * size_shares,
        )

    try:
        client = ClobClient(
            config.clob_api_url,
            key=config.private_key,
            chain_id=config.chain_id,
        )
        client.set_api_creds(client.create_or_derive_api_creds())

        order_args = OrderArgs(
            price=round(price, 2),
            size=round(size_shares, 2),
            side="SELL",
            token_id=token_id,
        )

        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)

        order_id = ""
        if isinstance(resp, dict):
            order_id = resp.get("orderID", resp.get("id", ""))

        return OrderResult(
            success=True,
            order_id=order_id,
            market_id="",
            side="SELL",
            price=price,
            size_usdc=price * size_shares,
        )

    except Exception as e:
        log.error(f"Sell execution failed: {e}")
        return OrderResult(
            success=False,
            order_id="",
            market_id="",
            side="SELL",
            price=price,
            size_usdc=price * size_shares,
            error=str(e),
        )
