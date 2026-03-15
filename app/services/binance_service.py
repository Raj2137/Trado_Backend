import logging

import ccxt

from app.core.config import settings

logger = logging.getLogger(__name__)


class BinanceService:
    def __init__(self, paper_trading=None):
        # Let runtime arguments override the env default when needed.
        self.paper_trading = (
            settings.PAPER_TRADING if paper_trading is None else paper_trading
        )
        self.paper_trading_fee_rate = settings.PAPER_TRADING_FEE_RATE
        self.exchange = ccxt.binance({
            "apiKey": settings.BINANCE_API_KEY,
            "secret": settings.BINANCE_SECRET_KEY,
            "enableRateLimit": True,
        })
        self._markets = None

    def get_price(self, symbol: str):
        ticker = self.exchange.fetch_ticker(symbol)
        return ticker["last"]

    def estimate_fee(self, notional_usdt: float):
        # Only apply the simplified fee model in paper trading mode.
        if not self.paper_trading:
            return 0.0
        return abs(notional_usdt) * self.paper_trading_fee_rate

    def _load_markets(self):
        # Cache market metadata because fee conversion may need base and quote assets.
        if self._markets is None:
            self._markets = self.exchange.load_markets()
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
            return 0.0

    def _extract_fee_entries(self, order):
        # Collect fee data from the normalized CCXT fields first.
        fee_entries = []

        fee = order.get("fee")
        if fee:
            fee_entries.append(fee)

        fees = order.get("fees") or []
        fee_entries.extend(fees)

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

    def _extract_filled_quantity(self, order, fallback_quantity):
        # Use the actual filled quantity when the exchange returns it.
        filled_quantity = order.get("filled")
        if filled_quantity not in (None, 0):
            return float(filled_quantity)
        return float(fallback_quantity)

    def _extract_execution_price(self, order, fallback_price, filled_quantity):
        # Prefer the exchange average price; otherwise derive it from order cost.
        average_price = order.get("average")
        if average_price not in (None, 0):
            return float(average_price)

        cost = order.get("cost")
        if cost not in (None, 0) and filled_quantity:
            return float(cost) / float(filled_quantity)

        return float(fallback_price)

    def _fetch_order_details(self, order, symbol):
        # Some Binance responses omit final fees on the initial create call.
        order_id = order.get("id")
        if not order_id:
            return order

        try:
            return self.exchange.fetch_order(order_id, symbol)
        except Exception as exc:
            logger.debug(
                "Could not fetch completed order details for %s %s: %s",
                symbol,
                order_id,
                exc,
            )
            return order

    def _normalize_live_order(self, order, side, symbol, fallback_quantity, fallback_price):
        # Convert the raw exchange order into the same shape used by paper mode.
        detailed_order = self._fetch_order_details(order, symbol)
        filled_quantity = self._extract_filled_quantity(detailed_order, fallback_quantity)
        execution_price = self._extract_execution_price(
            detailed_order,
            fallback_price=fallback_price,
            filled_quantity=filled_quantity,
        )
        gross_notional = detailed_order.get("cost")
        if gross_notional in (None, 0):
            gross_notional = filled_quantity * execution_price
        fee_usdt = self._extract_order_fee_usdt(
            detailed_order,
            symbol=symbol,
            reference_price=execution_price,
        )

        normalized = {
            "mode": "live",
            "side": side,
            "symbol": symbol,
            "quantity": filled_quantity,
            "price": execution_price,
            "gross_notional_usdt": gross_notional,
            "fee_usdt": fee_usdt,
            "raw_order": detailed_order,
        }

        if side == "BUY":
            normalized["net_cost_usdt"] = gross_notional + fee_usdt
        else:
            normalized["net_proceeds_usdt"] = gross_notional - fee_usdt

        return normalized

    def buy(self, symbol: str, quantity: float, price=None):
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

        raw_order = self.exchange.create_market_buy_order(symbol, quantity)
        return self._normalize_live_order(
            raw_order,
            side="BUY",
            symbol=symbol,
            fallback_quantity=quantity,
            fallback_price=execution_price,
        )

    def sell(self, symbol: str, quantity: float, price=None):
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

        raw_order = self.exchange.create_market_sell_order(symbol, quantity)
        return self._normalize_live_order(
            raw_order,
            side="SELL",
            symbol=symbol,
            fallback_quantity=quantity,
            fallback_price=execution_price,
        )
