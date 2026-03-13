from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"

# Filter thresholds
MIN_LIQUIDITY = 2_000.0
MIN_VOLUME_24H = 500.0
PRICE_LOW = 0.05
PRICE_HIGH = 0.95
MIN_EXPIRY_HOURS = 1
MAX_EXPIRY_DAYS = 14
MIN_MARKET_AGE_HOURS = 24
MAX_MARKETS_FETCH = 500
MAX_CANDIDATES = 30


async def scan_markets() -> list[dict]:
    """Fetch top markets from Gamma API sorted by volume, filter, return top candidates."""
    raw = await _fetch_top_markets()
    filtered = _filter_markets(raw)

    # Composite score: 40% entropy, 40% volume, 20% expiry proximity
    max_vol = max((m.get("_volume_24h", 0) for m in filtered), default=1) or 1
    max_days = MAX_EXPIRY_DAYS
    for m in filtered:
        entropy_score = m.get("_entropy", 0)
        volume_score = m.get("_volume_24h", 0) / max_vol
        days = m.get("_days_to_expiry", max_days)
        expiry_score = 1.0 - min(days / max_days, 1.0)
        m["_composite_score"] = 0.4 * entropy_score + 0.4 * volume_score + 0.2 * expiry_score

    filtered.sort(key=lambda m: m.get("_composite_score", 0), reverse=True)
    filtered = filtered[:MAX_CANDIDATES]
    log.info(f"Scanner: {len(raw)} fetched → {len(filtered)} candidates")
    return filtered


async def _fetch_top_markets() -> list[dict]:
    """Fetch active markets sorted by volume (highest first), capped at MAX_MARKETS_FETCH."""
    markets: list[dict] = []
    limit = 100
    offset = 0

    async with httpx.AsyncClient(timeout=30) as client:
        while len(markets) < MAX_MARKETS_FETCH:
            params = {
                "active": "true",
                "closed": "false",
                "limit": str(limit),
                "offset": str(offset),
                "order": "volume24hr",
                "ascending": "false",
            }
            try:
                resp = await client.get(f"{GAMMA_API}/markets", params=params)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                log.error(f"Gamma API error at offset {offset}: {e}")
                break

            batch = resp.json()
            if not batch:
                break

            markets.extend(batch)
            if len(batch) < limit:
                break
            offset += limit

    return markets[:MAX_MARKETS_FETCH]


def _filter_markets(markets: list[dict]) -> list[dict]:
    """Apply liquidity, volume, price, and expiry filters."""
    now = datetime.now(timezone.utc)
    result = []

    for m in markets:
        try:
            liquidity = float(m.get("liquidity", 0) or 0)
            volume_24h = float(m.get("volume24hr", 0) or 0)
            end_date_str = m.get("endDate") or m.get("end_date_iso")

            if liquidity < MIN_LIQUIDITY:
                continue
            if volume_24h < MIN_VOLUME_24H:
                continue

            outcomes_prices = _extract_prices(m)
            if not outcomes_prices:
                continue

            best_price = outcomes_prices.get("yes") or outcomes_prices.get("Yes")
            if best_price is None:
                best_price = next(iter(outcomes_prices.values()), None)
            if best_price is None:
                continue

            if not (PRICE_LOW <= best_price <= PRICE_HIGH):
                continue

            days_to_expiry = 999
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    time_to_expiry = end_date - now
                    if time_to_expiry < timedelta(hours=MIN_EXPIRY_HOURS):
                        continue
                    if time_to_expiry > timedelta(days=MAX_EXPIRY_DAYS):
                        continue
                    days_to_expiry = time_to_expiry.total_seconds() / 86400
                except (ValueError, TypeError):
                    pass

            # Market age filter: skip markets created less than 24h ago
            created_str = m.get("startDate") or m.get("createdAt")
            if created_str:
                try:
                    created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    age_hours = (now - created).total_seconds() / 3600
                    if age_hours < MIN_MARKET_AGE_HOURS:
                        continue
                except (ValueError, TypeError):
                    pass

            m["_days_to_expiry"] = days_to_expiry
            m["_prices"] = outcomes_prices
            m["_liquidity"] = liquidity
            m["_volume_24h"] = volume_24h
            m["_entropy"] = _shannon_entropy(outcomes_prices)
            result.append(m)

        except (ValueError, TypeError, KeyError) as e:
            log.debug(f"Skipping market {m.get('id', '?')}: {e}")
            continue

    return result


def _shannon_entropy(prices: dict[str, float]) -> float:
    """Compute Shannon entropy of outcome probabilities. Max=1.0 for binary market at 50/50."""
    probs = [p for p in prices.values() if 0 < p < 1]
    if not probs:
        return 0.0
    total = sum(probs)
    if total == 0:
        return 0.0
    norm = [p / total for p in probs]
    n = len(norm)
    if n <= 1:
        return 0.0
    max_ent = math.log2(n)
    ent = -sum(p * math.log2(p) for p in norm if p > 0)
    return ent / max_ent if max_ent > 0 else 0.0


def _extract_prices(market: dict) -> dict[str, float]:
    """Extract outcome prices from various Gamma API response formats."""
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
            return prices
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    if "bestAsk" in market:
        prices["yes"] = float(market["bestAsk"])
    if "bestBid" in market:
        prices["no"] = 1.0 - float(market["bestBid"])

    return prices
