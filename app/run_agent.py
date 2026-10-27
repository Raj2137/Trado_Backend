import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from app.core.config import settings
from app.services.coin_selector import CoinSelector
from app.services.decision_agent import PortfolioDecisionAgent
from app.services.llm_research_agent import LLMResearchAgent
from app.services.trading_agent import PortfolioAgent

# Configure application-wide logging for the scan output.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Reuse a module logger so log messages are easy to trace.
logger = logging.getLogger(__name__)

TOTAL_CAPITAL = settings.TOTAL_CAPITAL_USDT
MONITOR_INTERVAL_SECONDS = settings.MONITOR_INTERVAL_SECONDS
SCAN_INTERVAL_SECONDS = settings.SCAN_INTERVAL_SECONDS


def parse_args():
    # Allow startup to explicitly choose simulated or real execution.
    default_trade_mode = "paper" if settings.PAPER_TRADING else "live"
    parser = argparse.ArgumentParser(description="Run the trading agent")
    parser.add_argument(
        "--trade-mode",
        choices=("paper", "live"),
        default=default_trade_mode,
        help="Select paper trading or live trading",
    )
    return parser.parse_args()


def log_selected_coins(coins):
    # Print the scan result so ranking and backtest metrics stay visible.
    logger.info("Top %s coins selected for the next stage", len(coins))
    for index, coin in enumerate(coins, start=1):
        logger.info(
            "%02d. %s | score=%.3f | price=%.6f | 24h volume=$%.0f | "
            "6h momentum=%+.2f%% | 24h momentum=%+.2f%% | "
            "bt avg=%+.2f%% | bt win=%.0f%% | bt trades=%s | bt lock=%s | "
            "spread=%.3f%% | listed=%sd",
            index,
            coin["symbol"],
            coin["score"],
            coin["price"],
            coin["volume_24h"],
            coin["momentum_6h"] * 100,
            coin["momentum_24h"] * 100,
            coin["backtest_avg_return"] * 100,
            coin["backtest_win_rate"] * 100,
            coin["backtest_trades"],
            coin.get("lock_rule_label", "1%"),
            coin["spread_pct"] * 100,
            int(coin["listing_days"]),
        )


def scan_and_allocate(selector, decision_agent):
    # Run a fresh market scan and compute the best portfolio split for it.
    coins = selector.get_top_coins(
        limit=settings.BACKTEST_CANDIDATE_LIMIT,
        result_limit=settings.MAX_OPEN_POSITIONS,
    )
    if not coins:
        logger.warning("No qualified coins found in this scan")
        return [], None

    log_selected_coins(coins)

    recommended = decision_agent.recommend(coins)
    if not recommended:
        logger.warning("No allocation profile qualified for live execution")
        return coins, None

    logger.info(
        "Decision-agent allocation: %s | est avg=%+.2f%% | "
        "est total=%+.2f%% | est win=%.0f%% | deployed=$%.2f | idle cash=$%.2f",
        recommended["name"],
        recommended["portfolio_avg_return"] * 100,
        recommended["portfolio_total_return"] * 100,
        recommended["portfolio_win_rate"] * 100,
        recommended.get("deployed_capital", 0.0),
        recommended.get("cash_reserve_usdt", 0.0),
    )

    for allocation in recommended["allocations"]:
        logger.info(
            "Allocation %02d. %s -> %.1f%% | $%.2f | bt avg=%+.2f%% | bt win=%.0f%% | lock=%s",
            allocation["allocation_rank"],
            allocation["symbol"],
            allocation["allocation_pct"],
            allocation["amount_usdt"],
            allocation["backtest_avg_return"] * 100,
            allocation["backtest_win_rate"] * 100,
            allocation.get("lock_rule_label", "1%"),
        )

    return coins, recommended


def get_research_positions(agent):
    """Build a small, current portfolio snapshot for the advisory prompt."""
    snapshots = []
    for symbol, position in agent.positions.items():
        try:
            snapshot = agent.get_position_snapshot(symbol)
        except Exception:
            logger.exception("Could not fetch %s for the LLM research snapshot", symbol)
            continue
        snapshots.append({
            "symbol": symbol,
            "entry_price": position["entry_price"],
            "pnl_pct": round(snapshot["pnl_pct"] * 100, 3),
            "highest_pnl_pct": round(position["highest_pnl_pct"] * 100, 3),
            "locked_profit_pct": (
                None
                if position["locked_profit_pct"] is None
                else round(position["locked_profit_pct"] * 100, 3)
            ),
        })
    return snapshots


def main():
   
    args = parse_args()
    paper_trading = args.trade_mode == "paper"

    # Build the selector service that knows how to scan Binance markets.
    selector = CoinSelector()
    decision_agent = PortfolioDecisionAgent(total_capital_usdt=TOTAL_CAPITAL)
    research_agent = LLMResearchAgent()
    agent = PortfolioAgent(
        monitor_interval_seconds=MONITOR_INTERVAL_SECONDS,
        paper_trading=paper_trading,
        starting_capital_usdt=TOTAL_CAPITAL,
    )
    logger.info("Starting market scan in %s mode", args.trade_mode)

    while True:
        try:
            coins, recommended = scan_and_allocate(selector, decision_agent)
        except Exception as exc:
            logger.warning("Initial market scan failed: %s; retrying in 60 seconds", exc)
            time.sleep(60)
            continue

        if recommended:
            agent.update_latest_recommendations([coin["symbol"] for coin in coins])
            break

        logger.warning("No initial basket qualified; retrying in 60 seconds")
        time.sleep(60)

    try:
        decision_agent.run_minute(agent, recommended)
    except Exception:
        logger.exception("Initial basket execution failed; the decision loop will retry")
    if not agent.has_open_positions() and not agent.has_trading_capital():
        logger.warning("Trading halted because no capital remains to invest")
        return

    next_monitor_at = time.time()
    next_scan_at = time.time() + SCAN_INTERVAL_SECONDS
    next_research_at = time.time() if research_agent.enabled else None
    scan_future = None
    research_future = None

    # Scanning downloads many candle series. Keep it off the monitoring path
    # so a slow API response cannot delay the one-minute risk checks.
    with (
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="market-scan") as scan_executor,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm-research") as research_executor,
    ):
        while True:
            if not agent.has_open_positions() and not agent.has_trading_capital():
                logger.warning("Trading halted because no capital remains to invest")
                break

            now = time.time()

            if now >= next_monitor_at:
                try:
                    if agent.has_open_positions():
                        agent.monitor_positions_once()
                    # This uses cached rankings and live position/cash state only.
                    # It is intentionally fast and does not start a full scan.
                    decision_agent.run_minute(agent, recommended)
                except Exception:
                    logger.exception("Monitoring tick failed; retrying next minute")
                next_monitor_at = now + MONITOR_INTERVAL_SECONDS

            if scan_future is not None and scan_future.done():
                try:
                    coins, scanned_recommendation = scan_future.result()
                except Exception as exc:
                    logger.warning(
                        "Market rescan failed: %s; keeping the previous basket", exc
                    )
                else:
                    if scanned_recommendation:
                        try:
                            recommended = scanned_recommendation
                            agent.update_latest_recommendations(
                                [coin["symbol"] for coin in coins]
                            )
                            decision_agent.run_minute(agent, recommended)
                        except Exception:
                            logger.exception(
                                "Could not apply the latest recommendation; "
                                "keeping the current portfolio"
                            )
                finally:
                    scan_future = None

            if research_future is not None and research_future.done():
                try:
                    advisory = research_future.result()
                except Exception:
                    logger.exception("LLM research task failed; keeping prior advisory")
                else:
                    if advisory:
                        decision_agent.update_llm_advisory(advisory)
                finally:
                    research_future = None

            if now >= next_scan_at and scan_future is None:
                logger.info("Starting background market rescan for portfolio refill")
                scan_future = scan_executor.submit(
                    scan_and_allocate, selector, decision_agent
                )
                next_scan_at = now + SCAN_INTERVAL_SECONDS

            if (
                next_research_at is not None
                and now >= next_research_at
                and research_future is None
            ):
                logger.info("Starting ten-minute LLM market research")
                research_future = research_executor.submit(
                    research_agent.analyze,
                    list(coins),
                    get_research_positions(agent),
                )
                next_research_at = now + settings.LLM_ADVISOR_INTERVAL_SECONDS

            time.sleep(1)


if __name__ == "__main__":
    # Run the scan when this file is executed directly.
    main()
