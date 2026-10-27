"""Execute one durable scheduled trading cycle, then exit.

Invoke this command from a platform scheduler every 15 minutes. It is designed
for paper trading: state is read before the cycle and written even if the cycle
fails, so a later invocation resumes the same portfolio.
"""

import argparse
import logging

from sqlalchemy.engine import make_url

from app.core.config import settings
from app.run_agent import get_research_positions, scan_and_allocate
from app.services.coin_selector import CoinSelector
from app.services.decision_agent import PortfolioDecisionAgent
from app.services.llm_research_agent import LLMResearchAgent
from app.services.portfolio_state import PortfolioStateStore
from app.services.paper_performance import start_evaluation, record_performance, write_github_summary
from app.services.trading_agent import PortfolioAgent
from app.services.live_trading import LivePortfolioAgent


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="Run one scheduled trading cycle")
    parser.add_argument(
        "--trade-mode",
        choices=("paper", "reconcile", "live"),
        default="paper" if settings.PAPER_TRADING else "live",
    )
    parser.add_argument("--monitor-only", action="store_true", help="Reconcile and protect positions without strategy scans or entries")
    return parser.parse_args()


def main():
    args = parse_args()
    if settings.REQUIRE_PERSISTENT_DATABASE and make_url(settings.DATABASE_URL).get_backend_name() != "postgresql":
        raise RuntimeError("This deployment requires DATABASE_URL pointing to the existing PostgreSQL portfolio; SQLite is not allowed")
    mode = "paper" if args.trade_mode == "paper" else ("testnet" if settings.BINANCE_TESTNET else "live")
    state_store = PortfolioStateStore(settings.DATABASE_URL, mode=mode, account_id=settings.PORTFOLIO_ACCOUNT_ID)
    with state_store.cycle_lock():
        if args.trade_mode != "paper":
            agent = LivePortfolioAgent(state_store)
            try:
                agent.bootstrap(read_only=args.trade_mode == "reconcile")
                if args.trade_mode == "reconcile":
                    return
                run_strategy_cycle(agent, monitor_only=args.monitor_only)
                agent.checkpoint()
            except Exception as exc:
                # Do not save an in-memory snapshot after failed live application.
                # The journal plus last committed state drive restart recovery.
                logger.error("Live cycle stopped (%s). Resolve the order/balance discrepancy before continuing.", type(exc).__name__)
                if isinstance(exc, RuntimeError):
                    logger.error("%s", exc)
                raise SystemExit(1) from None
        else:
            run_paper_cycle(state_store, args.monitor_only)


def run_paper_cycle(state_store, monitor_only=False):
    agent = PortfolioAgent(
        monitor_interval_seconds=settings.MONITOR_INTERVAL_SECONDS,
        paper_trading=True,
        starting_capital_usdt=settings.TOTAL_CAPITAL_USDT,
    )
    saved_state = state_store.load()
    restored = agent.restore_state(saved_state)
    evaluation = (saved_state or {}).get("paper_evaluation")
    cycle_succeeded = False
    logger.info(
        "Starting scheduled paper cycle | restored_state=%s | open_positions=%s | available=%.2f",
        restored,
        len(agent.positions),
        agent.available_usdt or 0.0,
    )

    try:
        evaluation = start_evaluation(agent, evaluation)
        run_strategy_cycle(agent, monitor_only)
        cycle_succeeded = True
    except Exception:
        logger.exception("Scheduled paper cycle failed")
        raise
    finally:
        report = None
        if evaluation is not None:
            try:
                report = record_performance(agent, evaluation, cycle_succeeded)
            except Exception:
                logger.exception("Paper valuation unavailable; preserving portfolio without inventing a return")
        payload = agent.export_state()
        if evaluation is not None:
            payload["paper_evaluation"] = evaluation
        state_store.save(payload, performance=report)
        logger.info("Portfolio state saved for the next scheduled cycle")
        if report is not None:
            try:
                write_github_summary(report)
            except OSError:
                logger.exception("Could not write the GitHub summary; performance remains saved in the database")


def run_strategy_cycle(agent, monitor_only=False):
    # Exit protection has priority and never waits for a scan or an LLM.
    if agent.has_open_positions():
        agent.monitor_positions_once()

    # Keep the scheduler enabled: when the daily loss guard is active this
    # inexpensive check exits, allowing automatic reset on the next day.
    if agent.is_daily_trading_halted():
        logger.warning("Scheduled cycle skipped: daily-loss circuit breaker is active")
        return

    if monitor_only:
        return

    selector = CoinSelector()
    decision_agent = PortfolioDecisionAgent(agent.starting_capital_usdt)
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

if __name__ == "__main__":
    main()
