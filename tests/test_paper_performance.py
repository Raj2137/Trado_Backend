import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select

from app.core.config import settings
from app.services.paper_performance import start_evaluation, record_performance
from app.services.portfolio_state import PortfolioStateStore, PaperPerformanceRecord
from app.services.decision_agent import PortfolioDecisionAgent
from app import run_cycle


class Agent:
    realized_pnl_usdt = 0
    total_trades = 0
    daily_trading_halted = False

    def __init__(self, equity=100):
        self.equity = equity

    def _get_portfolio_summary(self):
        return {
            "equity_usdt": self.equity, "available_usdt": 0,
            "open_positions": 1, "net_unrealized_pnl_usdt": self.equity - 100,
        }


class PaperPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

    def test_target_is_net_equity_and_not_claimed_realized(self):
        agent = Agent()
        evaluation = start_evaluation(agent, now=self.now)
        agent.equity = 105
        report = record_performance(agent, evaluation, True, self.now + timedelta(hours=24))
        self.assertTrue(report["target_met_now"])
        self.assertEqual(report["test_return_pct"], 5)
        self.assertEqual(report["lifetime_realized_pnl_usdt"], 0)
        self.assertEqual(report["observation"], "24 hours complete; continue to 48")

    def test_target_crossing_and_drawdown_persist_after_retracement(self):
        agent = Agent()
        evaluation = start_evaluation(agent, now=self.now)
        agent.equity = 106
        record_performance(agent, evaluation, True, self.now + timedelta(hours=1))
        agent.equity = 98
        restored = start_evaluation(agent, json.loads(json.dumps(evaluation)))
        report = record_performance(agent, restored, False, self.now + timedelta(hours=48))
        self.assertFalse(report["target_met_now"])
        self.assertIsNotNone(report["target_first_reached_at"])
        self.assertEqual(report["test_return_pct"], -2)
        self.assertAlmostEqual(report["max_drawdown_pct"], 8 / 106)
        self.assertEqual(report["failed_cycles"], 1)
        self.assertEqual(report["observation"], "48 hours complete")

    def test_existing_portfolio_is_the_starting_baseline(self):
        agent = Agent(112)
        evaluation = start_evaluation(agent, now=self.now)
        report = record_performance(agent, evaluation, True, self.now)
        self.assertEqual(report["test_return_pct"], 0)
        self.assertAlmostEqual(report["target_equity_usdt"], 117.6)

    def test_two_paper_runs_restore_window_and_write_history_and_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            url = 'sqlite:///' + str(Path(folder) / 'state.db')
            summary = Path(folder) / 'summary.md'
            with patch.multiple(settings, DATABASE_URL=url, TOTAL_CAPITAL_USDT=100):
                with patch('sys.argv', ['run_cycle', '--trade-mode', 'paper']):
                    with patch.object(run_cycle, 'scan_and_allocate', return_value=([], None)):
                        with patch.dict('os.environ', {'GITHUB_STEP_SUMMARY': str(summary)}):
                            run_cycle.main()
                            run_cycle.main()
            store = PortfolioStateStore(url)
            try:
                self.assertEqual(store.load()['paper_evaluation']['cycles'], 2)
                with store.session_factory() as session:
                    history = list(session.scalars(select(PaperPerformanceRecord)))
                    self.assertEqual(len(history), 2)
                self.assertIn('5% total-return target', summary.read_text())
                # A report serialization failure must roll back the portfolio
                # change too, preserving an internally consistent history.
                before = store.load()
                with self.assertRaises(TypeError):
                    store.save({'available_usdt': 1}, performance={'bad': object()})
                self.assertEqual(store.load(), before)
            finally:
                store.engine.dispose()

    def test_allocator_accepts_decision_agent_arguments_and_preserves_weights(self):
        coins = [
            {'symbol': symbol, 'backtest_score': score, 'backtest_avg_return': 0.01,
             'backtest_total_return': 0.02, 'backtest_win_rate': 0.6}
            for symbol, score in [('BTC/USDT', 2), ('ETH/USDT', 1)]
        ]
        with patch.multiple(settings, INITIAL_DEPLOYMENT_RATIO=1.0, MAX_OPEN_POSITIONS=2):
            profile = PortfolioDecisionAgent(100).recommend(coins)
        self.assertEqual([item['amount_usdt'] for item in profile['allocations']], [50, 50])

    def test_hosted_job_cannot_silently_start_fresh_sqlite_portfolio(self):
        with patch.multiple(settings, REQUIRE_PERSISTENT_DATABASE=True, DATABASE_URL='sqlite:///:memory:'):
            with patch('sys.argv', ['run_cycle', '--trade-mode', 'paper']):
                with patch.object(run_cycle, 'PortfolioStateStore') as store:
                    with self.assertRaisesRegex(RuntimeError, 'SQLite is not allowed'):
                        run_cycle.main()
                    store.assert_not_called()


if __name__ == '__main__':
    unittest.main()
