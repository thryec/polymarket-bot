from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from .analyst import Signal
from .config import Config
from .portfolio import Portfolio

log = logging.getLogger(__name__)


@dataclass
class RiskManager:
    config: Config
    _pause_until: float = 0.0
    _halted: bool = False
    _bet_scale: float = 1.0
    _daily_loss: float = 0.0
    _daily_reset_ts: float = field(default_factory=time.time)

    def pre_trade_ok(self, signal: Signal, portfolio: Portfolio) -> bool:
        """Run all pre-trade risk checks. Returns True if trade is allowed."""
        if self._halted:
            log.warning("HALTED: All trading suspended due to max drawdown breach")
            return False

        if time.time() < self._pause_until:
            remaining = int(self._pause_until - time.time())
            log.warning(f"PAUSED: Trading paused for {remaining}s due to drawdown")
            return False

        bankroll = portfolio.bankroll()
        if bankroll <= 0:
            log.warning("Zero bankroll — cannot trade")
            return False

        exposure = portfolio.exposure()

        proposed_bet = signal.market_price * bankroll * 0.15
        for pos in portfolio.positions.values():
            if pos.market_id == signal.market_id:
                existing_cost = pos.cost_basis
                if (existing_cost + proposed_bet) > bankroll * 0.20:
                    log.warning(
                        f"Single position limit: {signal.question} "
                        f"(existing=${existing_cost:.0f}, limit=${bankroll * 0.20:.0f})"
                    )
                    return False

        if exposure > bankroll * 0.85:
            log.warning(f"Exposure limit: ${exposure:.0f} > ${bankroll * 0.85:.0f}")
            return False

        similar = self._count_similar_positions(signal, portfolio)
        if similar >= 2:
            log.warning(
                f"Correlation guard: {similar} similar positions for '{signal.question}'"
            )
            return False

        self._reset_daily_if_needed()
        if self._daily_loss > bankroll * 0.15:
            log.warning(f"Daily loss limit hit: ${self._daily_loss:.0f}")
            return False

        return True

    def update_drawdown(self, portfolio: Portfolio) -> None:
        """Check portfolio drawdown and apply circuit breaker rules."""
        portfolio.update_peak()
        dd = portfolio.drawdown()

        if dd > self.config.max_drawdown_pct:
            if not self._halted:
                log.critical(
                    f"CIRCUIT BREAKER: Drawdown {dd:.1%} > {self.config.max_drawdown_pct:.0%}. "
                    "HALTING ALL TRADING."
                )
                self._halted = True
                self._bet_scale = 0.0
        elif dd > 0.20:
            if self._pause_until < time.time():
                log.warning(f"Drawdown {dd:.1%} > 20%: Pausing new trades for 30 minutes")
                self._pause_until = time.time() + 1800
                self._bet_scale = 0.25
        elif dd > 0.10:
            log.info(f"Drawdown {dd:.1%} > 10%: Halving bet sizes")
            self._bet_scale = 0.5
        else:
            self._bet_scale = 1.0

    def scale_bet(self, bet: float) -> float:
        """Apply drawdown-based bet scaling."""
        return bet * self._bet_scale

    def record_loss(self, amount: float) -> None:
        """Track realized losses for daily limit."""
        if amount < 0:
            self._daily_loss += abs(amount)

    def is_halted(self) -> bool:
        return self._halted

    def reset_halt(self) -> None:
        """Manual reset after reviewing situation."""
        log.info("Halt reset — resuming trading")
        self._halted = False
        self._bet_scale = 1.0

    @staticmethod
    def _extract_keywords(text: str) -> set[str]:
        """Extract meaningful keywords from question text."""
        stop = {
            "will", "the", "be", "in", "on", "at", "to", "of", "a", "an",
            "by", "for", "or", "and", "is", "it", "this", "that", "with",
            "from", "as", "are", "was", "were", "been", "being", "have",
            "has", "had", "do", "does", "did", "but", "not", "no", "yes",
            "before", "after", "above", "below", "between", "during",
            "than", "more", "less", "what", "which", "who", "whom",
        }
        words = set(re.findall(r"[a-z]{3,}", text.lower()))
        return words - stop

    def _count_similar_positions(self, signal: Signal, portfolio: Portfolio) -> int:
        """Count existing positions with significant keyword overlap."""
        signal_kw = self._extract_keywords(signal.question)
        if not signal_kw:
            return 0
        count = 0
        for pos in portfolio.positions.values():
            if pos.market_id == signal.market_id:
                continue
            pos_kw = self._extract_keywords(pos.question)
            if not pos_kw:
                continue
            overlap = signal_kw & pos_kw
            smaller = min(len(signal_kw), len(pos_kw))
            if smaller > 0 and len(overlap) / smaller >= 0.4:
                count += 1
        return count

    def _reset_daily_if_needed(self) -> None:
        """Reset daily loss counter every 24 hours."""
        now = time.time()
        if now - self._daily_reset_ts > 86400:
            self._daily_loss = 0.0
            self._daily_reset_ts = now
