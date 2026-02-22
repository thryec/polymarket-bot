from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import anthropic
import httpx

from config import Config
from db import get_conn, insert_analysis

log = logging.getLogger(__name__)

ANALYSIS_TOOL = {
    "name": "submit_analysis",
    "description": "Submit your market analysis with a probability estimate and trading recommendation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "estimated_probability": {
                "type": "number",
                "description": "Your estimated true probability of YES outcome (0.0 to 1.0)",
            },
            "confidence": {
                "type": "number",
                "description": "Confidence in your estimate (1-10 scale, 10 = very confident)",
            },
            "recommendation": {
                "type": "string",
                "enum": ["BUY_YES", "BUY_NO", "SKIP"],
                "description": "Trading recommendation",
            },
            "edge": {
                "type": "number",
                "description": "Estimated edge: abs(estimated_probability - market_price)",
            },
            "reasoning": {
                "type": "string",
                "description": "Brief explanation of your analysis (2-3 sentences)",
            },
            "key_risks": {
                "type": "string",
                "description": "Key risks that could invalidate this analysis",
            },
        },
        "required": [
            "estimated_probability",
            "confidence",
            "recommendation",
            "edge",
            "reasoning",
            "key_risks",
        ],
    },
}


@dataclass
class Signal:
    market_id: str
    question: str
    side: str  # "YES" or "NO"
    market_price: float
    estimated_prob: float
    confidence: float
    edge: float
    reasoning: str
    key_risks: str
    token_id: str


# Cache: market_id -> timestamp of last analysis (skip if analyzed within 30 min)
_analysis_cache: dict[str, float] = {}
CACHE_TTL = 1800  # 30 minutes


async def pre_screen_markets(markets: list[dict], config: Config) -> list[dict]:
    """Use Haiku to quickly filter markets worth deep-analyzing. ~20x cheaper than Opus."""
    import time

    # Skip recently analyzed markets
    now = time.time()
    candidates = []
    for m in markets:
        mid = m.get("id", "")
        last = _analysis_cache.get(mid, 0)
        if now - last < CACHE_TTL:
            continue
        candidates.append(m)

    if not candidates:
        log.info("Pre-screen: all markets recently analyzed, skipping")
        return []

    # Build batch prompt for Haiku
    market_summaries = []
    for i, m in enumerate(candidates[:30]):
        prices = m.get("_prices", {})
        yes_p = prices.get("Yes", prices.get("yes", 0.5))
        q = m.get("question", "?")
        market_summaries.append(f"{i+1}. {q} (YES={yes_p:.2f}, vol=${m.get('_volume_24h', 0):,.0f})")

    batch_prompt = f"""You are a prediction market screener. Below are {len(market_summaries)} markets.
For each, reply with ONLY the number and one of: INTERESTING or SKIP.

Mark INTERESTING only if you think the market price might be meaningfully wrong (>5% edge)
based on your knowledge. Be selective — mark at most 5 as INTERESTING.

SKIP sports matches unless the odds look obviously wrong.
SKIP markets where you have no basis to disagree with the price.

Markets:
""" + "\n".join(market_summaries)

    try:
        client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": batch_prompt}],
        )
        text = response.content[0].text
    except anthropic.APIError as e:
        log.error(f"Haiku pre-screen error: {e}")
        return candidates[:5]  # Fallback: just send first 5

    # Parse which markets Haiku flagged
    flagged = []
    for i, m in enumerate(candidates[:30]):
        marker = f"{i+1}."
        # Check if this number was marked INTERESTING
        for line in text.split("\n"):
            if line.strip().startswith(marker) and "INTERESTING" in line.upper():
                flagged.append(m)
                break

    log.info(f"Pre-screen: {len(candidates)} candidates → {len(flagged)} flagged by Haiku")
    return flagged[:5]  # Cap at 5 for Opus


async def analyze_market(market: dict, config: Config) -> Signal | None:
    """Deep-analyze a single market using Opus. Only called for pre-screened markets."""
    import time

    question = market.get("question", "Unknown")
    market_id = market.get("id", "")
    description = market.get("description", "")
    prices = market.get("_prices", {})
    liquidity = market.get("_liquidity", 0)
    volume_24h = market.get("_volume_24h", 0)
    end_date = market.get("endDate") or market.get("end_date_iso", "Unknown")

    yes_price = prices.get("Yes", prices.get("yes", 0.5))
    no_price = prices.get("No", prices.get("no", 0.5))

    clob_token_ids = market.get("clobTokenIds")
    if clob_token_ids:
        if isinstance(clob_token_ids, str):
            clob_token_ids = json.loads(clob_token_ids)
    else:
        clob_token_ids = []

    # Mark as analyzed
    _analysis_cache[market_id] = time.time()

    # Fetch news context
    news_context = await _search_news(question, config)

    # Get past trade results for learning
    trade_history = _get_trade_history(config)

    prompt = _build_prompt(
        question=question,
        description=description,
        yes_price=yes_price,
        no_price=no_price,
        liquidity=liquidity,
        volume_24h=volume_24h,
        end_date=end_date,
        news_context=news_context,
        trade_history=trade_history,
    )

    try:
        client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            tools=[ANALYSIS_TOOL],
            tool_choice={"type": "tool", "name": "submit_analysis"},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        log.error(f"Claude API error for '{question}': {e}")
        return None

    analysis = _extract_tool_result(response)
    if not analysis:
        log.warning(f"No structured analysis returned for '{question}'")
        return None

    conn = get_conn(config.db_path)
    insert_analysis(
        conn,
        market_id=market_id,
        question=question,
        market_price=yes_price,
        estimated_prob=analysis["estimated_probability"],
        confidence=analysis["confidence"],
        edge=analysis["edge"],
        recommendation=analysis["recommendation"],
        reasoning=analysis["reasoning"],
        key_risks=analysis["key_risks"],
    )

    if analysis["recommendation"] == "SKIP":
        log.info(f"SKIP: {question} (edge={analysis['edge']:.3f}, conf={analysis['confidence']})")
        return None

    if analysis["edge"] < config.min_edge:
        log.info(f"Low edge: {question} (edge={analysis['edge']:.3f} < {config.min_edge})")
        return None

    if analysis["confidence"] < 5:
        log.info(f"Low confidence: {question} (conf={analysis['confidence']} < 5)")
        return None

    if analysis["recommendation"] == "BUY_YES":
        side = "YES"
        market_price = yes_price
        token_id = clob_token_ids[0] if len(clob_token_ids) > 0 else ""
    else:
        side = "NO"
        market_price = no_price
        token_id = clob_token_ids[1] if len(clob_token_ids) > 1 else ""

    signal = Signal(
        market_id=market_id,
        question=question,
        side=side,
        market_price=market_price,
        estimated_prob=analysis["estimated_probability"],
        confidence=analysis["confidence"],
        edge=analysis["edge"],
        reasoning=analysis["reasoning"],
        key_risks=analysis["key_risks"],
        token_id=token_id,
    )

    log.info(
        f"SIGNAL: {side} on '{question}' @ {market_price:.3f} "
        f"(est={analysis['estimated_probability']:.3f}, edge={analysis['edge']:.3f}, "
        f"conf={analysis['confidence']})"
    )
    return signal


def _build_prompt(
    question: str,
    description: str,
    yes_price: float,
    no_price: float,
    liquidity: float,
    volume_24h: float,
    end_date: str,
    news_context: str,
    trade_history: str = "",
) -> str:
    history_section = ""
    if trade_history:
        history_section = f"""
## Your Past Trading Performance
Review your past trades below. Learn from your wins and losses to calibrate better.
{trade_history}

**Key lessons to apply:**
- If you've been losing on a certain category (e.g. sports), be MORE conservative and lower your confidence for similar markets.
- If you've been winning on a category (e.g. geopolitics), your calibration is good — maintain your approach.
- If your estimated probabilities have been off, adjust accordingly (e.g. if you overestimated YES outcomes, bias slightly toward NO).
- Individual sports matches are very hard to predict without live form/injury data — default to SKIP unless you have strong evidence.
"""

    return f"""You are an expert prediction market analyst. Analyze this Polymarket market and estimate the true probability.

## Market
**Question:** {question}
**Description:** {description}
**Current YES price:** {yes_price:.4f} (implies {yes_price*100:.1f}% probability)
**Current NO price:** {no_price:.4f}
**Liquidity:** ${liquidity:,.0f}
**24h Volume:** ${volume_24h:,.0f}
**Expiry:** {end_date}

## Recent News Context
{news_context if news_context else "No recent news found."}
{history_section}
## Instructions
1. Estimate the TRUE probability of the YES outcome based on all available evidence.
2. Compare your estimate to the current market price.
3. If the difference (edge) is >= 5%, recommend BUY_YES or BUY_NO.
4. If the edge is < 5% or you're unsure, recommend SKIP.
5. Rate your confidence 1-10 (be honest — only rate 7+ if you have strong evidence).

Be aggressive in finding mispricings but honest about uncertainty. Consider base rates, recent developments, and potential for resolution surprises.

Use the submit_analysis tool to provide your structured analysis."""


def _get_trade_history(config: Config) -> str:
    """Pull resolved trades from DB to feed into the prompt for learning."""
    from db import get_conn

    conn = get_conn(config.db_path)
    resolved = conn.execute(
        "SELECT question, side, price, size_usdc, edge, confidence, result, pnl, reasoning "
        "FROM trades WHERE result IS NOT NULL ORDER BY ts DESC LIMIT 20"
    ).fetchall()

    if not resolved:
        return ""

    wins = [r for r in resolved if r[6] == "WIN"]
    losses = [r for r in resolved if r[6] == "LOSS"]
    total_pnl = sum(r[7] or 0 for r in resolved)

    lines = [f"**Record: {len(wins)}W-{len(losses)}L | Net P&L: ${total_pnl:+.2f}**\n"]

    for r in resolved:
        question, side, price, size, edge, conf, result, pnl, reasoning = r
        short_q = (question or "")[:60]
        pnl_str = f"${pnl:+.2f}" if pnl is not None else "n/a"
        lines.append(
            f"- **{result}** ({pnl_str}): {side} on '{short_q}' @ {price:.3f} "
            f"(edge={edge:.3f}, conf={conf:.0f})"
        )
        if reasoning and result == "LOSS":
            short_reason = (reasoning or "")[:120]
            lines.append(f"  Reasoning was: {short_reason}")

    return "\n".join(lines)


def _extract_tool_result(response) -> dict | None:
    """Extract the tool use input from Claude's response."""
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_analysis":
            return block.input
    return None


async def _search_news(query: str, config: Config) -> str:
    """Search for recent news using Brave Search API. Returns formatted headlines."""
    if not config.brave_search_api_key:
        return ""

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": "5", "freshness": "pw"},
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": config.brave_search_api_key,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = data.get("web", {}).get("results", [])
        if not results:
            return ""

        lines = []
        for r in results[:5]:
            title = r.get("title", "")
            snippet = r.get("description", "")
            age = r.get("age", "")
            lines.append(f"- [{age}] {title}: {snippet}")

        return "\n".join(lines)

    except httpx.HTTPError as e:
        log.debug(f"News search failed: {e}")
        return ""
