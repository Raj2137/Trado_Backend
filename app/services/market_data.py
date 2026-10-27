"""Unauthenticated Binance spot data for scanning and paper execution."""

import ccxt


def create_public_spot_exchange():
    exchange = ccxt.binance({
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {
            "defaultType": "spot",
            "fetchMarkets": {"types": ["spot"]},
            "fetchCurrencies": False,
            "fetchMargins": False,
        },
    })
    # Binance's documented market-data-only service supports exchangeInfo,
    # tickers and klines without account credentials or trading endpoints.
    exchange.urls["api"]["public"] = "https://data-api.binance.vision/api/v3"
    return exchange
