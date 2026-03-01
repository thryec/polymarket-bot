from __future__ import annotations

import logging
from dataclasses import dataclass

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from web3 import Web3

from .analyst import Signal
from .config import Config

log = logging.getLogger(__name__)

# Polymarket contract addresses (Polygon)
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

CTF_REDEEM_ABI = [{
    "constant": False,
    "inputs": [
        {"name": "collateralToken", "type": "address"},
        {"name": "parentCollectionId", "type": "bytes32"},
        {"name": "conditionId", "type": "bytes32"},
        {"name": "indexSets", "type": "uint256[]"},
    ],
    "name": "redeemPositions",
    "outputs": [],
    "type": "function",
}]

NEG_RISK_REDEEM_ABI = [{
    "constant": False,
    "inputs": [
        {"name": "conditionId", "type": "bytes32"},
        {"name": "amounts", "type": "uint256[]"},
    ],
    "name": "redeemPositions",
    "outputs": [],
    "type": "function",
}]


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

    if signal.side == "YES":
        cost = market_price
        win_prob = p
    else:
        cost = 1.0 - market_price
        win_prob = 1.0 - p

    if cost <= 0 or cost >= 1:
        return 0.0

    b = (1.0 - cost) / cost
    q = 1.0 - win_prob
    kelly_raw = (win_prob * b - q) / b

    if kelly_raw <= 0:
        return 0.0

    confidence_scale = ((signal.confidence - 4) / 6.0) ** 1.5
    edge_certainty = min(signal.edge / 0.20, 1.0)

    # Extreme price dampening: LLM estimates least reliable at extremes
    price_reliability = max(1.0 - (2.0 * abs(market_price - 0.5)) ** 2, 0.05)

    # Liquidity-aware sizing: thin markets get smaller bets
    liq_score = min(signal.liquidity / 10_000, 2.0)
    vol_score = min(signal.volume_24h / 5_000, 2.0)
    liquidity_mult = 0.3 + 0.7 * min((liq_score + vol_score) / 2, 1.5)

    # Time decay: reduce size near expiry
    days = getattr(signal, 'days_to_expiry', 14.0)
    time_mult = (0.1 + 0.9 * min(days / 2.0, 1.0)) if days < 2.0 else 1.0

    f = (config.kelly_fraction * kelly_raw * confidence_scale * edge_certainty
         * price_reliability * liquidity_mult * time_mult)

    bet = f * bankroll

    bet = max(bet, 0.0)
    bet = min(bet, config.max_bet_usdc)
    bet = min(bet, bankroll * 0.15)

    if bet < 5.0:
        return 0.0

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
        client.set_api_creds(client.create_or_derive_api_creds())

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


async def redeem_positions(
    condition_id: str,
    neg_risk: bool,
    config: Config,
    token_ids: list[str] | None = None,
) -> bool:
    """Redeem winning conditional tokens back to USDC.e after market resolution.

    For neg-risk markets, queries actual YES/NO token balances and passes them
    as [yes_amount, no_amount] to the NegRiskAdapter.
    """
    if config.dry_run:
        log.info(f"[DRY RUN] Would redeem condition {condition_id[:16]}...")
        return True

    try:
        w3 = Web3(Web3.HTTPProvider(config.polygon_rpc_url))
        account = w3.eth.account.from_key(config.private_key)

        condition_bytes = Web3.to_bytes(hexstr=condition_id)
        gas_price = int(w3.eth.gas_price * 1.2)

        if neg_risk:
            # Query actual token balances for the YES/NO positions
            yes_bal, no_bal = _get_ctf_balances(w3, account.address, token_ids)
            if yes_bal == 0 and no_bal == 0:
                log.info(f"No tokens to redeem for {condition_id[:16]}...")
                return True

            contract = w3.eth.contract(
                address=Web3.to_checksum_address(NEG_RISK_ADAPTER_ADDRESS),
                abi=NEG_RISK_REDEEM_ABI,
            )
            tx = contract.functions.redeemPositions(
                condition_bytes,
                [yes_bal, no_bal],
            ).build_transaction({
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gas": 500_000,
                "gasPrice": gas_price,
            })
        else:
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(CTF_ADDRESS),
                abi=CTF_REDEEM_ABI,
            )
            tx = contract.functions.redeemPositions(
                Web3.to_checksum_address(USDC_E_ADDRESS),
                b"\x00" * 32,
                condition_bytes,
                [1, 2],
            ).build_transaction({
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gas": 500_000,
                "gasPrice": gas_price,
            })

        signed = w3.eth.account.sign_transaction(tx, config.private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] == 1:
            log.info(f"REDEEMED condition {condition_id[:16]}... tx={tx_hash.hex()}")
            return True
        else:
            log.warning(f"Redeem tx reverted for {condition_id[:16]}... tx={tx_hash.hex()}")
            return False

    except Exception as e:
        log.error(f"Redeem failed for {condition_id[:16]}...: {e}")
        return False


CTF_BALANCE_ABI = [{
    "constant": True,
    "inputs": [
        {"name": "account", "type": "address"},
        {"name": "id", "type": "uint256"},
    ],
    "name": "balanceOf",
    "outputs": [{"name": "", "type": "uint256"}],
    "type": "function",
}]


def _get_ctf_balances(
    w3: Web3, wallet: str, token_ids: list[str] | None
) -> tuple[int, int]:
    """Query YES and NO token balances from the CTF contract.

    Returns raw amounts (6-decimal integers).
    """
    if not token_ids or len(token_ids) < 2:
        return 0, 0

    ctf = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_ADDRESS),
        abi=CTF_BALANCE_ABI,
    )
    wallet_addr = Web3.to_checksum_address(wallet)
    yes_bal = ctf.functions.balanceOf(wallet_addr, int(token_ids[0])).call()
    no_bal = ctf.functions.balanceOf(wallet_addr, int(token_ids[1])).call()
    return yes_bal, no_bal
