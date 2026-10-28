import logging

import ccxt
import time


from app.core.config import settings
from app.services.market_data import create_public_spot_exchange

logger = logging.getLogger(__name__)


class BinanceService:
    def __init__(self, paper_trading=None):
        # Let runtime arguments override the env default when needed.
        self.paper_trading = (
            settings.PAPER_TRADING if paper_trading is None else paper_trading
        )
        self.paper_trading_fee_rate = settings.PAPER_TRADING_FEE_RATE
        self.exchange = create_public_spot_exchange() if self.paper_trading else ccxt.binance({
            "apiKey": settings.BINANCE_TESTNET_API_KEY if settings.BINANCE_TESTNET else settings.BINANCE_API_KEY,
            "secret": settings.BINANCE_TESTNET_SECRET_KEY if settings.BINANCE_TESTNET else settings.BINANCE_SECRET_KEY,
            "enableRateLimit": True,
             "timeout": 30000,  # 30 seconds
            "options": {
                "defaultType": "spot",
                "fetchMarkets": {"types": ["spot"]},
                "fetchCurrencies": False,
                # Reconciliation deliberately checks account-wide open orders
                # to detect manual orders that could consume the bot's funds.
                # CCXT still applies the endpoint's higher request weight.
                "fetchOpenOrders": {"warnWithoutSymbol": False},
            },
          })
        if not self.paper_trading and settings.BINANCE_TESTNET:
            self.exchange.set_sandbox_mode(True)
        self._markets = None

    def get_price(self, symbol: str):
        ticker = self.exchange.fetch_ticker(symbol)
        return ticker["last"]

    def estimate_fee(self, notional_usdt: float):
        # Estimate a reserve for exits; live accounting uses confirmed commissions.
        rate = self.paper_trading_fee_rate if self.paper_trading else settings.LIVE_FEE_RESERVE_PCT
        return abs(notional_usdt) * rate
    
    def safe_load_markets(self, retries=3):
        for attempt in range(retries):
            try:
                return self.exchange.load_markets()
            except Exception as e:
                logger.warning(f"Retry {attempt+1}: {e}")
                time.sleep(2)

        raise Exception("Failed to load markets after retries")

    def _load_markets(self):
        # Cache market metadata because fee conversion may need base and quote assets.
        if self._markets is None:
            self._markets = self.safe_load_markets()
        return self._markets

    def _get_market_assets(self, symbol):
        market = self._load_markets().get(symbol, {})
        return market.get("base"), market.get("quote")

    def _convert_fee_to_usdt(self, currency, amount, reference_price, symbol):
        # Binance can charge fees in the quote asset, base asset, or BNB.
        if currency is None or amount in (None, 0):
            return 0.0

        base_asset, quote_asset = self._get_market_assets(symbol)
        currency = currency.upper()

        if currency == "USDT" or currency == quote_asset:
            return float(amount)

        if currency == base_asset:
            return float(amount) * reference_price

        conversion_symbol = f"{currency}/USDT"
        try:
            conversion_price = self.get_price(conversion_symbol)
            return float(amount) * conversion_price
        except Exception as exc:
            logger.warning(
                "Could not convert fee asset %s to USDT for %s: %s",
                currency,
                symbol,
                exc,
            )
            raise RuntimeError("Cannot value an exchange commission; reconciliation required") from exc

    def _extract_fee_entries(self, order):
        # Collect fee data from the normalized CCXT fields first.
        fee_entries = []

        fees = order.get("fees") or []
        if fees:
            return fees
        fee = order.get("fee")
        if fee:
            return [fee]

        # Binance sometimes exposes per-fill commissions inside the raw payload.
        raw_fills = order.get("info", {}).get("fills") or []
        for fill in raw_fills:
            commission = fill.get("commission")
            if commission is None:
                continue
            fee_entries.append({
                "cost": float(commission),
                "currency": fill.get("commissionAsset"),
            })

        return fee_entries

    def _extract_order_fee_usdt(self, order, symbol, reference_price):
        # Sum all detected fees and convert them into USDT for consistent PnL.
        fee_entries = self._extract_fee_entries(order)
        if not fee_entries:
            return 0.0

        total_fee_usdt = 0.0
        for fee_entry in fee_entries:
            total_fee_usdt += self._convert_fee_to_usdt(
                currency=fee_entry.get("currency"),
                amount=fee_entry.get("cost"),
                reference_price=reference_price,
                symbol=symbol,
            )
        return total_fee_usdt

    def buy(self, symbol: str, quantity: float, price=None):
        if not self.paper_trading:
            raise RuntimeError("Live orders must use the durable live executor")
        execution_price = self.get_price(symbol) if price is None else price
        gross_notional = quantity * execution_price

        if self.paper_trading:
            fee_usdt = self.estimate_fee(gross_notional)
            print("PAPER BUY executed")
            return {
                "mode": "paper",
                "side": "BUY",
                "symbol": symbol,
                "quantity": quantity,
                "price": execution_price,
                "gross_notional_usdt": gross_notional,
                "fee_usdt": fee_usdt,
                "net_cost_usdt": gross_notional + fee_usdt,
            }

    def sell(self, symbol: str, quantity: float, price=None):
        if not self.paper_trading:
            raise RuntimeError("Live orders must use the durable live executor")
        execution_price = self.get_price(symbol) if price is None else price
        gross_notional = quantity * execution_price

        if self.paper_trading:
            fee_usdt = self.estimate_fee(gross_notional)
            print("PAPER SELL executed")
            return {
                "mode": "paper",
                "side": "SELL",
                "symbol": symbol,
                "quantity": quantity,
                "price": execution_price,
                "gross_notional_usdt": gross_notional,
                "fee_usdt": fee_usdt,
                "net_proceeds_usdt": gross_notional - fee_usdt,
            }
