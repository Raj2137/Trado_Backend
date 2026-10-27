"""Optional real PostgreSQL tests; never use the deployment DATABASE_URL."""

import os
import unittest
import uuid

from sqlalchemy import select

from app.services.portfolio_state import PortfolioStateStore, PaperPerformanceRecord


@unittest.skipUnless(os.getenv("TEST_POSTGRES_URL"), "TEST_POSTGRES_URL is not configured")
class PostgresStateTests(unittest.TestCase):
    def setUp(self):
        account = "integration-" + uuid.uuid4().hex
        self.first = PortfolioStateStore(os.environ["TEST_POSTGRES_URL"], "live", account)
        self.second = PortfolioStateStore(os.environ["TEST_POSTGRES_URL"], "live", account)
        self.paper = PortfolioStateStore(os.environ["TEST_POSTGRES_URL"], "paper", account)
        for store in (self.first, self.second, self.paper):
            self.addCleanup(store.engine.dispose)

    def test_cross_connection_lock_and_atomic_order_application(self):
        client_id = uuid.uuid4().hex
        with self.first.cycle_lock():
            with self.assertRaisesRegex(RuntimeError, "Another cycle"):
                with self.second.cycle_lock():
                    self.fail("Concurrent writer acquired the PostgreSQL lock")
            self.first.put_order({"client_id": client_id, "applied": False})
            self.first.save({"available_usdt": 100})
            # Updating one journal and then failing another must roll back the
            # entire transaction, including the portfolio snapshot.
            with self.assertRaisesRegex(RuntimeError, "journal scope mismatch"):
                self.first.save({"available_usdt": 1}, [client_id, "missing-order"])
            self.assertEqual(self.second.load(), {"available_usdt": 100})
            self.assertFalse(self.second.journal_orders()[0]["applied"])
            self.first.save({"available_usdt": 99}, [client_id])
            self.assertEqual(self.second.load(), {"available_usdt": 99})
            self.assertTrue(self.second.journal_orders()[0]["applied"])
            self.assertIsNone(self.paper.load())
        with self.second.cycle_lock():
            self.second.save({"available_usdt": 98})
        # Reporting and journal acknowledgements share the merged save API,
        # while paper history stays separate from the live snapshot.
        with self.paper.cycle_lock():
            self.paper.save({"available_usdt": 105}, performance={"test_return_pct": 5})
        with self.paper.session_factory() as session:
            rows = session.scalars(select(PaperPerformanceRecord)).all()
            self.assertTrue(rows)
        self.assertEqual(self.second.load(), {"available_usdt": 98})


if __name__ == "__main__":
    unittest.main()
