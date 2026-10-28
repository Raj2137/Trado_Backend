"""Failure/recovery tests. All exchange operations are in-memory fakes."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ccxt

from app.core.config import settings
from app.services.binance_service import BinanceService
from app.services.live_trading import LivePortfolioAgent, LiveSafetyError
from app.services.portfolio_state import PortfolioStateStore, PortfolioStateRecord


class FakeExchange:
    apiKey = "fake-key"
    secret = "fake-secret"

    def __init__(self):
        self.orders = {}
        self.trades = {}
        self.submissions = []
        self.cash = 1000.0
        self.coins = 0.0
        self.fee_asset = "USDT"
        self.timeout_after_accept = False
        self.reject_before_accept = False
        self.sell_fraction = 1.0
        self.stop_supported = True
        self.cancel_fills = False

    def market(self, symbol):
        return {
            "symbol": symbol, "base": "BTC", "quote": "USDT", "spot": True, "active": True,
            "limits": {"amount": {"min": 0.000001}, "cost": {"min": 5}},
            "info": {"orderTypes": ["STOP_LOSS_LIMIT"] if self.stop_supported else []},
        }

    def load_markets(self):
        return {"BTC/USDT": self.market("BTC/USDT")}

    def fetch_balance(self):
        return {"free": {"USDT": self.cash, "BTC": self.coins}, "total": {"USDT": self.cash, "BTC": self.coins}}

    def fetch_open_orders(self):
        return [copy.deepcopy(order) for order in self.orders.values() if order["status"] == "open"]

    def fetch_ticker(self, symbol):
        return {"last": 100.0, "bid": 99.9, "ask": 100.0}

    def amount_to_precision(self, symbol, value):
        return str(int(value * 1e6) / 1e6)

    def price_to_precision(self, symbol, value):
        return f"{value:.4f}"

    def cost_to_precision(self, symbol, value):
        return str(int(value * 100) / 100)

    def create_order(self, symbol, kind, side, quantity, price, params):
        self.submissions.append((symbol, kind, side, quantity, price, params))
        if self.reject_before_accept:
            raise ccxt.InvalidOrder("simulated rejection")
        client_id = params["newClientOrderId"]
        order = {
            "id": str(len(self.orders) + 1), "clientOrderId": client_id,
            "symbol": symbol, "side": side, "status": "open", "filled": 0,
            "amount": quantity, "type": kind,
        }
        self.orders[client_id] = order
        if kind == "market":
            quantity = float(params["quoteOrderQty"]) / 100 if side == "buy" else quantity
            self.fill(client_id, quantity if side == "buy" else quantity * self.sell_fraction)
            order["status"] = "closed" if side == "buy" or self.sell_fraction == 1 else "canceled"
        if self.timeout_after_accept:
            self.timeout_after_accept = False
            raise ccxt.RequestTimeout("simulated accepted-but-timed-out request")
        return copy.deepcopy(order)

    def fill(self, client_id, quantity):
        order = self.orders[client_id]
        cost = quantity * 100
        fee = {"currency": self.fee_asset, "cost": quantity * 0.001 if self.fee_asset == "BTC" else cost * 0.001}
        order["filled"] = quantity
        self.trades[order["id"]] = [{
            "order": order["id"], "amount": quantity, "price": 100.0, "cost": cost,
            # Three representations of the same commission must count once.
            "fee": fee, "fees": [fee], "info": {"fills": [{"commission": fee["cost"], "commissionAsset": fee["currency"]}]},
        }]
        if order["side"] == "buy":
            self.cash -= cost + (fee["cost"] if self.fee_asset == "USDT" else 0)
            self.coins += quantity - (fee["cost"] if self.fee_asset == "BTC" else 0)
        else:
            self.cash += cost - (fee["cost"] if self.fee_asset == "USDT" else 0)
            self.coins -= quantity + (fee["cost"] if self.fee_asset == "BTC" else 0)

    def fetch_order(self, order_id, symbol, params):
        client_id = params["origClientOrderId"]
        if client_id not in self.orders:
            raise ccxt.OrderNotFound("simulated unknown outcome")
        return copy.deepcopy(self.orders[client_id])

    def fetch_my_trades(self, symbol, limit, params):
        return copy.deepcopy(self.trades.get(str(params["orderId"]), []))

    def cancel_order(self, order_id, symbol):
        order = next(order for order in self.orders.values() if order["id"] == order_id)
        if self.cancel_fills:
            self.cancel_fills = False
            self.fill(order["clientOrderId"], order["amount"])
            order["status"] = "closed"
        else:
            order["status"] = "canceled"


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.url = "sqlite:///" + str(Path(self.temp.name) / "state.db")
        self.config = patch.multiple(settings,
            BINANCE_TESTNET=False, LIVE_TRADING_ENABLED=True, LIVE_ENTRIES_ENABLED=True,
            LIVE_CAPITAL_LIMIT_USDT=100.0, LIVE_MAX_POSITION_USDT=50.0,
            LIVE_DAILY_LOSS_LIMIT_PCT=0.02, LIVE_FEE_RESERVE_PCT=0.005,
        )
        self.config.start()
        self.addCleanup(self.config.stop)
        self.exchange = FakeExchange()
        self.service = BinanceService(paper_trading=True)
        self.service.paper_trading = False
        self.service.exchange = self.exchange
        self.store = self.make_store()

    def make_store(self, mode="live", account="default"):
        store = PortfolioStateStore(self.url, mode=mode, account_id=account)
        self.addCleanup(store.engine.dispose)
        return store

    def agent(self):
        return LivePortfolioAgent(self.store, self.service)

    def test_read_only_never_submits_or_saves(self):
        with patch.object(settings, "LIVE_TRADING_ENABLED", False):
            with self.store.cycle_lock():
                self.agent().bootstrap(read_only=True)
        self.assertEqual(self.exchange.submissions, [])
        self.assertIsNone(self.store.load())

    def test_live_disabled_prevents_all_submissions(self):
        with patch.object(settings, "LIVE_TRADING_ENABLED", False):
            with self.store.cycle_lock(), self.assertRaises(LiveSafetyError):
                self.agent().bootstrap()
        self.assertEqual(self.exchange.submissions, [])

    def test_explicit_cap_required(self):
        with patch.object(settings, "LIVE_CAPITAL_LIMIT_USDT", 0):
            with self.assertRaises(LiveSafetyError):
                self.agent().validate_configuration()

    def test_read_only_reports_low_balance_without_budget_or_orders(self):
        self.exchange.cash = 1.5
        with patch.multiple(settings, LIVE_CAPITAL_LIMIT_USDT=0, LIVE_MAX_POSITION_USDT=0, LIVE_TRADING_ENABLED=False):
            with self.store.cycle_lock():
                with self.assertLogs('app.services.live_trading', level='INFO') as logs:
                    self.agent().bootstrap(read_only=True)
        self.assertTrue(any('free USDT=1.50' in line for line in logs.output))
        self.assertTrue(any('purchase minimum' in line for line in logs.output))
        self.assertFalse(self.exchange.submissions)
        self.assertIsNone(self.store.load())

    def test_low_balance_still_cannot_initialize_live_trading(self):
        self.exchange.cash = 1.5
        with self.store.cycle_lock():
            with self.assertRaisesRegex(LiveSafetyError, 'Insufficient free USDT'):
                self.agent().bootstrap()
        self.assertFalse(self.exchange.submissions)
        self.assertIsNone(self.store.load())

    def test_paper_legacy_state_is_never_imported_into_live(self):
        with self.store.session_factory() as session:
            session.add(PortfolioStateRecord(id=1, payload='{"available_usdt": 321}', updated_at="test"))
            session.commit()
        self.assertIsNone(self.store.load())
        self.assertEqual(self.make_store("paper").load()["available_usdt"], 321)
        self.assertIsNone(self.make_store("testnet").load())
        self.assertIsNone(self.make_store("paper", "other").load())

    def test_two_process_writers_cannot_share_portfolio_lock(self):
        second = self.make_store()
        with self.store.cycle_lock():
            with self.assertRaises(RuntimeError):
                with second.cycle_lock():
                    self.fail("Second portfolio writer acquired the lock")

    def test_live_save_without_lock_rejected(self):
        with self.assertRaises(RuntimeError):
            self.store.save({})

    def test_capped_buy_confirmed_fee_and_native_stop(self):
        with self.store.cycle_lock():
            agent = self.agent()
            agent.bootstrap()
            position = agent.buy_position("BTC/USDT", 1000)
            gross = position["initial_usdt"]
            self.assertLessEqual(gross * 1.005, 50.0)
            self.assertAlmostEqual(position["buy_fee_usdt"], gross * 0.001)
            self.assertAlmostEqual(agent.available_usdt, 100 - gross * 1.001)
            self.assertTrue(position["protection"])
            self.assertIsNone(self.exchange.submissions[0][3])
            self.assertIn("quoteOrderQty", self.exchange.submissions[0][5])
            self.assertEqual(self.exchange.submissions[1][1], "STOP_LOSS_LIMIT")

    def test_base_asset_buy_fee_reduces_sellable_quantity(self):
        self.exchange.fee_asset = "BTC"
        with self.store.cycle_lock():
            agent = self.agent()
            agent.bootstrap()
            position = agent.buy_position("BTC/USDT", 40)
            self.assertAlmostEqual(position["quantity"], 0.4 * 0.999)
            self.assertEqual(position["buy_fee_usdt"], 0)
            self.assertAlmostEqual(agent.available_usdt, 60)

    def test_timeout_after_accepted_buy_recovers_once(self):
        with self.store.cycle_lock():
            agent = self.agent()
            agent.bootstrap()
            self.exchange.timeout_after_accept = True
            with self.assertRaises(ccxt.RequestTimeout):
                agent.buy_position("BTC/USDT", 40)
            self.assertEqual(self.store.load()["positions"], {})
        with self.store.cycle_lock():
            recovered = self.agent()
            recovered.bootstrap()
            self.assertAlmostEqual(recovered.positions["BTC/USDT"]["quantity"], 0.4)
        with self.store.cycle_lock():
            again = self.agent()
            again.bootstrap()
            self.assertAlmostEqual(again.available_usdt, 59.96)
        self.assertEqual(len([s for s in self.exchange.submissions if s[2] == "buy"]), 1)

    def test_unresolved_not_found_never_resubmits(self):
        with self.store.cycle_lock():
            agent = self.agent()
            agent.bootstrap()
            self.exchange.reject_before_accept = True
            with self.assertRaises(ccxt.InvalidOrder):
                agent.buy_position("BTC/USDT", 40)
        self.exchange.reject_before_accept = False
        with self.store.cycle_lock(), self.assertRaises(ccxt.OrderNotFound):
            self.agent().bootstrap()
        self.assertEqual(len(self.exchange.submissions), 1)

    def test_intent_exists_before_exchange_submission(self):
        original = self.exchange.create_order
        def checked(*args):
            self.assertTrue(any(item["client_id"] == args[-1]["newClientOrderId"] for item in self.store.journal_orders()))
            return original(*args)
        with patch.object(self.exchange, "create_order", side_effect=checked):
            with self.store.cycle_lock():
                agent = self.agent()
                agent.bootstrap()
                agent.buy_position("BTC/USDT", 40)

    def test_partial_sell_retains_cost_basis_and_reprotects(self):
        with self.store.cycle_lock():
            agent = self.agent()
            agent.bootstrap()
            agent.buy_position("BTC/USDT", 40)
            self.exchange.sell_fraction = 0.5
            agent._close_position("BTC/USDT", "test", 100)
            remaining = agent.positions["BTC/USDT"]
            self.assertAlmostEqual(remaining["quantity"], 0.2)
            self.assertAlmostEqual(remaining["initial_usdt"], 20)
            self.assertTrue(remaining["protection"])

    def test_stop_filled_during_cancel_does_not_double_sell(self):
        with self.store.cycle_lock():
            agent = self.agent()
            agent.bootstrap()
            agent.buy_position("BTC/USDT", 40)
            self.exchange.cancel_fills = True
            agent._close_position("BTC/USDT", "test", 100)
            self.assertNotIn("BTC/USDT", agent.positions)
            self.assertEqual(len(self.exchange.submissions), 2)

    def test_manual_account_withdrawal_blocks_entries(self):
        with self.store.cycle_lock():
            agent = self.agent()
            agent.bootstrap()
        self.exchange.cash = 10
        with self.store.cycle_lock(), self.assertRaises(LiveSafetyError):
            self.agent().bootstrap()
        self.assertFalse(self.exchange.submissions)

    def test_changed_account_key_rejected(self):
        with self.store.cycle_lock():
            self.agent().bootstrap()
        self.exchange.apiKey = "another-account-key"
        with self.store.cycle_lock(), self.assertRaises(LiveSafetyError):
            self.agent().bootstrap()

    def test_entry_pause_allows_protection(self):
        with self.store.cycle_lock():
            agent = self.agent()
            agent.bootstrap()
            agent.buy_position("BTC/USDT", 40)
        with patch.object(settings, "LIVE_ENTRIES_ENABLED", False):
            with self.store.cycle_lock():
                agent = self.agent()
                agent.bootstrap()
                with self.assertRaises(LiveSafetyError):
                    agent.buy_position("BTC/USDT", 40)
                agent._close_position("BTC/USDT", "emergency_exit", 100)
                self.assertFalse(agent.positions)

    def test_unsupported_native_stop_blocks_buy(self):
        self.exchange.stop_supported = False
        with self.store.cycle_lock():
            agent = self.agent()
            agent.bootstrap()
            with self.assertRaises(LiveSafetyError):
                agent.buy_position("BTC/USDT", 40)
        self.assertFalse(self.exchange.submissions)

    def test_missing_fill_never_assumed_complete(self):
        with self.store.cycle_lock():
            agent = self.agent()
            agent.bootstrap()
            with patch.object(self.exchange, "fetch_my_trades", return_value=[]):
                with self.assertRaises(LiveSafetyError):
                    agent.buy_position("BTC/USDT", 40)
            self.assertEqual(self.store.load()["positions"], {})
        with self.store.cycle_lock():
            self.agent().bootstrap()
        self.assertEqual(len([s for s in self.exchange.submissions if s[2] == "buy"]), 1)

    def test_database_failure_after_fill_recovers_without_second_buy(self):
        with self.store.cycle_lock():
            agent = self.agent()
            agent.bootstrap()
            with patch.object(self.store, "save", side_effect=RuntimeError("simulated database outage")):
                with self.assertRaises(RuntimeError):
                    agent.buy_position("BTC/USDT", 40)
            self.assertEqual(self.store.load()["positions"], {})
            self.assertFalse(self.store.journal_orders()[0]["applied"])
        with self.store.cycle_lock():
            recovered = self.agent()
            recovered.bootstrap()
            self.assertTrue(recovered.positions["BTC/USDT"]["protection"])
            self.assertAlmostEqual(recovered.available_usdt, 59.96)
        self.assertEqual(len([s for s in self.exchange.submissions if s[2] == "buy"]), 1)

    def test_accepted_stop_timeout_recovers_without_duplicate_stop(self):
        original = self.exchange.create_order
        def timeout_on_stop(*args):
            if args[1] == "STOP_LOSS_LIMIT":
                self.exchange.timeout_after_accept = True
            return original(*args)
        with self.store.cycle_lock():
            agent = self.agent()
            agent.bootstrap()
            with patch.object(self.exchange, "create_order", side_effect=timeout_on_stop):
                with self.assertRaises(ccxt.RequestTimeout):
                    agent.buy_position("BTC/USDT", 40)
            self.assertIn("BTC/USDT", self.store.load()["positions"])
        with self.store.cycle_lock():
            recovered = self.agent()
            recovered.bootstrap()
            self.assertTrue(recovered.positions["BTC/USDT"]["protection"])
        self.assertEqual(len(self.exchange.submissions), 2)

    def test_partial_native_stop_fill_is_reconciled_and_reprotected(self):
        with self.store.cycle_lock():
            agent = self.agent()
            agent.bootstrap()
            agent.buy_position("BTC/USDT", 40)
            original_stop = agent.positions["BTC/USDT"]["protection"]
        self.exchange.fill(original_stop, 0.2)
        with self.store.cycle_lock():
            recovered = self.agent()
            recovered.bootstrap()
            self.assertAlmostEqual(recovered.positions["BTC/USDT"]["quantity"], 0.2)
            self.assertNotEqual(recovered.positions["BTC/USDT"]["protection"], original_stop)
            self.assertEqual(self.exchange.orders[original_stop]["status"], "canceled")

    def test_daily_halt_preserves_exit_capability(self):
        with self.store.cycle_lock():
            agent = self.agent()
            agent.bootstrap()
            agent.buy_position("BTC/USDT", 40)
            agent.daily_trading_halted = True
            with self.assertRaises(LiveSafetyError):
                agent.buy_position("BTC/USDT", 40)
            agent._close_position("BTC/USDT", "test", 100)
            self.assertFalse(agent.positions)

    def test_missing_order_filled_quantity_blocks_recovery(self):
        with self.store.cycle_lock():
            agent = self.agent()
            agent.bootstrap()
            self.exchange.timeout_after_accept = True
            with self.assertRaises(ccxt.RequestTimeout):
                agent.buy_position("BTC/USDT", 40)
        order = next(iter(self.exchange.orders.values()))
        order["filled"] = None
        with self.store.cycle_lock(), self.assertRaises(LiveSafetyError):
            self.agent().bootstrap()
        self.assertFalse(self.store.journal_orders()[0]["applied"])
        self.assertEqual(len(self.exchange.submissions), 1)

    def test_unrelated_existing_coins_are_not_adopted_or_sold(self):
        self.exchange.coins = 5
        with self.store.cycle_lock():
            agent = self.agent()
            agent.bootstrap()
            self.assertFalse(agent.positions)
            agent.buy_position("BTC/USDT", 40)
            agent._close_position("BTC/USDT", "test", 100)
        self.assertAlmostEqual(self.exchange.coins, 5)

    def test_profit_lock_replaces_native_stop_with_higher_stop(self):
        with self.store.cycle_lock():
            agent = self.agent()
            agent.bootstrap()
            agent.buy_position("BTC/USDT", 40)
            first_id = agent.positions["BTC/USDT"]["protection"]
            first_stop = agent.positions["BTC/USDT"]["protection_stop"]
            with patch.object(self.exchange, "fetch_ticker", return_value={"last": 120, "bid": 119.9, "ask": 120}):
                agent.monitor_positions_once()
            self.assertEqual(self.exchange.orders[first_id]["status"], "canceled")
            self.assertGreater(agent.positions["BTC/USDT"]["protection_stop"], first_stop)

    def test_lost_lock_prevents_exchange_submission(self):
        with self.store.cycle_lock():
            agent = self.agent()
            agent.bootstrap()
            with patch.object(self.store, "assert_lock_for_live", side_effect=RuntimeError("lost lock")):
                with self.assertRaises(RuntimeError):
                    agent.buy_position("BTC/USDT", 40)
        self.assertEqual(self.exchange.submissions, [])

    def test_actual_initial_cash_sets_the_performance_baseline(self):
        self.exchange.cash = 20
        with self.store.cycle_lock():
            agent = self.agent()
            agent.bootstrap()
            self.assertEqual(agent.starting_capital_usdt, 20)
            self.assertEqual(agent.daily_equity_baseline_usdt, 20)
            self.assertEqual(agent.export_state()["capital_limit_usdt"], 100)
        with self.store.cycle_lock():
            restored = self.agent()
            restored.bootstrap()
            self.assertEqual(restored.starting_capital_usdt, 20)


if __name__ == "__main__":
    unittest.main()
