"""Execute one durable scheduled trading cycle, then exit.

Invoke this command from a platform scheduler every 15 minutes. It is designed
for paper trading: state is read before the cycle and written even if the cycle
fails, so a later invocation resumes the same portfolio.
"""

import argparse
import logging

from app.core.config import settings
from app.run_agent import get_research_positions, scan_and_allocate
from app.services.coin_selector import CoinSelector
from app.services.decision_agent import PortfolioDecisionAgent
from app.services.llm_research_agent import LLMResearchAgent
from app.services.portfolio_state import PortfolioStateStore
from app.services.trading_agent import PortfolioAgent


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="Run one scheduled trading cycle")
    parser.add_argument(
        "--trade-mode",
        choices=("paper", "live"),
        default="paper" if settings.PAPER_TRADING else "live",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    state_store = PortfolioStateStore(settings.DATABASE_URL)
    agent = PortfolioAgent(
        monitor_interval_seconds=settings.MONITOR_INTERVAL_SECONDS,
        paper_trading=args.trade_mode == "paper",
        starting_capital_usdt=settings.TOTAL_CAPITAL_USDT,
    )
    restored = agent.restore_state(state_store.load())
    logger.info(
        "Starting scheduled %s cycle | restored_state=%s | open_positions=%s | available=%.2f",
        args.trade_mode,
        restored,
        len(agent.positions),
        agent.available_usdt or 0.0,
    )

    try:
        # Exit protection has priority and never waits for a scan or an LLM.
        if agent.has_open_positions():
            agent.monitor_positions_once()

        # Keep the scheduler enabled: when the daily loss guard is active this
        # inexpensive check exits, allowing automatic reset on the next day.
        if agent.is_daily_trading_halted():
            logger.warning("Scheduled cycle skipped: daily-loss circuit breaker is active")
            return

        selector = CoinSelector()
        decision_agent = PortfolioDecisionAgent(settings.TOTAL_CAPITAL_USDT)
        coins, recommendation = scan_and_allocate(selector, decision_agent)
        if not recommendation:
            logger.info("Cycle completed: no qualified entry basket; cash remains available")
            return

        agent.update_latest_recommendations([coin["symbol"] for coin in coins])

        # The adviser runs before any new entry. A failed or disabled adviser
        # is neutral; deterministic price-risk exits above still apply.
        research_agent = LLMResearchAgent()
        advisory = research_agent.analyze(coins, get_research_positions(agent))
        if advisory:
            decision_agent.update_llm_advisory(advisory)

        opened = decision_agent.run_minute(agent, recommendation)
        logger.info("Scheduled cycle complete | new_positions=%s", len(opened))
    except Exception:
        logger.exception("Scheduled cycle failed")
        raise
    finally:
        state_store.save(agent.export_state())
        logger.info("Portfolio state saved for the next scheduled cycle")


if __name__ == "__main__":
    main()
