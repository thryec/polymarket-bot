"""Market microstructure analysis — order book, trade flow, whale positions."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"


@dataclass
class MicrostructureSnapshot:
    # Order book
    spread: float = 0.0
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    imbalance_ratio: float = 0.0  # >1 = more bids (bullish), <1 = more asks
    best_bid: float = 0.0
    best_ask: float = 0.0
    # Trade flow
    recent_buy_volume: float = 0.0
    recent_sell_volume: float = 0.0
    trade_count: int = 0
    avg_trade_size: float = 0.0
    large_trade_count: int = 0  # trades > $100
    # Whale positions
    top_yes_holders: int = 0
    top_no_holders: int = 0
    top_yes_size: float = 0.0
    top_no_size: float = 0.0

    def to_prompt_section(self) -> str:
        """Format as text for the LLM analysis prompt."""
        lines = ["## Market Microstructure"]

        if self.spread > 0:
            lines.append(f"- **Spread:** {self.spread:.3f} ({'tight' if self.spread < 0.03 else 'wide' if self.spread > 0.08 else 'normal'})")
            lines.append(f"- **Bid depth:** ${self.bid_depth:,.0f} | Ask depth: ${self.ask_depth:,.0f}")
            if self.imbalance_ratio > 0:
                direction = "buy-side heavy" if self.imbalance_ratio > 1.3 else "sell-side heavy" if self.imbalance_ratio < 0.7 else "balanced"
                lines.append(f"- **Order imbalance:** {self.imbalance_ratio:.2f}x ({direction})")

        if self.trade_count > 0:
            lines.append(f"- **Recent trades:** {self.trade_count} fills")
            lines.append(f"- **Buy volume:** ${self.recent_buy_volume:,.0f} | Sell volume: ${self.recent_sell_volume:,.0f}")
            if self.avg_trade_size > 0:
                lines.append(f"- **Avg trade size:** ${self.avg_trade_size:,.0f}")
            if self.large_trade_count > 0:
                lines.append(f"- **Large trades (>$100):** {self.large_trade_count}")

        if self.top_yes_size > 0 or self.top_no_size > 0:
            lines.append(f"- **Top holders:** {self.top_yes_holders} YES (${self.top_yes_size:,.0f}) | {self.top_no_holders} NO (${self.top_no_size:,.0f})")

        if len(lines) == 1:
            return ""
        return "\n".join(lines)


async def analyze_microstructure(
    condition_id: str,
    token_ids: list[str],
) -> MicrostructureSnapshot:
    """Pull order book, recent trades, and whale data for a market."""
    snap = MicrostructureSnapshot()

    if not token_ids or len(token_ids) < 1:
        return snap

    yes_token = token_ids[0] if len(token_ids) > 0 else ""
    no_token = token_ids[1] if len(token_ids) > 1 else ""

    async with httpx.AsyncClient(timeout=10) as client:
        # 1. Order book
        if yes_token:
            await _fetch_order_book(client, yes_token, snap)

        # 2. Recent trades
        if condition_id:
            await _fetch_recent_trades(client, condition_id, snap)

        # 3. Top holders
        if condition_id:
            await _fetch_top_holders(client, condition_id, snap)

    return snap


async def _fetch_order_book(
    client: httpx.AsyncClient, token_id: str, snap: MicrostructureSnapshot
) -> None:
    """Fetch order book and compute spread, depth, imbalance."""
    try:
        resp = await client.get(f"{CLOB_API}/book", params={"token_id": token_id})
        if resp.status_code != 200:
            return
        data = resp.json()
    except Exception as e:
        log.debug(f"Order book fetch failed: {e}")
        return

    bids = data.get("bids", [])
    asks = data.get("asks", [])

    if bids:
        snap.best_bid = float(bids[0].get("price", 0))
    if asks:
        snap.best_ask = float(asks[0].get("price", 0))

    if snap.best_bid > 0 and snap.best_ask > 0:
        snap.spread = snap.best_ask - snap.best_bid

    # Compute depth (total $ within 5 cents of best)
    bid_depth = 0.0
    for b in bids:
        price = float(b.get("price", 0))
        size = float(b.get("size", 0))
        if snap.best_bid - price <= 0.05:
            bid_depth += price * size

    ask_depth = 0.0
    for a in asks:
        price = float(a.get("price", 0))
        size = float(a.get("size", 0))
        if price - snap.best_ask <= 0.05:
            ask_depth += price * size

    snap.bid_depth = bid_depth
    snap.ask_depth = ask_depth

    if ask_depth > 0:
        snap.imbalance_ratio = bid_depth / ask_depth


async def _fetch_recent_trades(
    client: httpx.AsyncClient, condition_id: str, snap: MicrostructureSnapshot
) -> None:
    """Fetch recent trades and compute volume flow metrics."""
    try:
        resp = await client.get(
            f"{DATA_API}/trades",
            params={"market": condition_id, "limit": "200"},
        )
        if resp.status_code != 200:
            return
        trades = resp.json()
    except Exception as e:
        log.debug(f"Trade history fetch failed: {e}")
        return

    if not isinstance(trades, list):
        return

    snap.trade_count = len(trades)
    total_size = 0.0

    for t in trades:
        side = t.get("side", "")
        size = float(t.get("size", 0))
        price = float(t.get("price", 0))
        value = size * price

        if side == "BUY":
            snap.recent_buy_volume += value
        else:
            snap.recent_sell_volume += value

        total_size += value
        if value > 100:
            snap.large_trade_count += 1

    if snap.trade_count > 0:
        snap.avg_trade_size = total_size / snap.trade_count


async def _fetch_top_holders(
    client: httpx.AsyncClient, condition_id: str, snap: MicrostructureSnapshot
) -> None:
    """Fetch top position holders to detect whale activity."""
    try:
        resp = await client.get(
            f"{DATA_API}/v1/market-positions",
            params={
                "market": condition_id,
                "sortBy": "TOKENS",
                "sortDirection": "DESC",
                "status": "OPEN",
                "limit": "20",
            },
        )
        if resp.status_code != 200:
            return
        data = resp.json()
    except Exception as e:
        log.debug(f"Top holders fetch failed: {e}")
        return

    if not isinstance(data, list):
        return

    for token_data in data:
        positions = token_data.get("positions", [])
        for pos in positions:
            outcome = pos.get("outcome", "")
            size = float(pos.get("size", 0))
            value = float(pos.get("currentValue", 0))

            if outcome in ("Yes", "yes"):
                snap.top_yes_holders += 1
                snap.top_yes_size += value
            elif outcome in ("No", "no"):
                snap.top_no_holders += 1
                snap.top_no_size += value
