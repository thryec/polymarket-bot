from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import anthropic
import httpx

from .config import Config
from .db import get_conn, get_category_win_rates, insert_analysis
from .microstructure import analyze_microstructure

log = logging.getLogger(__name__)

ANALYSIS_TOOL = {
    "name": "submit_analysis",
    "description": "Submit your market analysis with a probability estimate and trading recommendation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "estimated_probability": {
                "type": "number",
                "description": "Your estimated true probability of YES outcome (0.0 to 1.0). Round to nearest 0.05 — e.g. 0.55, 0.60, 0.75. False precision is worse than honest uncertainty.",
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
    liquidity: float = 0.0
    volume_24h: float = 0.0
    days_to_expiry: float = 14.0


# Cache: market_id -> timestamp of last analysis (skip if analyzed within 30 min)
_analysis_cache: dict[str, float] = {}
CACHE_TTL = 1800  # 30 minutes
MIN_LIQUIDITY_FOR_SCREEN = 5_000.0


async def pre_screen_markets(markets: list[dict], config: Config) -> list[dict]:
    """Use Haiku to quickly filter markets worth deep-analyzing. ~20x cheaper than Opus."""
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

    market_summaries = []
    for i, m in enumerate(candidates[:30]):
        prices = m.get("_prices", {})
        yes_p = prices.get("Yes", prices.get("yes", 0.5))
        q = m.get("question", "?")
        desc = (m.get("description") or "")[:120]
        liq = m.get("_liquidity", 0)
        summary = f"{i+1}. {q} (YES={yes_p:.2f}, liq=${liq:,.0f}, vol=${m.get('_volume_24h', 0):,.0f})"
        if desc:
            summary += f"\n   {desc}"
        market_summaries.append(summary)

    # Build category performance context from past trades
    cat_guidance = _get_category_performance(config)

    batch_prompt = f"""You are a prediction market screener. Below are {len(market_summaries)} markets.
For each, reply with ONLY the number and one of: INTERESTING or SKIP.

Mark INTERESTING only if you think the market price might be meaningfully wrong (>5% edge)
based on your knowledge. Be selective — mark at most 5 as INTERESTING.

SKIP sports matches unless the odds look obviously wrong.
SKIP markets where you have no basis to disagree with the price.
Prefer markets with high liquidity (>${MIN_LIQUIDITY_FOR_SCREEN:,.0f}) — they have better price discovery, so mispricings are more meaningful.
{cat_guidance}
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
        return candidates[:5]

    flagged = []
    for i, m in enumerate(candidates[:30]):
        marker = f"{i+1}."
        for line in text.split("\n"):
            if line.strip().startswith(marker) and "INTERESTING" in line.upper():
                flagged.append(m)
                break

    log.info(f"Pre-screen: {len(candidates)} candidates → {len(flagged)} flagged by Haiku")
    return flagged[:5]


async def analyze_market(market: dict, config: Config) -> Signal | None:
    """Deep-analyze a single market using Opus. Only called for pre-screened markets."""
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

    _analysis_cache[market_id] = time.time()

    condition_id = market.get("conditionId", market.get("condition_id", ""))

    news_context = await _search_news(question, config)
    trade_history = _get_trade_history(config)
    micro = await analyze_microstructure(condition_id, clob_token_ids)
    micro_context = micro.to_prompt_section()

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
        microstructure_context=micro_context,
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

    # Snap probability to nearest 0.05 — LLM can't meaningfully distinguish
    # 0.73 from 0.71, and false precision hurts Kelly sizing
    raw_prob = analysis["estimated_probability"]
    snapped_prob = round(raw_prob * 20) / 20  # nearest 0.05
    snapped_prob = max(0.05, min(0.95, snapped_prob))
    analysis["estimated_probability"] = snapped_prob
    analysis["edge"] = abs(snapped_prob - yes_price)

    conn = get_conn(config.db_path)
    insert_analysis(
        conn,
        market_id=market_id,
        question=question,
        market_price=yes_price,
        estimated_prob=snapped_prob,
        confidence=analysis["confidence"],
        edge=analysis["edge"],
        recommendation=analysis["recommendation"],
        reasoning=analysis["reasoning"],
        key_risks=analysis["key_risks"],
    )

    if analysis["recommendation"] == "SKIP":
        log.info(f"SKIP: {question} (edge={analysis['edge']:.3f}, conf={analysis['confidence']})")
        return None

    # Dynamic min_edge: raise threshold for categories with poor win rates
    effective_min_edge = config.min_edge
    cat_rates = get_category_win_rates(conn)
    if cat_rates:
        import re
        from .risk import CATEGORY_PATTERNS
        words = set(re.findall(r"[a-z]{2,}", question.lower()))
        best_cat, best_n = "other", 0
        for cat, kw in CATEGORY_PATTERNS.items():
            n = len(words & kw)
            if n > best_n:
                best_cat, best_n = cat, n
        if best_cat in cat_rates and cat_rates[best_cat]["win_rate"] < 0.4:
            effective_min_edge = max(config.min_edge, 0.10)
            log.info(f"Raised min_edge to {effective_min_edge:.0%} for '{best_cat}' (win rate {cat_rates[best_cat]['win_rate']:.0%})")

    if analysis["edge"] < effective_min_edge:
        log.info(f"Low edge: {question} (edge={analysis['edge']:.3f} < {effective_min_edge})")
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

    _days_to_expiry = market.get("_days_to_expiry", 14.0)

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
        liquidity=liquidity,
        volume_24h=volume_24h,
        days_to_expiry=_days_to_expiry,
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
    microstructure_context: str = "",
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

    return f"""You are an expert prediction market analyst. Your job is to find mispricings — but only when you have genuine evidence. False confidence is more costly than missed opportunities.

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
{microstructure_context}
{history_section}
## Analysis Framework (follow this order)

**Step 1 — Base rate.** Before looking at market-specific evidence, what is the historical base rate for this type of event? (e.g., how often does the Fed cut in a given month? How often does an incumbent win? How often does a team in this position win?) Start from the base rate and adjust.

**Step 2 — Case FOR the current price.** Write the strongest 2-3 arguments for why the market price is already correct. Markets with ${liquidity:,.0f} in liquidity and ${volume_24h:,.0f} daily volume reflect many informed participants. What do they know? Consider the order book imbalance and whale positions if shown above — large holders often have private information.

**Step 3 — Case AGAINST the current price.** What specific evidence suggests the price is wrong? This must be concrete — not "I think" but "the latest jobs report showed X" or "historically Y happens Z% of the time." Look at the trade flow data — are large trades skewing one direction?

**Step 4 — Consensus check.** Do any of the news results mention probability estimates, forecasts, polls, or odds from other sources (538, Metaculus, bookmakers, analysts)? If your estimate diverges significantly from both the market AND external forecasters, lower your confidence — you are likely wrong.

**Step 5 — Estimate.** Given steps 1-4, what is the true probability of YES? How far is it from the market price? Only recommend a trade if you have specific, articulable evidence from Step 3 that outweighs Step 2.

## Rules
- If your edge is < 5% or your evidence in Step 3 is weaker than Step 2, recommend SKIP.
- Confidence 1-6: you're guessing or relying on weak priors. Confidence 7-8: you have specific evidence. Confidence 9-10: you have strong, concrete evidence that the market is wrong.
- Sports matches without injury/form data: default to SKIP.
- The market is often right. Disagreeing requires strong justification.

Use the submit_analysis tool to provide your structured analysis."""


def _get_trade_history(config: Config) -> str:
    """Pull resolved trades from DB to feed into the prompt for learning."""
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


def _get_category_performance(config: Config) -> str:
    """Build category performance summary from resolved trades for the screener."""
    import re
    from .risk import CATEGORY_PATTERNS

    conn = get_conn(config.db_path)
    rows = conn.execute(
        "SELECT question, result FROM trades WHERE result IS NOT NULL"
    ).fetchall()
    if not rows:
        return ""

    cat_stats: dict[str, dict] = {}
    for row in rows:
        question = row[0] or ""
        result = row[1]
        words = set(re.findall(r"[a-z]{2,}", question.lower()))
        best_cat, best_n = "other", 0
        for cat, kw in CATEGORY_PATTERNS.items():
            n = len(words & kw)
            if n > best_n:
                best_cat, best_n = cat, n

        if best_cat not in cat_stats:
            cat_stats[best_cat] = {"wins": 0, "losses": 0}
        if result == "WIN":
            cat_stats[best_cat]["wins"] += 1
        else:
            cat_stats[best_cat]["losses"] += 1

    lines = []
    for cat, s in cat_stats.items():
        total = s["wins"] + s["losses"]
        if total >= 3:
            wr = s["wins"] / total
            if wr < 0.4:
                lines.append(f"Be EXTRA cautious on {cat} markets — past win rate is {wr:.0%} ({s['wins']}W-{s['losses']}L).")
    return "\n".join(lines)


def _extract_tool_result(response) -> dict | None:
    """Extract the tool use input from Claude's response."""
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_analysis":
            return block.input
    return None


async def _search_news(query: str, config: Config) -> str:
    """Search for recent news using Brave Search API with multiple angles."""
    if not config.brave_search_api_key:
        return ""

    # Build targeted search queries from the market question
    queries = [query]
    # Add a probability/odds-focused query to find forecaster opinions
    q_lower = query.lower()
    if any(w in q_lower for w in ["will", "?", "by", "before"]):
        queries.append(f"{query} probability forecast odds")

    seen_titles: set[str] = set()
    all_lines: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for q in queries[:2]:
                resp = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": q, "count": "5", "freshness": "pw"},
                    headers={
                        "Accept": "application/json",
                        "X-Subscription-Token": config.brave_search_api_key,
                    },
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                results = data.get("web", {}).get("results", [])
                for r in results[:5]:
                    title = r.get("title", "")
                    if title in seen_titles:
                        continue
                    seen_titles.add(title)
                    snippet = r.get("description", "")
                    age = r.get("age", "")
                    all_lines.append(f"- [{age}] {title}: {snippet}")

        return "\n".join(all_lines[:8])

    except httpx.HTTPError as e:
        log.debug(f"News search failed: {e}")
        return ""
