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
    def _build_recommended_profile(cls, coins, total_capital, min_trade_size):
        if not coins:
            return None, []

        allocation_order = cls._rank_for_allocation(coins)
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
            amount_usdt = total_capital * weight
            if amount_usdt < min_trade_size:
                continue

            allocations.append({
                "symbol": coin["symbol"],
                "allocation_pct": weight * 100,
                "allocation_rank": index,
                "amount_usdt": amount_usdt,
                "backtest_avg_return": coin.get("backtest_avg_return", 0.0),
                "backtest_total_return": coin.get("backtest_total_return", 0.0),
                "backtest_win_rate": coin.get("backtest_win_rate", 0.0),
                "backtest_score": coin.get("backtest_score", 0.0),
                "profit_lock_start_pct": coin.get("profit_lock_start_pct", 0.01),
                "profit_lock_step_pct": coin.get("profit_lock_step_pct", 0.01),
                "lock_rule_label": coin.get("lock_rule_label", "1%"),
            })
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

        # 🔥 Ensure FULL capital deployment (CRITICAL FIX)
        # If any capital is left unused (due to rounding or min_trade_size),
        # assign it to the top-ranked coin instead of leaving it idle.

        leftover_capital = total_capital - deployed_capital

        if leftover_capital >= min_trade_size and allocations:
            allocations[0]["amount_usdt"] += leftover_capital
            allocations[0]["allocation_pct"] += (leftover_capital / total_capital) * 100
            deployed_capital += leftover_capital

        recommended_profile = {
            "name": "adaptive_backtest_weighted",
            "weights": [weight * 100 for weight in normalized_weights],
            "allocations": allocations,
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
    def allocate(cls, coins, total_capital, min_trade_size=5, deployment_ratio=1.0, max_positions=None):
        # Keep compatibility with the decision agent's configured basket size
        # while retaining this allocator's backtest ranking and weights.
        if not 0 < deployment_ratio <= 1:
            raise ValueError("deployment_ratio must be greater than zero and at most one")
        if max_positions is not None:
            if max_positions < 1:
                raise ValueError("max_positions must be positive")
            coins = cls._rank_for_allocation(coins)[:max_positions]
        # Use explicit concentration caps for 1-3 names, then switch to a
        # full-capital backtest-weighted split once the basket is broader.
        recommended_profile, allocation_order = cls._build_recommended_profile(
            coins,
            total_capital * deployment_ratio,
            min_trade_size,
        )
        if recommended_profile is None:
            return {
                "order_basis": "backtest_score_then_avg_return",
                "allocation_order": [coin["symbol"] for coin in allocation_order],
                "profiles": [],
                "recommended": None,
            }

        recommended_profile["cash_reserve_usdt"] = max(
            total_capital - recommended_profile["deployed_capital"], 0.0
        )

        return {
            "order_basis": "backtest_score_then_avg_return",
            "allocation_order": [coin["symbol"] for coin in allocation_order],
            "profiles": [recommended_profile],
            "recommended": recommended_profile,
        }
