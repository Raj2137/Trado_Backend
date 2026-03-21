import logging

import ccxt

# Use a module logger so selection decisions can be traced in logs.
logger = logging.getLogger(__name__)


class CoinSelector:
    LOCK_PROFILE_OPTIONS = (
        {
            "profit_lock_start_pct": 0.01,
            "profit_lock_step_pct": 0.01,
            "lock_rule_label": "1%",
        },
        {
            "profit_lock_start_pct": 0.02,
            "profit_lock_step_pct": 0.01,
            "lock_rule_label": "2%",
        },
    )

    # Stable or quasi-stable assets are excluded because they are not useful
    # for momentum-based spot trading.
    STABLE_BASE_ASSETS = {
        "USDC",
        "BUSD",
        "FDUSD",
        "TUSD",
        "USDP",
        "DAI",
        "USDS",
        "EUR",
    }

    # Binance leveraged token suffixes are excluded because they are synthetic,
    # decay over time, and are poor candidates for this strategy.
    LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S")

    def __init__(self):
        # Initialize the exchange client with built-in rate limiting.
        self.exchange = ccxt.binance({
            "enableRateLimit": True,
            "timeout": 30000,  # 30 seconds
        })

        # Minimum 24h notional volume to avoid thin markets.
        self.MIN_24H_QUOTE_VOLUME_USD = 5_000_000

        # Minimum 7-day notional volume to filter out noisy names with only
        # temporary activity.
        self.MIN_WEEKLY_VOLUME_USD = 10_000_000

        # Ignore newly listed coins because they often have unstable behavior.
        self.MIN_LISTING_DAYS = 90

        # Require a reasonably tight spread so entries and exits are practical.
        self.MAX_SPREAD_PCT = 0.006

        # Require enough recent momentum so the shortlist is biased toward
        # active trend candidates instead of merely stable liquid coins.
        self.MIN_6H_MOMENTUM = 0.003   # 0.2%
        self.MIN_24H_MOMENTUM = 0.009   # 1.5%

        # Fetch a deeper history so the selector can screen for legitimate,
        # established names before running the recent rank/backtest passes.
        self.HISTORY_LIMIT = 336
        self.RANKING_WINDOW_HOURS = 24
        self.BACKTEST_WINDOW_HOURS = 48
        self.LEGITIMACY_WINDOW_HOURS = 24 * 14
        self.WEEKLY_VOLUME_WINDOW_HOURS = 24 * 7

        # The backtest now uses a 48-hour window, which gives the rolling
        # simulation more entry opportunities while keeping it recent.
        self.BACKTEST_LOOKBACK = 12
        self.BACKTEST_FORWARD_WINDOW = 6
        self.BACKTEST_STEP_HOURS = 2
        # Backtest is used to rank and choose the profit-lock rule, not to
        # disqualify otherwise valid scan candidates.
        self.MIN_BACKTEST_TRADES = 0
        self.STOP_LOSS_PCT = -0.02

        # Only deeply inspect the most liquid names to keep the scan focused.
        self.SCAN_LIMIT = 45

    def _safe_ratio(self, numerator, denominator):
        # Protect ratio calculations from division-by-zero.
        if not denominator:
            return 0.0
        return numerator / denominator

    def _clamp(self, value, lower=0.0, upper=1.0):
        # Normalize values into a scoring range.
        return max(lower, min(value, upper))

    def _estimate_candle_notional_usd(self, candle):
        # CCXT normalizes Binance spot OHLCV as:
        # [timestamp, open, high, low, close, base_volume]
        # so we estimate quote notional from price * base volume. Using the
        # candle typical price is slightly less biased than close * volume.
        typical_price = (candle[2] + candle[3] + candle[4]) / 3
        return typical_price * candle[5]

    def _get_listing_days(self, market):
        # Binance exposes the onboarding timestamp in market metadata.
        onboard_date = market.get("info", {}).get("onboardDate")
        if onboard_date is None:
            # If the listing date is unknown, treat the coin as old enough.
            return 9999

        try:
            listed_ms = int(onboard_date)
        except (TypeError, ValueError):
            # A malformed onboarding timestamp makes the age unreliable.
            return 0

        # Convert age from milliseconds into days.
        age_ms = max(self.exchange.milliseconds() - listed_ms, 0)
        return age_ms / 86_400_000

    def _is_leveraged_token(self, base_asset):
        # Leveraged tokens usually end with a known exchange suffix.
        upper_base = (base_asset or "").upper()
        return any(upper_base.endswith(suffix) for suffix in self.LEVERAGED_SUFFIXES)

    def _is_tradeable_symbol(self, symbol, market, ticker):
        # Restrict the universe to spot USDT pairs for a simple first version.
        if not symbol.endswith("/USDT"):
            return False

        # Ignore delisted, paused, or non-spot markets.
        if not market.get("active") or not market.get("spot"):
            return False

        # Explicitly reject futures, swaps, and other contract markets.
        if market.get("swap") or market.get("future") or market.get("contract"):
            return False

        # Exclude stable assets and exchange leveraged products.
        base_asset = (market.get("base", "") or "").upper()
        if base_asset in self.STABLE_BASE_ASSETS or self._is_leveraged_token(base_asset):
            return False

        # Require strong 24h quote volume to avoid illiquid traps.
        quote_volume = ticker.get("quoteVolume")
        if quote_volume is None or quote_volume < self.MIN_24H_QUOTE_VOLUME_USD:
            return False

        # A low number of trades is a warning sign even if notional volume looks fine.
        trade_count = ticker.get("count")
        if trade_count is not None and trade_count < 5_000:
            return False

        # Wide spreads make fills worse and can fake apparent momentum.
        bid = ticker.get("bid")
        ask = ticker.get("ask")
        if bid and ask and bid > 0 and ask > 0:
            spread_pct = (ask - bid) / bid
            if spread_pct > self.MAX_SPREAD_PCT:
                return False

        # New listings are more vulnerable to erratic moves and manipulation.
        if self._get_listing_days(market) < self.MIN_LISTING_DAYS:
            return False

        # Symbols that pass all checks are eligible for deeper scoring.
        return True

    def _passes_backtest_entry(self, closes, volumes, entry_index):
        # Reuse the same style of momentum and trend checks on recent intraday
        # candles so the backtest still matches the live scan style.
        if entry_index < self.BACKTEST_LOOKBACK:
            return False

        entry_price = closes[entry_index]
        momentum_3h = (
            (closes[entry_index] - closes[entry_index - 3]) / closes[entry_index - 3]
        )
        momentum_6h = (
            (closes[entry_index] - closes[entry_index - 6]) / closes[entry_index - 6]
        )
        momentum_12h = (
            (closes[entry_index] - closes[entry_index - 12]) / closes[entry_index - 12]
        )

        if (
            momentum_3h < 0
            or momentum_6h < self.MIN_6H_MOMENTUM * 0.5
            or momentum_12h < self.MIN_24H_MOMENTUM * 0.5
            or momentum_12h > 0.20
        ):
            return False

        recent_closes = closes[entry_index - self.BACKTEST_LOOKBACK:entry_index + 1]
        volatility = (max(recent_closes) - min(recent_closes)) / min(recent_closes)
        if volatility < 0.015 or volatility > 0.25:
            return False

        baseline_window = volumes[entry_index - self.BACKTEST_LOOKBACK:entry_index - 3]
        avg_volume = sum(baseline_window) / len(baseline_window)
        recent_volume = sum(volumes[entry_index - 2:entry_index + 1]) / 3
        volume_spike = recent_volume / avg_volume if avg_volume > 0 else 0
        if volume_spike < 0.8:
            return False

        sma_6 = sum(closes[entry_index - 5:entry_index + 1]) / 6
        sma_12 = sum(closes[entry_index - 11:entry_index + 1]) / 12
        if not (entry_price > sma_6 > sma_12):
            return False

        return True

    def _score_backtest_trades(self, trades):
        if not trades:
            return {
                "trades": 0,
                "win_rate": 0.0,
                "avg_return": 0.0,
                "total_return": 0.0,
                "profit_lock_rate": 0.0,
                "avg_hours_held": 0.0,
                "score": 0.0,
            }

        returns = [trade["return"] for trade in trades]
        winning_trades = [trade for trade in trades if trade["return"] > 0]
        lock_exits = [trade for trade in trades if trade["exit_reason"] == "profit_lock"]
        avg_return = sum(returns) / len(returns)
        total_return = sum(returns)
        win_rate = len(winning_trades) / len(trades)
        profit_lock_rate = len(lock_exits) / len(trades)
        avg_hours_held = sum(trade["hours_held"] for trade in trades) / len(trades)

        # Favor coins whose recent behavior repeatedly rewards the trailing
        # profit-lock rule without over-weighting thin samples.
        backtest_score = (
            self._clamp((avg_return + 0.01) / 0.05) * 0.45 +
            self._clamp((win_rate - 0.35) / 0.45) * 0.30 +
            self._clamp(profit_lock_rate / 0.70) * 0.15 +
            self._clamp(len(trades) / 3) * 0.10
        )

        return {
            "trades": len(trades),
            "win_rate": win_rate,
            "avg_return": avg_return,
            "total_return": total_return,
            "profit_lock_rate": profit_lock_rate,
            "avg_hours_held": avg_hours_held,
            "score": backtest_score,
        }

    def _is_better_backtest_result(self, candidate, incumbent):
        return (
            candidate["score"],
            candidate["total_return"],
            candidate["avg_return"],
            candidate["win_rate"],
            candidate["profit_lock_rate"],
        ) > (
            incumbent["score"],
            incumbent["total_return"],
            incumbent["avg_return"],
            incumbent["win_rate"],
            incumbent["profit_lock_rate"],
        )

    def _simulate_trade(
        self,
        entry_price,
        future_candles,
        profit_lock_start_pct,
        profit_lock_step_pct,
    ):
        # This approximates the live trailing logic using hourly candles:
        # stop out at -2%, then ratchet the locked exit according to the
        # lock profile being evaluated for that coin.
        locked_profit = None

        for hours_held, candle in enumerate(future_candles, start=1):
            high_price = candle[2]
            low_price = candle[3]

            # When no profit lock exists yet, use the hard stop first as the
            # conservative assumption for intrabar ordering.
            if locked_profit is None and low_price <= entry_price * (1 + self.STOP_LOSS_PCT):
                return self.STOP_LOSS_PCT, "stop_loss", hours_held

            high_profit = (high_price - entry_price) / entry_price
            if high_profit >= profit_lock_start_pct:
                lock_steps = int(
                    (high_profit - profit_lock_start_pct) / profit_lock_step_pct
                )
                highest_lock = (
                    profit_lock_start_pct
                    + lock_steps * profit_lock_step_pct
                )
                locked_profit = max(locked_profit or 0.0, highest_lock)

            if locked_profit is not None:
                lock_price = entry_price * (1 + locked_profit)
                if low_price <= lock_price:
                    return locked_profit, "profit_lock", hours_held

        final_close = future_candles[-1][4]
        final_return = (final_close - entry_price) / entry_price
        return final_return, "timeout", len(future_candles)

    def _backtest_strategy(self, ohlcv):
        # Run a rolling simulation over historical candles and choose the lock
        # profile that best matches this coin's recent behavior.
        closes = [c[4] for c in ohlcv]
        volumes = [c[5] for c in ohlcv]
        best_result = None
        max_entry_index = len(ohlcv) - self.BACKTEST_FORWARD_WINDOW - 1

        for lock_profile in self.LOCK_PROFILE_OPTIONS:
            trades = []
            for entry_index in range(
                self.BACKTEST_LOOKBACK,
                max_entry_index + 1,
                self.BACKTEST_STEP_HOURS,
            ):
                if not self._passes_backtest_entry(closes, volumes, entry_index):
                    continue

                entry_price = closes[entry_index]
                future_candles = ohlcv[
                    entry_index + 1:entry_index + 1 + self.BACKTEST_FORWARD_WINDOW
                ]
                trade_return, exit_reason, hours_held = self._simulate_trade(
                    entry_price,
                    future_candles,
                    profit_lock_start_pct=lock_profile["profit_lock_start_pct"],
                    profit_lock_step_pct=lock_profile["profit_lock_step_pct"],
                )
                trades.append({
                    "return": trade_return,
                    "exit_reason": exit_reason,
                    "hours_held": hours_held,
                })

            result = self._score_backtest_trades(trades)
            result.update(lock_profile)
            if best_result is None or self._is_better_backtest_result(result, best_result):
                best_result = result

        return best_result or {
            "trades": 0,
            "win_rate": 0.0,
            "avg_return": 0.0,
            "total_return": 0.0,
            "profit_lock_rate": 0.0,
            "avg_hours_held": 0.0,
            "score": 0.0,
            "profit_lock_start_pct": self.LOCK_PROFILE_OPTIONS[0]["profit_lock_start_pct"],
            "profit_lock_step_pct": self.LOCK_PROFILE_OPTIONS[0]["profit_lock_step_pct"],
            "lock_rule_label": self.LOCK_PROFILE_OPTIONS[0]["lock_rule_label"],
        }
    def safe_load_markets(self, retries=3):
            for attempt in range(retries):
                try:
                    return self.exchange.load_markets()
                except Exception as e:
                    logger.warning(f"Retry {attempt+1} loading markets: {e}")
                    time.sleep(2)

            raise Exception("Failed to load markets after retries")

    def get_top_coins(self, limit=10):
        # Load market metadata once so symbol-level checks are cheap.
        logger.info("Loading markets...")
        
        
        markets = self.safe_load_markets()

        # Pull all ticker snapshots so we can prefilter by live liquidity.
        logger.info("Fetching tickers...")
        tickers = self.exchange.fetch_tickers()

        # Keep only symbols that pass the hard tradability filters.
        symbols = [
            symbol for symbol in tickers
            if (
                symbol in markets
                and self._is_tradeable_symbol(symbol, markets[symbol], tickers[symbol])
            )
        ]

        # Focus the expensive OHLCV scan on the most liquid names first.
        symbols = sorted(
            symbols,
            key=lambda s: tickers[s]["quoteVolume"],
            reverse=True
        )[:self.SCAN_LIMIT]

        # Track candidates that pass the non-backtest selector filters first,
        # then run the recent backtest as a separate ranking stage.
        pre_backtest_candidates = []
        candidates = []

        logger.info(f"Scanning top {len(symbols)} liquid coins...")

        for symbol in symbols:
            try:
                # Fetch two weeks of hourly candles so legitimacy filters can
                # use a broader context before the recent rank/backtest passes.
                ohlcv = self.exchange.fetch_ohlcv(
                    symbol,
                    timeframe="1h",
                    limit=self.HISTORY_LIMIT
                )

                # Keep enough history for legitimacy checks, then reserve the
                # latest 24h slice for ranking and the latest 48h slice for
                # backtesting.
                if len(ohlcv) < max(
                    self.LEGITIMACY_WINDOW_HOURS,
                    self.BACKTEST_LOOKBACK + self.BACKTEST_FORWARD_WINDOW,
                    self.BACKTEST_WINDOW_HOURS,
                    self.RANKING_WINDOW_HOURS,
                ):
                    continue

                legitimacy_ohlcv = ohlcv[-self.LEGITIMACY_WINDOW_HOURS:]
                legitimacy_notional = [
                    self._estimate_candle_notional_usd(candle)
                    for candle in legitimacy_ohlcv
                ]
                weekly_notional_usd = sum(
                    legitimacy_notional[-self.WEEKLY_VOLUME_WINDOW_HOURS:]
                )
                if weekly_notional_usd < self.MIN_WEEKLY_VOLUME_USD:
                    continue

                legitimacy_closes = [c[4] for c in legitimacy_ohlcv]
                two_week_range = (
                    max(legitimacy_closes) - min(legitimacy_closes)
                ) / min(legitimacy_closes)
                if two_week_range > 1.20:
                    continue

                # Use the latest 24h for live ranking and a longer 48h slice
                # for the recent backtest.
                ranking_ohlcv = ohlcv[-self.RANKING_WINDOW_HOURS:]
                backtest_ohlcv = ohlcv[-self.BACKTEST_WINDOW_HOURS:]
                ranking_closes = [c[4] for c in ranking_ohlcv]
                ranking_volumes = [c[5] for c in ranking_ohlcv]
                ranking_notional = [
                    self._estimate_candle_notional_usd(candle)
                    for candle in ranking_ohlcv
                ]

                # Pull the latest market snapshot used in ranking and reporting.
                current_price = ranking_closes[-1]
                ticker = tickers[symbol]
                market = markets[symbol]
                listing_days = self._get_listing_days(market)
                spread_pct = 0.0

                # Compute the live spread if bid/ask data is available.
                bid = ticker.get("bid")
                ask = ticker.get("ask")
                if bid and ask and bid > 0 and ask > 0:
                    spread_pct = (ask - bid) / bid

                # Use only the latest 24 hours for live recommendation ranking.
                notional_24h_usd = sum(ranking_notional)
                liquidity_score = min(notional_24h_usd / 20_000_000, 1.0)

                # Measure trend strength across short and medium horizons.
                momentum_6h = (
                    (ranking_closes[-1] - ranking_closes[-7]) / ranking_closes[-7]
                )
                momentum_12h = (
                    (ranking_closes[-1] - ranking_closes[-13]) / ranking_closes[-13]
                )
                momentum_24h = (
                    (ranking_closes[-1] - ranking_closes[0]) / ranking_closes[0]
                )

                # Use weighted momentum so recent strength matters most.
                momentum = (
                    momentum_6h * 0.45 +
                    momentum_12h * 0.30 +
                    momentum_24h * 0.25
                )

                # Require strength across both recent and daily horizons.
                if (
                    momentum <= 0
                    or momentum_6h < self.MIN_6H_MOMENTUM
                    or momentum_24h < self.MIN_24H_MOMENTUM
                ):
                    continue

                # Extremely extended moves are usually worse entries even if they
                # rank highly on momentum, so keep a loose upper sanity check.
                if momentum_24h > 0.35:
                    continue

                # Normalize momentum into the scoring range.
                momentum_score = self._clamp(momentum * 8)

                # Prefer names where recent momentum is accelerating, not fading.
                acceleration = momentum_6h - momentum_24h / 4
                acceleration_score = self._clamp((acceleration + 0.01) * 18)

                # Look for coins that are moving enough to trade, but not chaotically.
                recent_closes = ranking_closes
                volatility = (max(recent_closes) - min(recent_closes)) / min(recent_closes)
                if volatility < 0.02 or volatility > 0.50:
                    continue
                volatility_score = self._clamp(volatility / 0.20)

                # Compare recent trading activity with its recent baseline.
                baseline_window = ranking_volumes[:-6]
                avg_volume = sum(baseline_window) / len(baseline_window)
                recent_volume = sum(ranking_volumes[-6:]) / 6

                # Require recent activity to at least match the baseline.
                volume_spike = recent_volume / avg_volume if avg_volume > 0 else 0
                if volume_spike <  0.7:
                    continue

                # Stronger volume confirmation gets a better score.
                volume_score = self._clamp(volume_spike / 2.5)

                # Check whether price is near a recent breakout zone.
                previous_24h_high = max(ranking_closes[:-1])
                breakout = current_price >= previous_24h_high * 0.995
                breakout_score = 1.0 if breakout else 0.0

                # Prefer coins that are in a clean short-term uptrend structure.
                sma_8 = sum(ranking_closes[-8:]) / 8
                sma_20 = sum(ranking_closes[-20:]) / 20
                if not (current_price > sma_8 > sma_20):
                    continue

                # Measure how strongly price is separated above its moving averages.
                trend_strength = (
                    self._safe_ratio(current_price - sma_8, sma_8) +
                    self._safe_ratio(sma_8 - sma_20, sma_20)
                )
                trend_score = self._clamp(trend_strength * 25)

                # Count how many recent candles closed green as a basic consistency check.
                green_candles = sum(
                    1 for candle in ranking_ohlcv
                    if candle[4] > candle[1]
                )
                candle_consistency = green_candles / len(ranking_ohlcv)
                consistency_score = self._clamp((candle_consistency - 0.45) / 0.25)

                # Apply a soft penalty to stretched moves instead of banning most
                # strong runners outright.
                exhaustion_penalty = self._clamp(max(momentum_24h - 0.22, 0) / 0.13)

                selector_score = (
                    momentum_score * 0.37 +
                    acceleration_score * 0.17 +
                    volume_score * 0.14 +
                    trend_score * 0.11 +
                    volatility_score * 0.06 +
                    liquidity_score * 0.06 +
                    consistency_score * 0.04 +
                    breakout_score * 0.05 -
                    exhaustion_penalty * 0.10
                )

                pre_backtest_candidates.append({
                    "symbol": symbol,
                    "backtest_ohlcv": backtest_ohlcv,
                    "selector_score": selector_score,
                    "score": selector_score,
                    "price": current_price,
                    "volume_24h": ticker["quoteVolume"],
                    "momentum": momentum,
                    "momentum_6h": momentum_6h,
                    "momentum_24h": momentum_24h,
                    "acceleration": acceleration,
                    "volume_spike": volume_spike,
                    "volatility": volatility,
                    "breakout": breakout,
                    "spread_pct": spread_pct,
                    "listing_days": listing_days,
                    "weekly_volume_usd": weekly_notional_usd,
                    "two_week_range": two_week_range,
                    "trend_score": trend_score,
                    "momentum_score": momentum_score,
                    "acceleration_score": acceleration_score,
                    "liquidity_score": liquidity_score,
                    "volatility_score": volatility_score,
                    "volume_score": volume_score,
                    "breakout_score": breakout_score,
                    "consistency_score": consistency_score,
                    "exhaustion_penalty": exhaustion_penalty,
                })

            except Exception as e:
                # Skip symbols that fail data collection without killing the scan.
                logger.debug(f"Skipping {symbol}: {e}")
                continue

        logger.info(
            "Selected %d coins after selector filters",
            len(pre_backtest_candidates),
        )

        pre_backtest_candidates.sort(
            key=lambda coin: coin["selector_score"],
            reverse=True,
        )
        selector_shortlist = pre_backtest_candidates[:limit]

        logger.info(
            "Starting %dh backtest ranking for %d shortlisted coins",
            self.BACKTEST_WINDOW_HOURS,
            len(selector_shortlist),
        )

        for candidate in selector_shortlist:
            backtest_ohlcv = candidate.pop("backtest_ohlcv")

            # Backtest the exit rule on recent historical setups to help
            # rank the selector-shortlisted coins and choose the lock rule.
            backtest = self._backtest_strategy(backtest_ohlcv)

            # Combine live momentum and historical strategy behavior into
            # one final ranking score without changing shortlist membership.
            score = (
                candidate["selector_score"] * 0.70 +
                backtest["score"] * 0.30
            )

            candidate["score"] = score
            candidate["backtest_trades"] = backtest["trades"]
            candidate["backtest_win_rate"] = backtest["win_rate"]
            candidate["backtest_avg_return"] = backtest["avg_return"]
            candidate["backtest_total_return"] = backtest["total_return"]
            candidate["backtest_profit_lock_rate"] = backtest["profit_lock_rate"]
            candidate["backtest_avg_hours_held"] = backtest["avg_hours_held"]
            candidate["backtest_score"] = backtest["score"]
            candidate["profit_lock_start_pct"] = backtest["profit_lock_start_pct"]
            candidate["profit_lock_step_pct"] = backtest["profit_lock_step_pct"]
            candidate["lock_rule_label"] = backtest["lock_rule_label"]

            # These were only needed to build the final score.
            candidate.pop("momentum_score")
            candidate.pop("acceleration_score")
            candidate.pop("liquidity_score")
            candidate.pop("volatility_score")
            candidate.pop("volume_score")
            candidate.pop("breakout_score")
            candidate.pop("consistency_score")
            candidate.pop("selector_score")

            candidates.append(candidate)

        # Highest scoring coins become the final shortlist.
        candidates.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        top = candidates[:limit]

        # Emit a summary for visibility and debugging.
        logger.info(f"Selected top {len(top)} coins")

        for coin in top:
            logger.info(
                f"{coin['symbol']} | "
                f"Score: {coin['score']:.3f} | "
                f"6h Momentum: {coin['momentum_6h']:.3f} | "
                f"24h Momentum: {coin['momentum_24h']:.3f} | "
                f"BT Avg: {coin['backtest_avg_return']:.3f} | "
                f"BT Win: {coin['backtest_win_rate']:.2f} | "
                f"Lock: {coin['lock_rule_label']} | "
                f"Volume Spike: {coin['volume_spike']:.2f} | "
                f"Spread: {coin['spread_pct'] * 100:.3f}%"
            )

        # Return the shortlist for the next step in the trading pipeline.
        return top
