"""Durable spot execution. Unknown outcomes block orders rather than retrying.

Exchange access is injected in tests. Constructing this agent never submits an
order; bootstrap/reconciliation precedes every monitor or strategy cycle.
"""

import hashlib
import logging
import math
import time
import uuid

from app.core.config import settings
from app.services.trading_agent import PortfolioAgent

logger = logging.getLogger(__name__)
TERMINAL = {"closed", "canceled", "expired", "rejected"}


class LiveSafetyError(RuntimeError):
    pass


class LivePortfolioAgent(PortfolioAgent):
    def __init__(self, store, service=None):
        super().__init__(paper_trading=False, starting_capital_usdt=settings.LIVE_CAPITAL_LIMIT_USDT)
        if service is not None:
            self.binance = service
        self.exchange = self.binance.exchange
        self.store = store
        self.daily_loss_limit_pct = settings.LIVE_DAILY_LOSS_LIMIT_PCT
        self.blocked = False
        self.initialized = False
        self.last_stage = "initialization"
        self.identity = hashlib.sha256((self.exchange.apiKey or "").encode()).hexdigest()

    def export_state(self):
        payload = super().export_state()
        payload.update({
            "mode": self.store.mode,
            "account_fingerprint": self.identity,
            "capital_limit_usdt": settings.LIVE_CAPITAL_LIMIT_USDT,
            "starting_capital_usdt": self.starting_capital_usdt,
            "initialized": self.initialized,
        })
        return payload

    def checkpoint(self, applied=()):
        self.store.save(self.export_state(), applied_orders=applied)

    def _gate(self, entry=False):
        self.store.assert_lock_for_live()
        if not settings.LIVE_TRADING_ENABLED:
            raise LiveSafetyError("LIVE_TRADING_ENABLED is false; use --trade-mode reconcile")
        if self.blocked:
            raise LiveSafetyError("Live cycle is blocked after an unresolved error")
        if entry and (not settings.LIVE_ENTRIES_ENABLED or self.is_daily_trading_halted()):
            raise LiveSafetyError("New live entries are paused; protective exits remain enabled")

    def validate_configuration(self, read_only=False):
        if not self.exchange.apiKey or not self.exchange.secret:
            raise LiveSafetyError("Binance credentials are missing for the selected environment")
        expected_mode = "testnet" if settings.BINANCE_TESTNET else "live"
        if self.store.mode != expected_mode:
            raise LiveSafetyError("Exchange environment and database scope do not match")
        if not read_only:
            for name in ("LIVE_CAPITAL_LIMIT_USDT", "LIVE_MAX_POSITION_USDT"):
                value = getattr(settings, name)
                if not math.isfinite(value) or value <= 0:
                    raise LiveSafetyError(f"Set an explicit positive {name}")
        for name in ("LIVE_DAILY_LOSS_LIMIT_PCT", "LIVE_FEE_RESERVE_PCT", "STOP_LOSS_PCT", "LIVE_STOP_LIMIT_GAP_PCT", "LIVE_MAX_SPREAD_PCT"):
            value = getattr(settings, name)
            if not math.isfinite(value) or not 0 < value < 1:
                raise LiveSafetyError(f"{name} must be between 0 and 1")

    def _restore(self, read_only=False):
        payload = self.store.load()
        if payload:
            if payload.get("mode") != self.store.mode or payload.get("account_fingerprint") != self.identity:
                raise LiveSafetyError("Saved state belongs to another mode or API key; reconciliation/migration required")
            if not read_only and payload.get("capital_limit_usdt") != settings.LIVE_CAPITAL_LIMIT_USDT:
                raise LiveSafetyError("Live capital cap changed; reconcile the saved budget before changing it")
            self.restore_state(payload)
            self.starting_capital_usdt = payload["starting_capital_usdt"]
            self.initialized = payload.get("initialized", False)

    def bootstrap(self, read_only=False):
        self.last_stage = "validating local settings"
        self.validate_configuration(read_only=read_only)
        self.last_stage = "restoring saved state"
        self._restore(read_only=read_only)
        self.last_stage = "loading Binance spot markets"
        logger.info("Account check: loading Binance spot markets")
        self.exchange.load_markets()
        if not read_only:
            self._gate()
            # Resolve journalled outcomes before trusting the portfolio snapshot.
            self.recover_orders()
        self.last_stage = "reading Binance Spot wallet balances"
        logger.info("Account check: reading Binance Spot wallet balances")
        balances = self.exchange.fetch_balance()
        self.last_stage = "reading Binance open orders"
        logger.info("Account check: reading Binance open orders")
        open_orders = self.exchange.fetch_open_orders()
        self.last_stage = "reconciling wallet balances and saved holdings"
        known = {item["client_id"] for item in self.store.journal_orders()}
        managed_bases = {symbol.split("/")[0] for symbol in self.positions}
        for order in open_orders:
            if order.get("clientOrderId") not in known and (
                order.get("symbol", "").split("/")[0] in managed_bases
                or order.get("side") == "buy"
            ):
                raise LiveSafetyError("Unmanaged open order conflicts with bot funds; reconcile it first")
        actual_cash = float(balances.get("free", {}).get("USDT", 0) or 0)
        if not self.initialized:
            budget = settings.LIVE_CAPITAL_LIMIT_USDT
            self.available_usdt = min(budget, actual_cash) if math.isfinite(budget) and budget > 0 else 0.0
            if not read_only and self.available_usdt < settings.MIN_TRADE_SIZE_USDT:
                raise LiveSafetyError("Insufficient free USDT for the configured bot budget")
            self.daily_equity_baseline_usdt = self.available_usdt
            self.starting_capital_usdt = self.available_usdt
            self.initialized = True
        elif actual_cash + 1e-6 < self.available_usdt:
            raise LiveSafetyError("Exchange USDT is below the bot ledger; manual trades or transfers require reconciliation")
        for symbol, position in self.positions.items():
            actual = float(balances.get("total", {}).get(symbol.split("/")[0], 0) or 0)
            if actual + 1e-10 < position["quantity"]:
                raise LiveSafetyError(f"Exchange balance is below bot holdings for {symbol}; entries blocked")
        logger.info(
            "%s account check | free USDT=%.2f | bot cash=%.2f | managed positions=%s | open orders=%s",
            self.store.mode, actual_cash, self.available_usdt, len(self.positions), len(open_orders),
        )
        pending = [item for item in self.store.journal_orders() if not item.get("applied")]
        if read_only:
            logger.info("Read-only check: no orders submitted or canceled; journal records awaiting reconciliation=%s", len(pending))
            if actual_cash < settings.MIN_TRADE_SIZE_USDT:
                logger.warning("Free USDT is below the bot's %.2f USDT purchase minimum; this account check does not enable trading", settings.MIN_TRADE_SIZE_USDT)
            return
        self.checkpoint()
        self._evaluate_daily_loss_guard(self._get_portfolio_summary()["equity_usdt"])
        for symbol in list(self.positions):
            self.ensure_protection(symbol)

    def _query(self, item):
        # A timeout/not-found is deliberately unresolved. Never blindly resubmit.
        order = self.exchange.fetch_order(None, item["symbol"], {"origClientOrderId": item["client_id"]})
        if not order.get("id"):
            raise LiveSafetyError(f"Missing exchange order ID for journal {item['client_id']}")
        if order.get("clientOrderId") != item["client_id"] or order.get("side") != item["side"]:
            raise LiveSafetyError("Exchange response does not match the journalled order")
        if order.get("status") not in TERMINAL | {"open"} or order.get("filled") is None:
            raise LiveSafetyError("Exchange order status or filled quantity is unknown")
        if not math.isfinite(float(order["filled"])) or float(order["filled"]) < 0:
            raise LiveSafetyError("Invalid exchange filled quantity")
        if order["status"] == "closed" and float(order["filled"]) == 0:
            raise LiveSafetyError("Closed order has no confirmed fill")
        return order

    def _prepare_amount(self, symbol, amount, price):
        market = self.exchange.market(symbol)
        if not market.get("spot") or market.get("quote") != "USDT" or not market.get("active", True):
            raise LiveSafetyError("Only active USDT spot markets are allowed")
        quantity = float(self.exchange.amount_to_precision(symbol, amount))
        if quantity <= 0:
            raise LiveSafetyError("Order rounds to zero")
        for field, value in (("amount", quantity), ("cost", quantity * price)):
            limits = market.get("limits", {}).get(field, {})
            if limits.get("min") is not None and value < limits["min"]:
                raise LiveSafetyError(f"Order is below the exchange minimum {field}")
            if limits.get("max") is not None and value > limits["max"]:
                raise LiveSafetyError(f"Order exceeds the exchange maximum {field}")
        return quantity

    def _submit(self, symbol, side, quantity, kind="market", stop=None, metadata=None):
        self._gate(entry=side == "buy")
        client_id = "trado-" + uuid.uuid4().hex[:24]
        item = {
            "client_id": client_id, "symbol": symbol, "side": side,
            "quantity": quantity, "kind": kind, "stop": stop,
            "metadata": metadata or {}, "applied": False,
        }
        # Commit intent before sending anything to Binance.
        self.store.put_order(item)
        params = {"newClientOrderId": client_id}
        price = None
        order_type = "market"
        if kind == "protection":
            order_type = "STOP_LOSS_LIMIT"
            params.update({"stopPrice": stop, "timeInForce": "GTC"})
            price = float(self.exchange.price_to_precision(symbol, stop * (1 - settings.LIVE_STOP_LIMIT_GAP_PCT)))
        if side == "buy":
            # Spend a fixed quote amount, rather than an estimated base amount
            # whose market execution price could exceed the bot's cash cap.
            params["quoteOrderQty"] = self.exchange.cost_to_precision(symbol, metadata["quote_budget"])
        try:
            self.store.assert_lock_for_live()
            self.exchange.create_order(symbol, order_type, side, None if side == "buy" else quantity, price, params)
            self._resolve(item)
        except Exception:
            self.blocked = True
            logger.error("Live order unresolved; no retry will be submitted. Reconcile journal %s", client_id)
            raise
        return item

    def _confirmed_fill(self, item, order):
        filled = float(order.get("filled") or 0)
        if filled <= 0:
            return None
        # Query by exchange order ID to avoid counting fee/fees/raw_fills twice.
        trades = self.exchange.fetch_my_trades(item["symbol"], limit=1000, params={"orderId": order["id"]})
        trades = [trade for trade in trades if str(trade.get("order")) == str(order["id"])]
        total = sum(float(trade.get("amount") or 0) for trade in trades)
        if abs(total - filled) > max(1e-10, filled * 1e-8):
            raise LiveSafetyError("Trade history does not yet confirm every fill; reconciliation required")
        gross = 0.0
        fees = 0.0
        base_fee = 0.0
        base = item["symbol"].split("/")[0]
        for trade in trades:
            cost = trade.get("cost")
            if cost is None or float(cost) <= 0:
                raise LiveSafetyError("Missing confirmed trade cost")
            gross += float(cost)
            entries = self.binance._extract_fee_entries(trade)
            if not entries:
                raise LiveSafetyError("Commission is not confirmed; reconciliation required")
            for fee in entries:
                if fee.get("cost") is None or fee.get("currency") is None:
                    raise LiveSafetyError("Commission asset or amount is missing")
                if float(fee["cost"]) < 0:
                    raise LiveSafetyError("Commission rebates require manual reconciliation")
                if fee["currency"] == base:
                    base_fee += float(fee["cost"])
                else:
                    fees += self.binance._convert_fee_to_usdt(fee["currency"], fee["cost"], trade["price"], item["symbol"])
        return {"quantity": filled, "gross": gross, "fees": fees, "base_fee": base_fee}

    def _resolve(self, item, cancel=False):
        order = self._query(item)
        status = order.get("status")
        if status not in TERMINAL and (cancel or float(order.get("filled") or 0) > 0):
            self._gate()
            self.exchange.cancel_order(order["id"], item["symbol"])
            order = self._query(item)
            status = order.get("status")
        if status not in TERMINAL:
            if item["kind"] != "protection":
                raise LiveSafetyError("Market order is still pending; next cycle will reconcile it")
            if item["symbol"] not in self.positions:
                raise LiveSafetyError("Protective order has no managed position")
            self.positions[item["symbol"]]["protection"] = item["client_id"]
            self.positions[item["symbol"]]["protection_stop"] = item["stop"]
            self.checkpoint()
            return
        fill = self._confirmed_fill(item, order)
        if fill:
            if item["side"] == "buy":
                self._apply_buy(item, fill)
            else:
                self._apply_sell(item, fill)
        if item["kind"] == "protection" and item["symbol"] in self.positions:
            self.positions[item["symbol"]].pop("protection", None)
            self.positions[item["symbol"]].pop("protection_stop", None)
        # Portfolio effects and journal acknowledgement commit atomically.
        self.checkpoint(applied=[item["client_id"]])

    def _apply_buy(self, item, fill):
        symbol = item["symbol"]
        if symbol in self.positions:
            raise LiveSafetyError("Unexpected second buy for an existing managed position")
        quantity = fill["quantity"] - fill["base_fee"]
        if quantity <= 0:
            raise LiveSafetyError("No purchased quantity remains after commission")
        metadata = item["metadata"]
        position = self._build_position(
            symbol, fill["gross"], fill["gross"] / fill["quantity"], quantity,
            metadata.get("profit_lock_start_pct") or self.default_profit_lock_start_pct,
            metadata.get("profit_lock_floor_pct") or self.default_profit_lock_floor_pct,
            0.0, metadata.get("lock_rule_label") or "live net profit floor",
        )
        # Base-asset commission is already reflected in the reduced quantity.
        position["buy_fee_usdt"] = fill["fees"]
        self.positions[symbol] = position
        self.available_usdt -= fill["gross"] + fill["fees"]
        if self.available_usdt < -1e-6:
            # Preserve/protect the actual fill even if commissions exceed the
            # reserved budget. Pausing entries must not discard real holdings.
            self.daily_trading_halted = True
            logger.error("Actual commission exceeded the reserved bot cash; new entries halted")

    def _apply_sell(self, item, fill):
        symbol = item["symbol"]
        position = self.positions.get(symbol)
        consumed = fill["quantity"] + fill["base_fee"]
        if position is None or consumed > position["quantity"] + 1e-10:
            raise LiveSafetyError("Confirmed sale does not match managed quantity")
        fraction = min(consumed / position["quantity"], 1.0)
        proceeds = fill["gross"] - fill["fees"]
        pnl = proceeds - fraction * (position["initial_usdt"] + position["buy_fee_usdt"])
        self.available_usdt += proceeds
        self._record_closed_trade(symbol, pnl)
        position["quantity"] -= consumed
        position["initial_usdt"] *= 1 - fraction
        position["buy_fee_usdt"] *= 1 - fraction
        if position["quantity"] <= 1e-10:
            del self.positions[symbol]
            self.reentry_not_before[symbol] = time.time() + self.reentry_cooldown_seconds

    def recover_orders(self):
        for item in self.store.journal_orders():
            if not item.get("applied"):
                self._resolve(item)

    def _get_affordable_buy_amount(self, requested_usdt_amount):
        allocated = sum(position["initial_usdt"] + position["buy_fee_usdt"] for position in self.positions.values())
        return max(0.0, min(
            requested_usdt_amount,
            settings.LIVE_MAX_POSITION_USDT / (1 + settings.LIVE_FEE_RESERVE_PCT),
            (settings.LIVE_CAPITAL_LIMIT_USDT - allocated) / (1 + settings.LIVE_FEE_RESERVE_PCT),
            (self.available_usdt or 0.0) / (1 + settings.LIVE_FEE_RESERVE_PCT),
        ))

    def buy_position(self, symbol, usdt_amount, **metadata):
        self._evaluate_daily_loss_guard(self._get_portfolio_summary()["equity_usdt"])
        self._gate(entry=True)
        if symbol in self.positions or len(self.positions) >= self.max_open_positions:
            return None
        if time.time() < self.reentry_not_before.get(symbol, 0):
            return None
        amount = self._get_affordable_buy_amount(usdt_amount)
        if amount < self.min_trade_size_usdt:
            return None
        market = self.exchange.market(symbol)
        if "STOP_LOSS_LIMIT" not in market.get("info", {}).get("orderTypes", []):
            raise LiveSafetyError("Market does not advertise the required native stop-loss-limit order")
        if any(order.get("symbol") == symbol for order in self.exchange.fetch_open_orders()):
            raise LiveSafetyError("Existing orders on this symbol require reconciliation")
        ticker = self.exchange.fetch_ticker(symbol)
        bid, ask = ticker.get("bid"), ticker.get("ask")
        if not bid or not ask or ask < bid or (ask - bid) / ask > settings.LIVE_MAX_SPREAD_PCT:
            raise LiveSafetyError("Missing or excessive bid/ask spread")
        quantity = self._prepare_amount(symbol, amount / ask, ask)
        # A minimum-size buy may be impossible to protect after commission and
        # a lower stop price. Validate that prospective exit before entering.
        self._prepare_amount(
            symbol, quantity * (1 - settings.LIVE_FEE_RESERVE_PCT),
            ask * (1 - settings.STOP_LOSS_PCT) * (1 - settings.LIVE_STOP_LIMIT_GAP_PCT),
        )
        free_cash = float(self.exchange.fetch_balance().get("free", {}).get("USDT", 0) or 0)
        if quantity * ask * (1 + settings.LIVE_FEE_RESERVE_PCT) > free_cash:
            raise LiveSafetyError("Exchange cash changed before entry")
        metadata["quote_budget"] = amount
        self._submit(symbol, "buy", quantity, metadata=metadata)
        if symbol in self.positions:
            self.ensure_protection(symbol)
        return self.positions.get(symbol)

    def ensure_protection(self, symbol):
        position = self.positions[symbol]
        target_return = position.get("locked_profit_pct")
        if target_return is None:
            target_return = -settings.STOP_LOSS_PCT
        basis = position["initial_usdt"] + position["buy_fee_usdt"]
        stop = float(self.exchange.price_to_precision(
            symbol, basis * (1 + target_return) / (position["quantity"] * (1 - settings.LIVE_FEE_RESERVE_PCT))
        ))
        existing = position.get("protection")
        if existing and position.get("protection_stop", 0) >= stop:
            return
        if existing:
            self._cancel_protection(symbol)
            if symbol not in self.positions:
                return
            position = self.positions[symbol]
        price = self.binance.get_price(symbol)
        if stop >= price:
            self._close_position(symbol, "protection_threshold_already_crossed", price)
            return
        quantity = self._prepare_amount(symbol, position["quantity"], stop * (1 - settings.LIVE_STOP_LIMIT_GAP_PCT))
        self._submit(symbol, "sell", quantity, kind="protection", stop=stop)

    def _cancel_protection(self, symbol):
        client_id = self.positions[symbol].get("protection")
        if not client_id:
            return
        item = next(item for item in self.store.journal_orders() if item["client_id"] == client_id)
        self._resolve(item, cancel=True)

    def _close_position(self, symbol, exit_reason, price):
        self._gate()
        self._cancel_protection(symbol)
        if symbol not in self.positions:
            return {"symbol": symbol, "exit_reason": "exchange_protection_filled"}
        quantity = self._prepare_amount(symbol, self.positions[symbol]["quantity"], price)
        free = float(self.exchange.fetch_balance().get("free", {}).get(symbol.split("/")[0], 0) or 0)
        if quantity > free + 1e-10:
            raise LiveSafetyError("Managed coins are not free to sell; reconciliation required")
        self._submit(symbol, "sell", quantity, metadata={"exit_reason": exit_reason})
        if symbol in self.positions:
            self.ensure_protection(symbol)
        return {"symbol": symbol, "exit_reason": exit_reason}

    def partial_close_position(self, *args, **kwargs):
        raise LiveSafetyError("Live discretionary partial rotation is disabled")

    def monitor_positions_once(self):
        self._gate()
        super().monitor_positions_once()
        for symbol in list(self.positions):
            self.ensure_protection(symbol)
        self.checkpoint()

    def refill_from_recommendations(self, desired_allocations):
        if not settings.LIVE_ENTRIES_ENABLED:
            return []
        opened = []
        scale = 1.0
        if not self.positions:
            planned = sum(allocation["amount_usdt"] for allocation in desired_allocations)
            if planned > 0:
                scale = min(self.available_usdt, settings.LIVE_CAPITAL_LIMIT_USDT) / (
                    planned * (1 + settings.LIVE_FEE_RESERVE_PCT)
                )
        for allocation in desired_allocations:
            position = self.buy_position(
                allocation["symbol"], allocation["amount_usdt"] * scale,
                profit_lock_start_pct=allocation.get("profit_lock_start_pct"),
                profit_lock_floor_pct=allocation.get("profit_lock_floor_pct"),
                lock_rule_label=allocation.get("lock_rule_label"),
            )
            if position:
                opened.append(position)
        return opened
