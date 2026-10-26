"""Fast, deterministic portfolio decisions built from selector metrics.

This is intentionally not an LLM. It makes one-minute decisions locally from
the ranked market data and position state, which keeps execution fast,
repeatable, and auditable.
"""

import logging
import time

from app.core.config import settings
from app.services.portfolio_allocator import PortfolioAllocator

logger = logging.getLogger(__name__)


class PortfolioDecisionAgent:
    def __init__(self, total_capital_usdt):
        self.total_capital_usdt = total_capital_usdt
        self.llm_advisory = None

    def recommend(self, ranked_coins):
        """Turn the qualified top-two selector output into a full basket."""
        return PortfolioAllocator.allocate(
            ranked_coins,
            total_capital=self.total_capital_usdt,
            min_trade_size=settings.MIN_TRADE_SIZE_USDT,
            deployment_ratio=settings.INITIAL_DEPLOYMENT_RATIO,
            max_positions=settings.MAX_OPEN_POSITIONS,
        )["recommended"]

    def update_llm_advisory(self, advisory):
        """Accept validated research, never a direct order instruction."""
        self.llm_advisory = advisory
        logger.info(
            "LLM advisory updated | regime=%s | confidence=%.2f | summary=%s",
            advisory["market_regime"],
            advisory["market_confidence"],
            advisory["summary"],
        )

    def _advisory_for(self, symbol):
        if not self.llm_advisory:
            return None
        generated_at = self.llm_advisory.get("generated_at", 0)
        if time.time() - generated_at > settings.LLM_ADVISOR_MAX_AGE_SECONDS:
            logger.warning("LLM advisory expired; continuing with quantitative rules only")
            self.llm_advisory = None
            return None
        for view in self.llm_advisory.get("coin_views", []):
            if view.get("symbol") == symbol:
                return view
        return None

    def _prioritized_allocations(self, recommendation):
        """Keep approved primary allocations ahead of replacements.

        The LLM can block a high-confidence ``avoid`` entry, but it cannot
        silently promote a fallback candidate above an approved allocation.
        This makes the execution log directly auditable against the plan.
        """
        def eligible_allocations(allocations):
            eligible = []
            for allocation in allocations:
                advice = self._advisory_for(allocation["symbol"])
                confidence = advice.get("confidence", 0.0) if advice else 0.0
                action = advice.get("action", "neutral") if advice else "neutral"

                if action == "avoid" and confidence >= settings.LLM_MIN_CONFIDENCE:
                    logger.info(
                        "LLM advisory blocks new entry for %s", allocation["symbol"]
                    )
                    continue
                eligible.append(allocation)
            return eligible

        # Preserve the deterministic allocation order. Fallbacks are used only
        # if a primary position could not be opened or later leaves a slot.
        return (
            eligible_allocations(recommendation["allocations"])
            + eligible_allocations(recommendation.get("fallback_allocations", []))
        )

    def _close_llm_confirmed_weak_positions(self, execution_agent):
        """Allow an LLM warning only when deterministic weakness confirms it."""
        for symbol in list(execution_agent.positions):
            advice = self._advisory_for(symbol)
            if not advice:
                continue
            if (
                advice.get("action") != "avoid"
                or advice.get("confidence", 0.0) < settings.LLM_MIN_CONFIDENCE
            ):
                continue

            snapshot = execution_agent.get_position_snapshot(symbol)
            # This is deliberately stricter than the normal stop. The LLM can
            # accelerate an already weak (-0.5% or worse) exit; it cannot sell
            # a winner or override the deterministic hard-stop rules.
            if snapshot["pnl_pct"] <= -0.005:
                logger.info(
                    "LLM-confirmed weakness exit for %s | pnl=%+.2f%%",
                    symbol,
                    snapshot["pnl_pct"] * 100,
                )
                execution_agent.close_position(symbol, "llm_confirmed_weakness")

    def run_minute(self, execution_agent, recommendation):
        """Apply hard rules, then fill slots with bounded research input."""
        if not recommendation:
            return []
        if execution_agent.is_daily_trading_halted():
            logger.warning("New entries disabled by the daily-loss circuit breaker")
            return []
        self._close_llm_confirmed_weak_positions(execution_agent)
        return execution_agent.refill_from_recommendations(
            self._prioritized_allocations(recommendation)
        )
