import logging
import time

from app.core.config import settings
from app.services.binance_service import BinanceService

logger = logging.getLogger(__name__)


class PortfolioAgent:
    def __init__(
        self,
        monitor_interval_seconds=60,
        paper_trading=None,
        starting_capital_usdt=None,
    ):
        self.total_profit_usdt = 0.0
        self.total_loss_usdt = 0.0
        self.realized_pnl_usdt = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.realized_pnl_by_symbol = {}
        self.starting_capital_usdt = (
            None if starting_capital_usdt is None else float(starting_capital_usdt)
        )
        self.available_usdt = self.starting_capital_usdt
        self.capital_epsilon_usdt = 1e-8
        self.capital_reserve_usdt = (
            0.0
            if self.starting_capital_usdt is None
            else self.starting_capital_usdt * (1 - settings.INITIAL_DEPLOYMENT_RATIO)
        )
        # Use one Binance service for the whole portfolio run.
        self.binance = BinanceService(paper_trading=paper_trading)
        self.paper_trading = self.binance.paper_trading

        # Poll open positions once per minute by default.
        self.monitor_interval_seconds = monitor_interval_seconds

        # Exit a losing trade at -2% net PnL.
        self.stop_loss_pct = -settings.STOP_LOSS_PCT
        self.weak_position_exit_pct = -0.01  # -1%
        self.recommendation_exit_top_n = 10

        # At +2%, protect at least +1%. Thereafter, exit after a 2 percentage
        # point drawdown from the highest net PnL reached by the position.
        self.default_profit_lock_start_pct = settings.PROFIT_LOCK_START_PCT
        self.default_profit_lock_floor_pct = settings.PROFIT_LOCK_FLOOR_PCT
        self.default_profit_lock_drawdown_pct = settings.PROFIT_LOCK_DRAWDOWN_PCT
        self.max_open_positions = settings.MAX_OPEN_POSITIONS
        self.min_trade_size_usdt = settings.MIN_TRADE_SIZE_USDT
        self.reentry_cooldown_seconds = settings.REENTRY_COOLDOWN_SECONDS

        # Track all live positions keyed by symbol.
        self.positions = {}
        self.latest_recommended_symbols = set()
        self.reentry_not_before = {}

    def has_open_positions(self):
        # Let the caller know whether there is anything left to manage.
        return bool(self.positions)

    def update_latest_recommendations(self, symbols):
        # Keep the latest top-ranked symbols so weak holdings can be compared
        # against the current shortlist instead of stale entries.
        self.latest_recommended_symbols = set(symbols[:self.recommendation_exit_top_n])

    def _capital_tracking_enabled(self):
        return self.available_usdt is not None

    def has_trading_capital(self):
        if not self._capital_tracking_enabled():
            return True
        return self.available_usdt > self.capital_reserve_usdt + self.capital_epsilon_usdt

    def has_investable_capital(self):
        if not self._capital_tracking_enabled():
            return True
        return (
            self.available_usdt - self.capital_reserve_usdt
            >= self.min_trade_size_usdt
        )

    def _get_affordable_buy_amount(self, requested_usdt_amount):
        if requested_usdt_amount <= 0:
            return 0.0

        if not self._capital_tracking_enabled():
            return requested_usdt_amount

        spendable_usdt = self.available_usdt - self.capital_reserve_usdt
        if spendable_usdt <= self.capital_epsilon_usdt:
            return 0.0

        if self.paper_trading:
            fee_rate = self.binance.paper_trading_fee_rate
            max_gross_affordable = spendable_usdt / (1 + fee_rate)
        else:
            max_gross_affordable = spendable_usdt

        return min(requested_usdt_amount, max_gross_affordable)

    def _decrease_available_usdt(self, amount_usdt):
        if not self._capital_tracking_enabled():
            return

        self.available_usdt = max(self.available_usdt - amount_usdt, 0.0)

    def _increase_available_usdt(self, amount_usdt):
        if not self._capital_tracking_enabled():
            return

        self.available_usdt += amount_usdt

    def _build_position(
        self,
        symbol,
        usdt_amount,
        entry_price,
        quantity,
        profit_lock_start_pct,
        profit_lock_floor_pct,
        profit_lock_drawdown_pct,
        lock_rule_label,
        # entry_time
    ):
        # Store the state required to manage the live trailing lock.
        return {
            "symbol": symbol,
            "entry_price": entry_price,
            "quantity": quantity,
            "initial_usdt": usdt_amount,
            "buy_fee_usdt": 0.0,
            "total_fees_usdt": 0.0,
            "highest_pnl_pct": 0.0,
            "locked_profit_pct": None,
            "profit_lock_start_pct": profit_lock_start_pct,
            "profit_lock_floor_pct": profit_lock_floor_pct,
            "profit_lock_drawdown_pct": profit_lock_drawdown_pct,
            "lock_rule_label": lock_rule_label,
            # "entry_time": time.time(),
        }

    def _update_profit_lock(self, position, pnl_pct):
        # Each position can use its own profile, but all profiles implement a
        # drawdown-based lock rather than selling immediately at the trigger.
        profit_lock_start_pct = position.get(
            "profit_lock_start_pct",
            self.default_profit_lock_start_pct,
        )
        profit_lock_floor_pct = position.get(
            "profit_lock_floor_pct",
            self.default_profit_lock_floor_pct,
        )
        profit_lock_drawdown_pct = position.get(
            "profit_lock_drawdown_pct",
            self.default_profit_lock_drawdown_pct,
        )
        if pnl_pct < profit_lock_start_pct:
            return

        new_lock_pct = max(
            profit_lock_floor_pct,
            pnl_pct - profit_lock_drawdown_pct,
        )
        current_lock_pct = position["locked_profit_pct"] or 0.0

        if new_lock_pct > current_lock_pct:
            position["locked_profit_pct"] = new_lock_pct
            logger.info(
                "Profit lock raised for %s -> %.2f%% | rule=%s",
                position["symbol"],
                new_lock_pct * 100,
                position.get("lock_rule_label", "default"),
            )

    def buy_position(
        self,
        symbol,
        usdt_amount,
        profit_lock_start_pct=None,
        profit_lock_floor_pct=None,
        profit_lock_drawdown_pct=None,
        lock_rule_label=None,
    ):
        if time.time() < self.reentry_not_before.get(symbol, 0):
            logger.info("Skipping %s because its re-entry cooldown is active", symbol)
            return None
        # Skip tiny allocations that are not worth sending to the exchange.
        affordable_usdt_amount = self._get_affordable_buy_amount(usdt_amount)
        if affordable_usdt_amount <= 0:
            logger.warning("Skipping %s because allocation is %.2f USDT", symbol, usdt_amount)
            return None
        if affordable_usdt_amount < usdt_amount:
            logger.info(
                "Reducing %s allocation from %.2f USDT to %.2f USDT due to remaining capital",
                symbol,
                usdt_amount,
                affordable_usdt_amount,
            )

        requested_price = self.binance.get_price(symbol)
        requested_quantity = affordable_usdt_amount / requested_price
        order = self.binance.buy(symbol, requested_quantity, price=requested_price)

        # Use the actual fill data returned by the exchange whenever it is available.
        filled_quantity = order.get("quantity", requested_quantity)
        entry_price = order.get("price", requested_price)
        gross_cost_usdt = order.get("gross_notional_usdt", affordable_usdt_amount)
        buy_fee_usdt = order.get("fee_usdt", 0.0)
        total_spent_usdt = order.get("net_cost_usdt", gross_cost_usdt + buy_fee_usdt)
        position_lock_start_pct = (
            self.default_profit_lock_start_pct
            if profit_lock_start_pct is None
            else profit_lock_start_pct
        )
        position_lock_floor_pct = (
            self.default_profit_lock_floor_pct
            if profit_lock_floor_pct is None
            else profit_lock_floor_pct
        )
        position_lock_drawdown_pct = (
            self.default_profit_lock_drawdown_pct
            if profit_lock_drawdown_pct is None
            else profit_lock_drawdown_pct
        )
        position_lock_rule_label = (
            lock_rule_label
            or (
                f"{position_lock_start_pct * 100:.0f}% trigger / "
                f"{position_lock_drawdown_pct * 100:.0f}% trail / "
                f"{position_lock_floor_pct * 100:.0f}% floor"
            )
        )

        position = self._build_position(
            symbol,
            gross_cost_usdt,
            entry_price,
            filled_quantity,
            position_lock_start_pct,
            position_lock_floor_pct,
            position_lock_drawdown_pct,
            position_lock_rule_label,
        )
        position["buy_fee_usdt"] = buy_fee_usdt
        position["total_fees_usdt"] = position["buy_fee_usdt"]
        self.positions[symbol] = position
        self._decrease_available_usdt(total_spent_usdt)

        if self._capital_tracking_enabled():
            logger.info(
                "BUY %s | allocation=%.2f USDT | filled cost=%.2f | entry=%.6f | qty=%.8f | fee=%.4f | lock_rule=%s | available=%.2f",
                symbol,
                affordable_usdt_amount,
                gross_cost_usdt,
                entry_price,
                filled_quantity,
                position["buy_fee_usdt"],
                position["lock_rule_label"],
                self.available_usdt,
            )
        else:
            logger.info(
                "BUY %s | allocation=%.2f USDT | filled cost=%.2f | entry=%.6f | qty=%.8f | fee=%.4f | lock_rule=%s",
                symbol,
                affordable_usdt_amount,
                gross_cost_usdt,
                entry_price,
                filled_quantity,
                position["buy_fee_usdt"],
                position["lock_rule_label"],
            )
        return position

    def buy_allocations(self, allocations):
        # Open all positions from the recommended portfolio split.
        opened_positions = []
        for allocation in allocations:
            if len(self.positions) >= self.max_open_positions:
                break
            if allocation["symbol"] in self.positions:
                continue
            position = self.buy_position(
                allocation["symbol"],
                allocation["amount_usdt"],
                profit_lock_start_pct=allocation.get("profit_lock_start_pct"),
                profit_lock_floor_pct=allocation.get("profit_lock_floor_pct"),
                profit_lock_drawdown_pct=allocation.get("profit_lock_drawdown_pct"),
                lock_rule_label=allocation.get("lock_rule_label"),
            )
            if position is not None:
                opened_positions.append(position)
        return opened_positions

    def get_position_snapshot(self, symbol):
        # Read the latest value of a live position so replacement decisions can
        # be based on current profit since entry.
        position = self.positions[symbol]
        price = self.binance.get_price(symbol)
        gross_current_value = position["quantity"] * price
        estimated_sell_fee_usdt = self.binance.estimate_fee(gross_current_value)
        net_exit_value = gross_current_value - estimated_sell_fee_usdt
        pnl_usdt = net_exit_value - position["initial_usdt"] - position["buy_fee_usdt"]
        pnl_pct = pnl_usdt / position["initial_usdt"]
        return {
            "symbol": symbol,
            "price": price,
            "gross_current_value": gross_current_value,
            "net_exit_value": net_exit_value,
            "estimated_sell_fee_usdt": estimated_sell_fee_usdt,
            "pnl_usdt": pnl_usdt,
            "pnl_pct": pnl_pct,
        }

    def _get_portfolio_summary(self):
        # Recalculate the portfolio state from the currently open positions so
        # closed trades are not double-counted in both cash and open equity.
        total_gross_current_value = 0.0
        total_estimated_sell_fees_usdt = 0.0
        total_unrealized_profit_usdt = 0.0
        total_unrealized_loss_usdt = 0.0
        total_unrealized_pnl_usdt = 0.0

        for symbol in self.positions:
            snapshot = self.get_position_snapshot(symbol)
            total_gross_current_value += snapshot["gross_current_value"]
            total_estimated_sell_fees_usdt += snapshot["estimated_sell_fee_usdt"]
            total_unrealized_pnl_usdt += snapshot["pnl_usdt"]

            if snapshot["pnl_usdt"] >= 0:
                total_unrealized_profit_usdt += snapshot["pnl_usdt"]
            else:
                total_unrealized_loss_usdt += abs(snapshot["pnl_usdt"])

        net_open_value = total_gross_current_value - total_estimated_sell_fees_usdt
        available_usdt = self.available_usdt if self._capital_tracking_enabled() else 0.0
        total_equity_usdt = available_usdt + net_open_value

        return {
            "open_positions": len(self.positions),
            "unrealized_profit_usdt": total_unrealized_profit_usdt,
            "unrealized_loss_usdt": total_unrealized_loss_usdt,
            "net_unrealized_pnl_usdt": total_unrealized_pnl_usdt,
            "available_usdt": available_usdt,
            "equity_usdt": total_equity_usdt,
        }

    def get_ranked_positions_by_pnl(self):
        # Worst current holdings come first so the rescan loop can replace them.
        snapshots = [
            self.get_position_snapshot(symbol)
            for symbol in self.positions
        ]
        return sorted(snapshots, key=lambda snapshot: snapshot["pnl_pct"])

    def _record_closed_trade(self, symbol, pnl_usdt):
        # Keep a realized PnL ledger for both portfolio-wide and per-coin logs.
        symbol_totals = self.realized_pnl_by_symbol.setdefault(
            symbol,
            {
                "profit_usdt": 0.0,
                "loss_usdt": 0.0,
                "net_pnl_usdt": 0.0,
                "trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
            },
        )

        self.total_trades += 1
        symbol_totals["trades"] += 1
        self.realized_pnl_usdt += pnl_usdt
        symbol_totals["net_pnl_usdt"] += pnl_usdt

        if pnl_usdt >= 0:
            self.total_profit_usdt += pnl_usdt
            self.winning_trades += 1
            symbol_totals["profit_usdt"] += pnl_usdt
            symbol_totals["winning_trades"] += 1
            return

        realized_loss_usdt = abs(pnl_usdt)
        self.total_loss_usdt += realized_loss_usdt
        self.losing_trades += 1
        symbol_totals["loss_usdt"] += realized_loss_usdt
        symbol_totals["losing_trades"] += 1

    def _get_symbol_realized_summary(self, symbol):
        return self.realized_pnl_by_symbol.get(
            symbol,
            {
                "profit_usdt": 0.0,
                "loss_usdt": 0.0,
                "net_pnl_usdt": 0.0,
                "trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
            },
        )

    def _log_symbol_realized_summary(self, symbol):
        totals = self._get_symbol_realized_summary(symbol)
        logger.info(
            "COIN REALIZED PNL %s | profit=%.2f USDT | loss=%.2f USDT | net=%+.2f USDT | closed_trades=%s",
            symbol,
            totals["profit_usdt"],
            totals["loss_usdt"],
            totals["net_pnl_usdt"],
            totals["trades"],
        )

    def _log_realized_pnl_summary(self):
        total_coins = len(self.realized_pnl_by_symbol)
        logger.info(
            "REALIZED SUMMARY | total_profit=%.2f USDT | total_loss=%.2f USDT | net=%+.2f USDT | closed_trades=%s | wins=%s | losses=%s | total_coins=%s",
            self.total_profit_usdt,
            self.total_loss_usdt,
            self.realized_pnl_usdt,
            self.total_trades,
            self.winning_trades,
            self.losing_trades,
            total_coins,
        )

        if not self.realized_pnl_by_symbol:
            return

        coin_summaries = []
        for symbol in sorted(self.realized_pnl_by_symbol):
            totals = self.realized_pnl_by_symbol[symbol]
            coin_summaries.append(
                (
                    f"{symbol}: profit={totals['profit_usdt']:.2f} USDT, "
                    f"loss={totals['loss_usdt']:.2f} USDT, "
                    f"net={totals['net_pnl_usdt']:+.2f} USDT, "
                    f"trades={totals['trades']}"
                )
            )

        logger.info("COIN-WISE REALIZED SUMMARY | %s", " | ".join(coin_summaries))

    def _close_position(self, symbol, exit_reason, price):
        # Send the sell order and remove the position from live tracking.
        position = self.positions[symbol]
        order = self.binance.sell(symbol, position["quantity"], price=price)

        gross_current_value = order.get("gross_notional_usdt", position["quantity"] * price)
        sell_fee_usdt = order.get(
            "fee_usdt",
            self.binance.estimate_fee(gross_current_value),
        )
        net_exit_value = order.get(
            "net_proceeds_usdt",
            gross_current_value - sell_fee_usdt,
        )
        total_fees_usdt = position["buy_fee_usdt"] + sell_fee_usdt
        pnl_usdt = net_exit_value - position["initial_usdt"] - position["buy_fee_usdt"]
        pnl_pct = pnl_usdt / position["initial_usdt"]
        position["total_fees_usdt"] = total_fees_usdt

        logger.info(
            "SELL %s | reason=%s | exit=%.6f | gross=%.2f | net=%.2f | fees=%.4f | pnl=%+.2f (%+.2f%%)",
            symbol,
            exit_reason,
            order.get("price", price),
            gross_current_value,
            net_exit_value,
            total_fees_usdt,
            pnl_usdt,
            pnl_pct * 100,
        )

        del self.positions[symbol]
        self.reentry_not_before[symbol] = (
            time.time() + self.reentry_cooldown_seconds
        )
        self._increase_available_usdt(net_exit_value)
        self._record_closed_trade(symbol, pnl_usdt)
        self._log_realized_pnl_summary()
        if self._capital_tracking_enabled():
            logger.info("AVAILABLE CAPITAL | available=%.2f USDT", self.available_usdt)

        return {
            "symbol": symbol,
            "exit_reason": exit_reason,
            "price": order.get("price", price),
            "gross_current_value": gross_current_value,
            "current_value": net_exit_value,
            "sell_fee_usdt": sell_fee_usdt,
            "total_fees_usdt": total_fees_usdt,
            "pnl_usdt": pnl_usdt,
            "pnl_pct": pnl_pct,
        }

    def refill_from_recommendations(self, desired_allocations):
        """Use available cash to fill empty slots from the latest ranked basket.

        This method deliberately does not sell a healthy holding. Sales are
        governed by the stop-loss and trailing-profit rules in the one-minute
        monitor. That prevents frequent rescans from causing churn.
        """
        opened = []
        for allocation in desired_allocations:
            if len(self.positions) >= self.max_open_positions:
                break
            if allocation["symbol"] in self.positions:
                continue
            if not self.has_investable_capital():
                break

            position = self.buy_position(
                allocation["symbol"],
                allocation["amount_usdt"],
                profit_lock_start_pct=allocation.get("profit_lock_start_pct"),
                profit_lock_floor_pct=allocation.get("profit_lock_floor_pct"),
                profit_lock_drawdown_pct=allocation.get("profit_lock_drawdown_pct"),
                lock_rule_label=allocation.get("lock_rule_label"),
            )
            if position is not None:
                opened.append(position)
        return opened

    def partial_close_position(self, symbol, fraction, reason):
        """
        Sell a fraction of an existing position (0 < fraction <= 1)
        """

        if symbol not in self.positions:
            return None

        position = self.positions[symbol]

        if fraction <= 0:
            return None

        fraction = min(fraction, 1.0)

        sell_quantity = position["quantity"] * fraction

        price = self.binance.get_price(symbol)
        order = self.binance.sell(symbol, sell_quantity, price=price)

        gross_value = order.get("gross_notional_usdt", sell_quantity * price)
        sell_fee = order.get("fee_usdt", self.binance.estimate_fee(gross_value))
        net_value = order.get("net_proceeds_usdt", gross_value - sell_fee)

        # Reduce position size
        position["quantity"] -= sell_quantity

        # Proportional cost reduction
        proportion = sell_quantity / (sell_quantity + position["quantity"] + 1e-8)
        position["initial_usdt"] *= (1 - proportion)

        position["total_fees_usdt"] += sell_fee

        self._increase_available_usdt(net_value)

        logger.info(
            "PARTIAL SELL %s | fraction=%.2f | qty=%.6f | net=%.2f | reason=%s",
            symbol,
            fraction,
            sell_quantity,
            net_value,
            reason,
        )

        # If nearly empty → fully remove
        if position["quantity"] <= 1e-8:
            del self.positions[symbol]

        return net_value
    def close_position(self, symbol, exit_reason):
        # Public close helper used by the monitoring and rescan refill logic.
        snapshot = self.get_position_snapshot(symbol)
        return self._close_position(symbol, exit_reason, snapshot["price"])

    def rotate_positions(self, desired_allocations):
        """
        Smart rotation logic (SAFE VERSION)

        Goals:
        - Use free cash first
        - Protect strong winners
        - Replace weak positions
        - Avoid unnecessary churn
        """

        if not desired_allocations:
            return []

        refills = []

        # Map desired allocations by symbol for quick lookup
        desired_map = {a["symbol"]: a for a in desired_allocations}
        current_symbols = set(self.positions.keys())

        # -------------------------------
        # STEP 1: Try to BUY using FREE CASH
        # -------------------------------
        for allocation in desired_allocations:
            symbol = allocation["symbol"]

            if symbol in current_symbols:
                continue

            if not self.has_trading_capital():
                break

            new_position = self.buy_position(
                symbol,
                allocation["amount_usdt"],
                profit_lock_start_pct=allocation.get("profit_lock_start_pct"),
                profit_lock_floor_pct=allocation.get("profit_lock_floor_pct"),
                profit_lock_drawdown_pct=allocation.get("profit_lock_drawdown_pct"),
                lock_rule_label=allocation.get("lock_rule_label"),
            )

            if new_position:
                refills.append({
                    "bought_symbol": symbol,
                    "buy_amount": new_position["initial_usdt"],
                })

        # -------------------------------
        # STEP 2: Evaluate rotation (ONLY if no free cash)
        # -------------------------------
        if self.has_trading_capital():
            return refills  # cash handled everything

        ranked_positions = self.get_ranked_positions_by_pnl()

        for allocation in desired_allocations:
            new_symbol = allocation["symbol"]

            if new_symbol in current_symbols:
                continue

            if not ranked_positions:
                break

            # Worst performing position
            weakest = ranked_positions[0]
            weak_symbol = weakest["symbol"]
            weak_pnl = weakest["pnl_pct"]

            # -------------------------------
            # RULE 1: Protect strong winners
            # -------------------------------
            if weak_pnl > 0.01:
                logger.info(
                    "Skipping rotation: %s is strong (pnl=%.2f%%)",
                    weak_symbol,
                    weak_pnl * 100,
                )
                continue

            # -------------------------------
            # RULE 2: Only rotate if weak
            # -------------------------------
            if weak_pnl > -0.003:
                logger.info(
                    "Skipping rotation: %s not weak enough (pnl=%.2f%%)",
                    weak_symbol,
                    weak_pnl * 100,
                )
                continue

            # -------------------------------
            # RULE 3: Rotate weak → new
            # -------------------------------
            logger.info(
                "ROTATION: Selling %s (pnl=%.2f%%) → Buying %s",
                weak_symbol,
                weak_pnl * 100,
                new_symbol,
            )

            # -------------------------------
            # PARTIAL ROTATION DECISION
            # -------------------------------
            if weak_pnl > 0.01:
                rotate_fraction = 0.0  # strong → ignore
            elif weak_pnl > 0:
                rotate_fraction = 0.2  # neutral → small
            elif weak_pnl > -0.005:
                rotate_fraction = 0.5  # weak → medium
            else:
                rotate_fraction = 1.0  # very weak → full

            if rotate_fraction == 0:
                logger.info("Skipping rotation: %s is strong", weak_symbol)
                continue

            logger.info(
                "ROTATION (partial): %s | pnl=%.2f%% | fraction=%.2f → %s",
                weak_symbol,
                weak_pnl * 100,
                rotate_fraction,
                new_symbol,
            )

            if rotate_fraction < 1.0:
                closed = self.partial_close_position(
                    weak_symbol,
                    rotate_fraction,
                    "partial_rotation"
                )
            else:
                closed = self.close_position(
                    weak_symbol,
                    "full_rotation"
                )

            if not closed:
                continue

            new_position = self.buy_position(
                new_symbol,
                allocation["amount_usdt"],
                profit_lock_start_pct=allocation.get("profit_lock_start_pct"),
                profit_lock_floor_pct=allocation.get("profit_lock_floor_pct"),
                profit_lock_drawdown_pct=allocation.get("profit_lock_drawdown_pct"),
                lock_rule_label=allocation.get("lock_rule_label"),
            )

            if new_position:
                refills.append({
                    "sold_symbol": weak_symbol,
                    "bought_symbol": new_symbol,
                    "buy_amount": new_position["initial_usdt"],
                })

                # Update state
                current_symbols.remove(weak_symbol)
                current_symbols.add(new_symbol)
                ranked_positions = self.get_ranked_positions_by_pnl()

        if not refills:
            logger.info("No rotations or refills performed")

        return refills

    def monitor_positions_once(self):
        
        
        # Process one monitoring tick so the caller can interleave 1-minute
        # monitoring with the independent rescan loop.
        if not self.positions:
            logger.warning("No open positions to monitor")
            return

        logger.info("Monitoring tick for %s open positions", len(self.positions))

        for symbol in list(self.positions.keys()):
            position = self.positions[symbol]
            price = self.binance.get_price(symbol)

            
            gross_current_value = position["quantity"] * price
            estimated_sell_fee_usdt = self.binance.estimate_fee(gross_current_value)
            pnl_usdt = (
                gross_current_value
                - estimated_sell_fee_usdt
                - position["initial_usdt"]
                - position["buy_fee_usdt"]
            )
            pnl_pct = pnl_usdt / position["initial_usdt"]  # ✅ NET PnL
            net_pnl_pct = pnl_usdt / position["initial_usdt"]

            if pnl_pct > position["highest_pnl_pct"]:
                position["highest_pnl_pct"] = pnl_pct

            self._update_profit_lock(position, position["highest_pnl_pct"])

            logger.info(
                "MONITOR %s | invested=%.2f USDT | price=%.6f | gross pnl=%+.2f%% | net pnl=%+.2f USDT (%+.2f%%) | est fee=%.4f | lock=%s | rule=%s",
                symbol,
                position["initial_usdt"],
                price,
                pnl_pct * 100,
                pnl_usdt,
                net_pnl_pct * 100,
                position["buy_fee_usdt"] + estimated_sell_fee_usdt,
                (
                    f"{position['locked_profit_pct'] * 100:.2f}%"
                    if position["locked_profit_pct"] is not None
                    else "none"
                ),
                position.get("lock_rule_label", "default"),
            )
            self._log_symbol_realized_summary(symbol)

            if (
                self.latest_recommended_symbols
                and symbol not in self.latest_recommended_symbols
                and pnl_pct <= self.weak_position_exit_pct
            ):
                logger.warning(
                    "Weak holding exit for %s | gross pnl=%+.2f%% | threshold=%+.2f%% | not in latest top %s",
                    symbol,
                    pnl_pct * 100,
                    self.weak_position_exit_pct * 100,
                    self.recommendation_exit_top_n,
                )
                self._close_position(symbol, "weak_not_top_recommended", price)
                continue

            if pnl_pct <= self.stop_loss_pct:
                logger.warning(
                    "Stop loss hit for %s | pnl=%+.2f%% | threshold=%+.2f%%",
                    symbol,
                    pnl_pct * 100,
                    self.stop_loss_pct * 100,
                )
                self._close_position(symbol, "stop_loss", price)
                continue

            locked_profit_pct = position["locked_profit_pct"]
            if locked_profit_pct is not None and pnl_pct <= locked_profit_pct:
                logger.info(
                    "Locked profit exit for %s | pnl=%+.2f%% | locked=%+.2f%%",
                    symbol,
                    pnl_pct * 100,
                    locked_profit_pct * 100,
                )
                self._close_position(symbol, "profit_lock", price)
                continue

            logger.debug(
                "%s remains open | unrealized pnl=%+.2f USDT",
                symbol,
                pnl_usdt,
            )

        portfolio_summary = self._get_portfolio_summary()
        if self.starting_capital_usdt is not None:
            portfolio_net_pnl_usdt = (
                portfolio_summary["equity_usdt"] - self.starting_capital_usdt
            )
            portfolio_net_pnl_pct = portfolio_net_pnl_usdt / self.starting_capital_usdt
            logger.info(
                "PORTFOLIO SUMMARY | open=%s | unrealized_profit=%.2f USDT | unrealized_loss=%.2f USDT | net_unrealized=%+.2f USDT | realized_net=%+.2f USDT | available=%.2f USDT | equity=%.2f USDT | total_pnl=%+.2f USDT (%+.2f%%)",
                portfolio_summary["open_positions"],
                portfolio_summary["unrealized_profit_usdt"],
                portfolio_summary["unrealized_loss_usdt"],
                portfolio_summary["net_unrealized_pnl_usdt"],
                self.realized_pnl_usdt,
                portfolio_summary["available_usdt"],
                portfolio_summary["equity_usdt"],
                portfolio_net_pnl_usdt,
                portfolio_net_pnl_pct * 100,
            )
        else:
            logger.info(
                "PORTFOLIO SUMMARY | open=%s | unrealized_profit=%.2f USDT | unrealized_loss=%.2f USDT | net_unrealized=%+.2f USDT | realized_net=%+.2f USDT | equity=%.2f USDT",
                portfolio_summary["open_positions"],
                portfolio_summary["unrealized_profit_usdt"],
                portfolio_summary["unrealized_loss_usdt"],
                portfolio_summary["net_unrealized_pnl_usdt"],
                self.realized_pnl_usdt,
                portfolio_summary["equity_usdt"],
            )

    def monitor_positions(self):
        # Optional helper to monitor continuously when no separate rescan loop
        # is needed by the caller.
        if not self.positions:
            logger.warning("No open positions to monitor")
            return

        logger.info(
            "Monitoring %s open positions every %s seconds",
            len(self.positions),
            self.monitor_interval_seconds,
        )

        while self.positions:
            self.monitor_positions_once()
            if self.positions:
                time.sleep(self.monitor_interval_seconds)
