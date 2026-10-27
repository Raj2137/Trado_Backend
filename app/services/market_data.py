"""Unauthenticated Binance spot market data for paper mode and scanning."""

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
    exchange.urls["api"]["public"] = "https://data-api.binance.vision/api/v3"
    return exchange
