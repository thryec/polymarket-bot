from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import httpx

from .config import Config
from .db import get_conn, insert_snapshot, insert_trade
from .executor import OrderResult

log = logging.getLogger(__name__)


@dataclass
class Position:
    market_id: str
    question: str
    side: str  # "YES" or "NO"
    token_id: str
    size_shares: float
    avg_price: float
    cost_basis: float
    current_price: float = 0.0

    @property
    def current_value(self) -> float:
        return self.size_shares * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        return self.current_value - self.cost_basis

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.cost_basis == 0:
            return 0.0
        return self.unrealized_pnl / self.cost_basis


class Portfolio:
    def __init__(self, config: Config):
        self.config = config
        self.positions: dict[str, Position] = {}
        self.realized_pnl: float = 0.0
        self._usdc_balance: float = 0.0
        self._peak_value: float = 0.0

    def record_trade(self, result: OrderResult, signal=None) -> None:
        """Record a new trade from an order result."""
        if not result.success:
            return

        token_id = signal.token_id if signal else ""
        question = signal.question if signal else ""
        side = result.side
        market_id = result.market_id

        if side == "SELL":
            if token_id in self.positions:
                pos = self.positions[token_id]
                sell_value = result.size_usdc
                self.realized_pnl += sell_value - pos.cost_basis
                del self.positions[token_id]
                log.info(f"Closed position: {question} (realized P&L: ${sell_value - pos.cost_basis:.2f})")
        else:
            size_shares = result.size_usdc / result.price if result.price > 0 else 0

            if token_id in self.positions:
                pos = self.positions[token_id]
                total_cost = pos.cost_basis + result.size_usdc
                total_shares = pos.size_shares + size_shares
                pos.avg_price = total_cost / total_shares if total_shares > 0 else 0
                pos.size_shares = total_shares
                pos.cost_basis = total_cost
            else:
                self.positions[token_id] = Position(
                    market_id=market_id,
                    question=question,
                    side=side,
                    token_id=token_id,
                    size_shares=size_shares,
                    avg_price=result.price,
                    cost_basis=result.size_usdc,
                    current_price=result.price,
                )

        conn = get_conn(self.config.db_path)
        insert_trade(
            conn,
            market_id=market_id,
            question=question,
            side=side,
            price=result.price,
            size_usdc=result.size_usdc,
            order_id=result.order_id,
            status="filled" if result.success else "failed",
            edge=signal.edge if signal else None,
            confidence=signal.confidence if signal else None,
            estimated_prob=signal.estimated_prob if signal else None,
            reasoning=signal.reasoning if signal else None,
        )

    async def sync_prices(self) -> None:
        """Update current prices for all open positions from Gamma API."""
        if not self.positions:
            return

        market_ids = {pos.market_id for pos in self.positions.values()}

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                for mid in market_ids:
                    resp = await client.get(
                        f"https://gamma-api.polymarket.com/markets/{mid}"
                    )
                    if resp.status_code != 200:
                        continue

                    market = resp.json()
                    prices = _parse_prices(market)

                    for pos in self.positions.values():
                        if pos.market_id == mid:
                            price_key = "Yes" if pos.side == "YES" else "No"
                            alt_key = "yes" if pos.side == "YES" else "no"
                            pos.current_price = prices.get(price_key, prices.get(alt_key, pos.avg_price))

        except httpx.HTTPError as e:
            log.warning(f"Failed to sync prices: {e}")

    async def sync_balance(self) -> None:
        """Fetch USDC.e balance from Polygon blockchain."""
        if self.config.dry_run:
            if self._usdc_balance == 0:
                self._usdc_balance = 1000.0
            return

        try:
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider(self.config.polygon_rpc_url))
            usdc_address = Web3.to_checksum_address(USDC_E_ADDRESS)
            wallet = Web3.to_checksum_address(self.config.wallet_address)
            erc20_abi = [
                {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
                 "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}],
                 "type": "function"},
                {"constant": True, "inputs": [], "name": "decimals",
                 "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
            ]
            contract = w3.eth.contract(address=usdc_address, abi=erc20_abi)
            decimals = contract.functions.decimals().call()
            raw_balance = contract.functions.balanceOf(wallet).call()
            self._usdc_balance = raw_balance / (10 ** decimals)
            log.info(f"USDC balance: ${self._usdc_balance:.2f}")
        except Exception as e:
            log.warning(f"Failed to fetch balance: {e}")
            if self._usdc_balance == 0:
                log.warning("Using fallback balance of $0 — trades will be blocked")

    def bankroll(self) -> float:
        """Total portfolio value: USDC + positions."""
        position_value = sum(p.current_value for p in self.positions.values())
        return self._usdc_balance + position_value

    def exposure(self) -> float:
        """Total cost basis of open positions."""
        return sum(p.cost_basis for p in self.positions.values())

    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values())

    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl()

    def update_peak(self) -> None:
        current = self.bankroll()
        if current > self._peak_value:
            self._peak_value = current

    def drawdown(self) -> float:
        if self._peak_value == 0:
            return 0.0
        return 1.0 - (self.bankroll() / self._peak_value)

    def snapshot(self) -> None:
        """Save portfolio snapshot to DB."""
        conn = get_conn(self.config.db_path)
        positions_data = {
            tid: {
                "market_id": p.market_id,
                "question": p.question,
                "side": p.side,
                "size_shares": p.size_shares,
                "avg_price": p.avg_price,
                "cost_basis": p.cost_basis,
                "current_price": p.current_price,
                "unrealized_pnl": p.unrealized_pnl,
            }
            for tid, p in self.positions.items()
        }
        insert_snapshot(
            conn,
            bankroll=self.bankroll(),
            exposure=self.exposure(),
            unrealized_pnl=self.unrealized_pnl(),
            realized_pnl=self.realized_pnl,
            positions=positions_data,
        )

    def positions_needing_exit(self) -> list[Position]:
        """Return positions that hit stop-loss or take-profit."""
        exits = []
        for pos in self.positions.values():
            pnl_pct = pos.unrealized_pnl_pct
            if pnl_pct <= -0.30:
                log.warning(f"STOP-LOSS triggered: {pos.question} ({pnl_pct:.1%})")
                exits.append(pos)
            elif pnl_pct >= 0.50:
                log.info(f"TAKE-PROFIT triggered: {pos.question} ({pnl_pct:.1%})")
                exits.append(pos)
        return exits


# Reuse USDC.e address from executor
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


def _parse_prices(market: dict) -> dict[str, float]:
    """Parse outcome prices from a Gamma API market response."""
    prices = {}
    outcome_prices_raw = market.get("outcomePrices")
    outcomes_raw = market.get("outcomes")

    if outcome_prices_raw and outcomes_raw:
        try:
            if isinstance(outcome_prices_raw, str):
                price_list = json.loads(outcome_prices_raw)
            else:
                price_list = outcome_prices_raw

            if isinstance(outcomes_raw, str):
                outcome_list = json.loads(outcomes_raw)
            else:
                outcome_list = outcomes_raw

            for name, price in zip(outcome_list, price_list):
                prices[name] = float(price)
        except (json.JSONDecodeError, ValueError):
            pass

    return prices
