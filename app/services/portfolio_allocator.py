class PortfolioAllocator:
    # Explicit deployment rules for concentrated baskets with only a few names.
    SINGLE_COIN_WEIGHTS = [1.0]        # 100%
    TWO_COIN_WEIGHTS = [0.5, 0.5]      # 50-50
    THREE_COIN_WEIGHTS = [0.4, 0.3, 0.3]  # keep same

    @classmethod
    def _normalize_weights(cls, weights):
        total_weight = sum(weights)
        if total_weight <= 0:
            return []
        return [weight / total_weight for weight in weights]

    @classmethod
    def _rank_for_allocation(cls, coins):
        # Allocation order is based on historical behavior, not the live scan rank.
        return sorted(
            coins,
            key=lambda coin: (
                coin.get("backtest_score", 0.0),
                coin.get("backtest_avg_return", 0.0),
                coin.get("backtest_win_rate", 0.0),
                coin.get("score", 0.0),
            ),
            reverse=True,
        )

    @classmethod
    def _get_raw_weights(cls, allocation_order):
        coin_count = len(allocation_order)
        if coin_count == 1:
            return cls.SINGLE_COIN_WEIGHTS
        if coin_count == 2:
            return cls.TWO_COIN_WEIGHTS
        if coin_count == 3:
            return cls.THREE_COIN_WEIGHTS

        # For broader baskets, distribute the full book according to each
        # coin's backtest strength instead of using a static profile.
        backtest_strengths = [
            max(coin.get("backtest_score", 0.0), 0.0)
            for coin in allocation_order
        ]
        if sum(backtest_strengths) <= 0:
            backtest_strengths = [1.0 for _ in allocation_order]
        return cls._normalize_weights(backtest_strengths)

    @classmethod
    def _build_allocation(cls, coin, weight, rank, total_capital, deployment_ratio):
        return {
            "symbol": coin["symbol"],
            "allocation_pct": deployment_ratio * weight * 100,
            "allocation_rank": rank,
            "amount_usdt": total_capital * deployment_ratio * weight,
            "backtest_avg_return": coin.get("backtest_avg_return", 0.0),
            "backtest_total_return": coin.get("backtest_total_return", 0.0),
            "backtest_win_rate": coin.get("backtest_win_rate", 0.0),
            "backtest_score": coin.get("backtest_score", 0.0),
            "profit_lock_start_pct": coin.get("profit_lock_start_pct", 0.02),
            "profit_lock_floor_pct": coin.get("profit_lock_floor_pct", 0.01),
            "profit_lock_drawdown_pct": coin.get("profit_lock_drawdown_pct", 0.02),
            "lock_rule_label": coin.get(
                "lock_rule_label", "2% trigger / 2% trail / 1% floor"
            ),
        }

    @classmethod
    def _build_recommended_profile(
        cls,
        coins,
        total_capital,
        min_trade_size,
        deployment_ratio,
        max_positions,
    ):
        if not coins:
            return None, []

        # The decision agent receives the top-ten ranked coins, but opens a
        # concentrated basket. This avoids spreading a small account across
        # too many minimum-size orders.
        ranked_coins = cls._rank_for_allocation(coins)
        allocation_order = ranked_coins[:max_positions]
        raw_weights = cls._get_raw_weights(allocation_order)
        normalized_weights = (
            raw_weights
            if len(allocation_order) <= 3
            else cls._normalize_weights(raw_weights)
        )
        if not normalized_weights:
            return None, allocation_order

        allocations = []
        portfolio_avg_return = 0.0
        portfolio_total_return = 0.0
        portfolio_win_rate = 0.0
        portfolio_backtest_score = 0.0
        portfolio_profit_lock_rate = 0.0
        deployed_capital = 0.0

        for index, (coin, weight) in enumerate(
            zip(allocation_order, normalized_weights),
            start=1,
        ):
            allocation = cls._build_allocation(
                coin, weight, index, total_capital, deployment_ratio
            )
            amount_usdt = allocation["amount_usdt"]
            if amount_usdt < min_trade_size:
                continue

            allocations.append(allocation)
            deployed_capital += amount_usdt
            portfolio_avg_return += weight * coin.get("backtest_avg_return", 0.0)
            portfolio_total_return += weight * coin.get("backtest_total_return", 0.0)
            portfolio_win_rate += weight * coin.get("backtest_win_rate", 0.0)
            portfolio_backtest_score += weight * coin.get("backtest_score", 0.0)
            portfolio_profit_lock_rate += (
                weight * coin.get("backtest_profit_lock_rate", 0.0)
            )

        if not allocations:
            return None, allocation_order

        # Keep the rest of the top-ten ranked coins as replacement candidates.
        # A replacement uses an equal-sized slot, rather than deploying the
        # entire remaining cash balance into one new name.
        fallback_weight = 1 / max_positions
        fallback_allocations = [
            cls._build_allocation(
                coin,
                fallback_weight,
                index,
                total_capital,
                deployment_ratio,
            )
            for index, coin in enumerate(ranked_coins[max_positions:], start=max_positions + 1)
        ]

        recommended_profile = {
            "name": "risk_reserved_backtest_weighted",
            "weights": [weight * 100 for weight in normalized_weights],
            "allocations": allocations,
            "fallback_allocations": fallback_allocations,
            "portfolio_avg_return": portfolio_avg_return,
            "portfolio_total_return": portfolio_total_return,
            "portfolio_win_rate": portfolio_win_rate,
            "portfolio_backtest_score": portfolio_backtest_score,
            "portfolio_profit_lock_rate": portfolio_profit_lock_rate,
            "deployed_capital": deployed_capital,
            "cash_reserve_usdt": max(total_capital - deployed_capital, 0.0),
        }
        return recommended_profile, allocation_order

    @classmethod
    def allocate(
        cls,
        coins,
        total_capital,
        min_trade_size=5,
        deployment_ratio=0.75,
        max_positions=3,
    ):
        if not 0 < deployment_ratio <= 1:
            raise ValueError("deployment_ratio must be greater than 0 and at most 1")
        if max_positions < 1:
            raise ValueError("max_positions must be at least 1")

        # Use a small, risk-reserved basket selected from the top candidates.
        recommended_profile, allocation_order = cls._build_recommended_profile(
            coins,
            total_capital,
            min_trade_size,
            deployment_ratio,
            max_positions,
        )
        if recommended_profile is None:
            return {
                "order_basis": "backtest_score_then_avg_return",
                "allocation_order": [coin["symbol"] for coin in allocation_order],
                "profiles": [],
                "recommended": None,
            }

        return {
            "order_basis": "backtest_score_then_avg_return",
            "allocation_order": [coin["symbol"] for coin in allocation_order],
            "profiles": [recommended_profile],
            "recommended": recommended_profile,
        }
