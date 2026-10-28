"""Exercise the installed CCXT client without sending exchange requests."""

import unittest
from unittest.mock import patch

from app.core.config import settings
from app.services.binance_service import BinanceService


class BinanceClientTests(unittest.TestCase):
    def test_account_wide_open_orders_acknowledges_ccxt_warning(self):
        with patch.multiple(settings, BINANCE_TESTNET=False, BINANCE_API_KEY='test-only-key', BINANCE_SECRET_KEY='test-only-secret'):
            exchange = BinanceService(paper_trading=False).exchange
        self.assertTrue(exchange.enableRateLimit)
        exchange.set_markets([])
        with patch.object(exchange, 'privateGetOpenOrders', return_value=[]) as endpoint:
            self.assertEqual(exchange.fetch_open_orders(), [])
        endpoint.assert_called_once()
        self.assertNotIn('symbol', endpoint.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
