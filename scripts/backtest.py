"""Dry backtest — test scanner filters and microstructure signals against resolved markets.

Pulls recently settled Polymarket markets, runs our scanner filters and
microstructure analysis, and checks whether our signals would have predicted
the correct outcome. No LLM calls (free to run).

Usage:
    python -m scripts.backtest [--limit 100]
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

# Same thresholds as scanner
MIN_LIQUIDITY = 2_000.0
MIN_VOLUME_24H = 500.0
PRICE_LOW = 0.10
PRICE_HIGH = 0.90


async def fetch_resolved_markets(limit: int = 100) -> list[dict]:
    """Fetch recently resolved markets, fetching extra to filter for tradeable ones."""
    markets: list[dict] = []
    offset = 0
    batch_size = 100
    # Fetch 5x more than needed since many will be filtered out
    fetch_limit = limit * 10

    async with httpx.AsyncClient(timeout=30) as client:
        while len(markets) < fetch_limit:
            resp = await client.get(
                f"{GAMMA_API}/markets",
                params={
                    "closed": "true",
                    "limit": str(batch_size),
                    "offset": str(offset),
                    "order": "volume",
                    "ascending": "false",
                },
            )
            if resp.status_code != 200:
                print(f"  API error: {resp.status_code}")
                break
            batch = resp.json()
            if not batch:
                break
            markets.extend(batch)
            offset += batch_size
            if len(batch) < batch_size:
                break

    return markets


def parse_resolution(market: dict) -> dict | None:
    """Extract resolution outcome and pre-resolution price from a market."""
    outcomes_raw = market.get("outcomes")
    prices_raw = market.get("outcomePrices")

    if not outcomes_raw or not prices_raw:
        return None

    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
    except (json.JSONDecodeError, ValueError):
        return None

    if len(outcomes) < 2 or len(prices) < 2:
        return None

    # Determine winner: the outcome whose final price is "1" (or closest to 1)
    yes_final = float(prices[0])
    no_final = float(prices[1])

    if yes_final > 0.9:
        winner = "YES"
    elif no_final > 0.9:
        winner = "NO"
    else:
        return None  # not cleanly resolved

    return {
        "winner": winner,
        "yes_final": yes_final,
        "no_final": no_final,
    }


async def fetch_price_history(token_id: str) -> list[dict]:
    """Fetch price history for a token."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{CLOB_API}/prices-history",
                params={"market": token_id, "interval": "all", "fidelity": "1440"},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            return data.get("history", [])
    except Exception:
        return []


async def fetch_order_book_snapshot(token_id: str) -> dict:
    """Fetch current/last order book for a token (may be empty for resolved markets)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{CLOB_API}/book",
                params={"token_id": token_id},
            )
            if resp.status_code != 200:
                return {}
            return resp.json()
    except Exception:
        return {}


def compute_entropy(yes_price: float) -> float:
    """Shannon entropy normalized to [0,1] for a binary market."""
    if yes_price <= 0 or yes_price >= 1:
        return 0.0
    no_price = 1.0 - yes_price
    ent = -(yes_price * math.log2(yes_price) + no_price * math.log2(no_price))
    return ent  # max is 1.0 at 50/50


def analyze_price_trajectory(history: list[dict], winner: str) -> dict:
    """Analyze price path leading up to resolution."""
    if not history:
        return {"has_history": False}

    prices = [h["p"] for h in history]
    n = len(prices)

    if n < 3:
        return {"has_history": False}

    first_price = prices[0]
    last_price = prices[-1]
    mid_price = prices[n // 2]

    # Price at various points before resolution
    price_7d_ago = prices[max(0, n - 7)] if n >= 7 else first_price
    price_3d_ago = prices[max(0, n - 3)] if n >= 3 else first_price
    price_1d_ago = prices[-1]

    # Was the market right? (did the price trend toward the eventual winner?)
    if winner == "YES":
        correct_at_entry = first_price > 0.5
        correct_at_end = last_price > 0.5
        final_edge = 1.0 - last_price  # how much value was left
    else:
        correct_at_entry = first_price < 0.5
        correct_at_end = last_price < 0.5
        final_edge = last_price  # how much value was left

    # Volatility: std dev of daily price changes
    changes = [abs(prices[i] - prices[i-1]) for i in range(1, n)]
    volatility = sum(changes) / len(changes) if changes else 0

    # Trend: was price converging toward correct outcome?
    if winner == "YES":
        trend = last_price - first_price  # positive = trending toward YES (correct)
    else:
        trend = first_price - last_price  # positive = trending toward NO (correct)

    return {
        "has_history": True,
        "n_days": n,
        "first_price": first_price,
        "last_price": last_price,
        "mid_price": mid_price,
        "price_7d_ago": price_7d_ago,
        "price_3d_ago": price_3d_ago,
        "correct_at_entry": correct_at_entry,
        "correct_at_end": correct_at_end,
        "final_edge": final_edge,
        "volatility": volatility,
        "trend": trend,
        "entropy_at_entry": compute_entropy(first_price),
        "entropy_at_end": compute_entropy(last_price),
    }


def would_scanner_select(market: dict) -> tuple[bool, str]:
    """Check if our scanner filters would have selected this market.

    For resolved markets, liquidity is 0 (book closed). Use total volume instead.
    """
    volume = float(market.get("volume", 0) or 0)

    # Minimum total volume as a proxy for "was this a real market"
    if volume < 1_000:
        return False, f"low volume (${volume:,.0f})"

    return True, "passed"


async def run_backtest(limit: int = 100):
    print("=" * 80)
    print("  POLYMARKET DRY BACKTEST")
    print("  Testing scanner filters + price trajectory against resolved markets")
    print("=" * 80)

    # 1. Fetch resolved markets
    print(f"\nFetching {limit} resolved markets...")
    markets = await fetch_resolved_markets(limit)
    print(f"  Got {len(markets)} markets")

    # 2. Filter and analyze
    results = []
    scanner_passed = 0
    scanner_failed = 0

    for i, m in enumerate(markets):
        question = m.get("question", "?")[:60]
        market_id = m.get("id", "")

        resolution = parse_resolution(m)
        if not resolution:
            continue

        passed, reason = would_scanner_select(m)
        if not passed:
            scanner_failed += 1
            continue
        scanner_passed += 1

        # Get price history for YES token
        clob_ids = m.get("clobTokenIds")
        if clob_ids:
            if isinstance(clob_ids, str):
                try:
                    clob_ids = json.loads(clob_ids)
                except (json.JSONDecodeError, ValueError):
                    clob_ids = []

        yes_token = clob_ids[0] if clob_ids and len(clob_ids) > 0 else ""

        if yes_token:
            history = await fetch_price_history(yes_token)
            # Rate limit
            await asyncio.sleep(0.2)
        else:
            history = []

        trajectory = analyze_price_trajectory(history, resolution["winner"])
        if not trajectory.get("has_history"):
            continue

        results.append({
            "question": question,
            "winner": resolution["winner"],
            "volume": float(m.get("volume", 0) or 0),
            "liquidity": float(m.get("liquidity", 0) or 0),
            **trajectory,
        })

        if (i + 1) % 10 == 0:
            print(f"  Processed {i + 1}/{len(markets)} markets...")

    print(f"\n  Scanner: {scanner_passed} passed / {scanner_failed} filtered out")
    print(f"  With price history: {len(results)}")

    if not results:
        print("\nNo results to analyze.")
        return

    # 3. Compute aggregate stats
    print(f"\n{'=' * 80}")
    print("  RESULTS")
    print(f"{'=' * 80}")

    # Market efficiency: how often was the market correct at entry?
    correct_entry = sum(1 for r in results if r["correct_at_entry"])
    correct_end = sum(1 for r in results if r["correct_at_end"])
    n = len(results)

    print(f"\n  Market Efficiency (n={n} resolved markets)")
    print(f"  {'─' * 50}")
    print(f"  Market correct at listing:     {correct_entry}/{n} ({correct_entry/n:.1%})")
    print(f"  Market correct before close:   {correct_end}/{n} ({correct_end/n:.1%})")

    # Edge available: how much value was left on the table at various points?
    avg_final_edge = sum(r["final_edge"] for r in results) / n
    edges_above_5 = sum(1 for r in results if r["final_edge"] >= 0.05)
    edges_above_10 = sum(1 for r in results if r["final_edge"] >= 0.10)
    edges_above_20 = sum(1 for r in results if r["final_edge"] >= 0.20)

    print(f"\n  Edge Available (last price before resolution)")
    print(f"  {'─' * 50}")
    print(f"  Avg remaining edge:            {avg_final_edge:.1%}")
    print(f"  Markets with >5% edge:         {edges_above_5}/{n} ({edges_above_5/n:.1%})")
    print(f"  Markets with >10% edge:        {edges_above_10}/{n} ({edges_above_10/n:.1%})")
    print(f"  Markets with >20% edge:        {edges_above_20}/{n} ({edges_above_20/n:.1%})")

    # Entropy analysis: do high-entropy markets (near 50/50) resolve less predictably?
    high_ent = [r for r in results if r["entropy_at_entry"] > 0.9]
    low_ent = [r for r in results if r["entropy_at_entry"] < 0.7]

    if high_ent and low_ent:
        he_correct = sum(1 for r in high_ent if r["correct_at_entry"]) / len(high_ent)
        le_correct = sum(1 for r in low_ent if r["correct_at_entry"]) / len(low_ent)
        he_edge = sum(r["final_edge"] for r in high_ent) / len(high_ent)
        le_edge = sum(r["final_edge"] for r in low_ent) / len(low_ent)

        print(f"\n  Entropy Breakdown")
        print(f"  {'─' * 50}")
        print(f"  High entropy (>0.9, near 50/50):  n={len(high_ent)}")
        print(f"    Market correct at entry:      {he_correct:.1%}")
        print(f"    Avg remaining edge:           {he_edge:.1%}")
        print(f"  Low entropy (<0.7, confident):    n={len(low_ent)}")
        print(f"    Market correct at entry:      {le_correct:.1%}")
        print(f"    Avg remaining edge:           {le_edge:.1%}")

    # Volatility analysis
    avg_vol = sum(r["volatility"] for r in results) / n
    high_vol = [r for r in results if r["volatility"] > avg_vol]
    low_vol = [r for r in results if r["volatility"] <= avg_vol]

    if high_vol and low_vol:
        hv_edge = sum(r["final_edge"] for r in high_vol) / len(high_vol)
        lv_edge = sum(r["final_edge"] for r in low_vol) / len(low_vol)
        hv_correct = sum(1 for r in high_vol if r["correct_at_end"]) / len(high_vol)
        lv_correct = sum(1 for r in low_vol if r["correct_at_end"]) / len(low_vol)

        print(f"\n  Volatility Breakdown (avg daily change: {avg_vol:.3f})")
        print(f"  {'─' * 50}")
        print(f"  High volatility (n={len(high_vol)}):")
        print(f"    Market correct before close:  {hv_correct:.1%}")
        print(f"    Avg remaining edge:           {hv_edge:.1%}")
        print(f"  Low volatility (n={len(low_vol)}):")
        print(f"    Market correct before close:  {lv_correct:.1%}")
        print(f"    Avg remaining edge:           {lv_edge:.1%}")

    # Naive strategy: always bet with the market (take the favorite side)
    # What would the win rate be?
    print(f"\n  Naive Strategy: 'Always Bet the Favorite'")
    print(f"  {'─' * 50}")
    print(f"  Win rate:                      {correct_end/n:.1%}")
    avg_cost = sum(max(r["last_price"], 1.0 - r["last_price"]) for r in results) / n
    avg_payout = 1.0
    implied_edge = (correct_end / n) * avg_payout - avg_cost
    print(f"  Avg cost (favorite price):     ${avg_cost:.3f}")
    print(f"  Implied edge per trade:        {implied_edge:+.3f}")

    # Contrarian strategy: bet against the market when entropy is high
    contrarian = [r for r in results if r["entropy_at_entry"] > 0.9]
    if contrarian:
        # "Contrarian" = bet the opposite of the slight favorite at entry
        cont_wins = sum(1 for r in contrarian if not r["correct_at_entry"])
        cont_wr = cont_wins / len(contrarian)
        print(f"\n  Contrarian Strategy: 'Fade the Favorite on 50/50 Markets'")
        print(f"  {'─' * 50}")
        print(f"  Markets considered:            {len(contrarian)}")
        print(f"  Win rate:                      {cont_wr:.1%}")

    # Top edge opportunities (markets where last price was most wrong)
    print(f"\n  Top 10 Markets by Remaining Edge (mispriced before resolution)")
    print(f"  {'─' * 70}")
    print(f"  {'Edge':>6} {'Winner':>6} {'Last':>6} {'Vol':>10}  Question")
    print(f"  {'─'*6} {'─'*6} {'─'*6} {'─'*10}  {'─'*35}")
    sorted_by_edge = sorted(results, key=lambda r: r["final_edge"], reverse=True)
    for r in sorted_by_edge[:10]:
        print(f"  {r['final_edge']:>5.1%} {r['winner']:>6} {r['last_price']:>6.3f} ${r['volume']:>9,.0f}  {r['question']}")

    # Brier score of the market itself
    brier_sum = 0.0
    for r in results:
        outcome = 1.0 if r["winner"] == "YES" else 0.0
        brier_sum += (r["last_price"] - outcome) ** 2
    market_brier = brier_sum / n

    print(f"\n  Market Calibration")
    print(f"  {'─' * 50}")
    print(f"  Market Brier Score:            {market_brier:.4f}")
    print(f"  (0=perfect, 0.25=random — our bot needs to beat {market_brier:.4f})")

    print(f"\n{'=' * 80}")


def main():
    limit = 100
    if len(sys.argv) > 1 and sys.argv[1] == "--limit":
        limit = int(sys.argv[2])
    elif len(sys.argv) > 1:
        limit = int(sys.argv[1])

    asyncio.run(run_backtest(limit))


if __name__ == "__main__":
    main()
