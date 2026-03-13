# Quant Article Findings: "How to Simulate Like a Quant Desk"

Key gaps identified in our bot and the fixes applied.

## 1. No Calibration Tracking
**Problem:** No feedback loop — bot doesn't know if its probability estimates are accurate.
**Fix:** Added Brier score computation over resolved trades (`db.py:get_calibration_stats`) and
calibration section to the status dashboard showing per-bucket accuracy.

## 2. No Extreme-Price Awareness
**Problem:** LLM probability estimates are least reliable at market extremes (p<0.10 or p>0.90).
The bot was treating all price levels equally.
**Fix:** Added `price_reliability` multiplier in `executor.py:calculate_bet()`:
`max(1.0 - (2.0 * abs(market_price - 0.5)) ** 2, 0.05)`
- p=0.50 → 1.0x (full size)
- p=0.85 → 0.51x (half size)
- p=0.95 → 0.19x (tiny size)

## 3. No Category-Based Concentration Limits
**Problem:** Correlation guard only used keyword overlap. Bot could take 5+ crypto bets
that don't share keywords but are all correlated to BTC price.
**Fix:** Added `CATEGORY_PATTERNS` in `risk.py` with keyword-based categorization
(crypto, us_politics, geopolitics, sports, economics). Max 3 positions per category.

## 4. No Liquidity-Aware Sizing
**Problem:** Bot sized bets the same regardless of market liquidity. Thin markets have
wider spreads and more slippage.
**Fix:** Added `liquidity_mult` in `executor.py` based on liquidity + 24h volume:
- Thin ($2k) → 0.4x
- Baseline ($10k) → 1.0x
- Liquid ($50k+) → 1.35x

## 5. No Time-Decay Conservatism
**Problem:** Markets near expiry are volatile and harder to exit. Bot didn't account
for time remaining.
**Fix:** Added `time_mult` in `executor.py`:
- 2+ days → 1.0x (normal)
- 1 day → 0.55x
- 6 hours → 0.33x

---

## Phase 2: Accuracy Improvements (from article's deeper analysis)

### 6. Weak Analysis Prompt
**Problem:** Prompt said "be aggressive in finding mispricings" — biased toward action.
No structured framework for base rate thinking. LLM anchors on market price.
**Fix:** Restructured prompt with 5-step analysis framework:
1. Base rate — historical frequency before looking at specifics
2. Case FOR current price — strongest arguments the market is right
3. Case AGAINST — specific evidence the price is wrong
4. Consensus check — compare against external forecasters mentioned in news
5. Estimate — only trade if Step 3 outweighs Step 2

### 7. Haiku Screener Too Loose
**Problem:** Screener only saw question + price. No description, no category performance data.
**Fix:** Added market description + liquidity to screener summaries. Feed category win rates
from past trades so Haiku avoids categories where the bot has historically lost.

### 8. Shallow News Context
**Problem:** Single Brave search with just the market question. Misses forecast/odds context.
**Fix:** Run 2 searches — one with the raw question, one appending "probability forecast odds"
to find forecaster opinions. Deduplicate results, return up to 8 snippets.

### 9. No Category Specialization
**Problem:** Same 5% min_edge for all categories, even ones where bot has <40% win rate.
**Fix:** `get_category_win_rates()` in `db.py` tracks per-category performance. If a category
has <40% win rate over 3+ resolved trades, min_edge is raised to 10%.

### 10. No Market Age Filter
**Problem:** Brand new markets have poor price discovery. Early prices are unreliable.
**Fix:** Added `MIN_MARKET_AGE_HOURS = 24` filter in scanner. Markets created <24h ago
are skipped — wait for initial price discovery to settle.

### 11. No Consensus Cross-Check
**Problem:** Bot forms opinions in isolation. If its estimate diverges from the market AND
external forecasters, it's probably wrong.
**Fix:** Added Step 4 (consensus check) in the analysis prompt. Claude now explicitly looks
for external probability estimates in the news context and lowers confidence when its
estimate diverges from multiple sources.
