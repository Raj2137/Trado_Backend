import argparse
import logging
import time

from app.core.config import settings
from app.services.coin_selector import CoinSelector
from app.services.portfolio_allocator import PortfolioAllocator
from app.services.trading_agent import PortfolioAgent

# Configure application-wide logging for the scan output.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Reuse a module logger so log messages are easy to trace.
logger = logging.getLogger(__name__)

TOTAL_CAPITAL = 100
MONITOR_INTERVAL_SECONDS = 60
SCAN_INTERVAL_SECONDS = 300


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


def scan_and_allocate(selector):
    # Run a fresh market scan and compute the best portfolio split for it.
    coins = selector.get_top_coins(limit=10)
    if not coins:
        logger.warning("No qualified coins found in this scan")
        return [], None

    log_selected_coins(coins)

    allocation_analysis = PortfolioAllocator.allocate(
        coins,
        total_capital=TOTAL_CAPITAL,
    )
    recommended = allocation_analysis["recommended"] if allocation_analysis else None
    if not recommended:
        logger.warning("No allocation profile qualified for live execution")
        return coins, None

    logger.info(
        "Recommended allocation profile: %s | est avg=%+.2f%% | "
        "est total=%+.2f%% | est win=%.0f%% | deployed=$%.2f | cash reserve=$%.2f",
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


def main():
   
    args = parse_args()
    paper_trading = args.trade_mode == "paper"

    # Build the selector service that knows how to scan Binance markets.
    selector = CoinSelector()
    agent = PortfolioAgent(
        monitor_interval_seconds=MONITOR_INTERVAL_SECONDS,
        paper_trading=paper_trading,
        starting_capital_usdt=TOTAL_CAPITAL,
    )
    logger.info("Starting market scan in %s mode", args.trade_mode)

    coins, recommended = scan_and_allocate(selector)
    agent.update_latest_recommendations([coin["symbol"] for coin in coins])
    if not recommended:
        return

    agent.buy_allocations(recommended["allocations"])
    if not agent.has_open_positions() and not agent.has_trading_capital():
        logger.warning("Trading halted because no capital remains to invest")
        return

    next_monitor_at = time.time()
    next_scan_at = time.time() + SCAN_INTERVAL_SECONDS
    while True:
        if not agent.has_open_positions() and not agent.has_trading_capital():
            logger.warning("Trading halted because no capital remains to invest")
            break

        now = time.time()

        if agent.has_open_positions() and now >= next_monitor_at:
            agent.monitor_positions_once()
            next_monitor_at = now + MONITOR_INTERVAL_SECONDS

        if now >= next_scan_at:
            logger.info("Starting independent rescan for portfolio refill")
            coins, recommended = scan_and_allocate(selector)
            agent.update_latest_recommendations([coin["symbol"] for coin in coins])
            next_scan_at = now + SCAN_INTERVAL_SECONDS

            if recommended:
                if agent.has_open_positions():
                    agent.rotate_positions(recommended["allocations"])
                else:
                    if not agent.has_trading_capital():
                        logger.warning("Trading halted because no capital remains to invest")
                        break
                    logger.info("No open positions remain, opening the new recommended basket")
                    agent.buy_allocations(recommended["allocations"])

        time.sleep(1)


if __name__ == "__main__":
    # Run the scan when this file is executed directly.
    main()
