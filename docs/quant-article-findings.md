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
