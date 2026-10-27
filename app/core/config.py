import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    TRADING_TIMEZONE = os.getenv("TRADING_TIMEZONE", "Asia/Kolkata")
    PORTFOLIO_ACCOUNT_ID = os.getenv("PORTFOLIO_ACCOUNT_ID", "default")
    LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
    LIVE_ENTRIES_ENABLED = os.getenv("LIVE_ENTRIES_ENABLED", "false").lower() == "true"
    LIVE_CAPITAL_LIMIT_USDT = float(os.getenv("LIVE_CAPITAL_LIMIT_USDT", "0"))
    LIVE_MAX_POSITION_USDT = float(os.getenv("LIVE_MAX_POSITION_USDT", "0"))
    LIVE_DAILY_LOSS_LIMIT_PCT = float(os.getenv("LIVE_DAILY_LOSS_LIMIT_PCT", "0.02"))
    LIVE_FEE_RESERVE_PCT = float(os.getenv("LIVE_FEE_RESERVE_PCT", "0.005"))
    LIVE_MAX_SPREAD_PCT = float(os.getenv("LIVE_MAX_SPREAD_PCT", "0.003"))
    LIVE_STOP_LIMIT_GAP_PCT = float(os.getenv("LIVE_STOP_LIMIT_GAP_PCT", "0.005"))
    LIVE_MONITOR_INTERVAL_SECONDS = int(os.getenv("LIVE_MONITOR_INTERVAL_SECONDS", "15"))
    BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "false").lower() == "true"
    BINANCE_TESTNET_API_KEY = os.getenv("BINANCE_TESTNET_API_KEY")
    BINANCE_TESTNET_SECRET_KEY = os.getenv("BINANCE_TESTNET_SECRET_KEY")
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/portfolio_state.db")
    BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
    BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")
    PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() == "true"
    PAPER_TRADING_FEE_RATE = float(os.getenv("PAPER_TRADING_FEE_RATE", "0.001"))
    TOTAL_CAPITAL_USDT = float(os.getenv("TOTAL_CAPITAL_USDT", "100"))
    # Risk exits need a much faster loop than ranking/rebalancing.
    MONITOR_INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL_SECONDS", "60"))
    SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "900"))
    # The approved one- or two-coin basket is fully deployed; only trading
    # fees leave a small unavoidable cash remainder.
    INITIAL_DEPLOYMENT_RATIO = float(os.getenv("INITIAL_DEPLOYMENT_RATIO", "1.0"))
    MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "2"))
    # Evaluate this wider live-signal shortlist before choosing the final two.
    BACKTEST_CANDIDATE_LIMIT = int(os.getenv("BACKTEST_CANDIDATE_LIMIT", "10"))
    MIN_TRADE_SIZE_USDT = float(os.getenv("MIN_TRADE_SIZE_USDT", "5"))
    STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.02"))
    # Halt new entries for the remainder of the calendar day after this
    # percentage decline from that day's opening equity.
    DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.30"))
    # Backtests must remain positive after estimated buy and sell fees before
    # they are allowed to fund a new position.
    MIN_BACKTEST_TRADES = int(os.getenv("MIN_BACKTEST_TRADES", "3"))
    MIN_BACKTEST_NET_AVG_RETURN = float(
        os.getenv("MIN_BACKTEST_NET_AVG_RETURN", "0.0")
    )
    PROFIT_LOCK_START_PCT = float(os.getenv("PROFIT_LOCK_START_PCT", "0.02"))
    # Once a position reaches the trigger, preserve an absolute 2% net gain.
    PROFIT_LOCK_FLOOR_PCT = float(os.getenv("PROFIT_LOCK_FLOOR_PCT", "0.02"))
    # Retained for compatibility with existing deployment variables. Fixed
    # profit-floor mode intentionally does not trail below the peak.
    PROFIT_LOCK_DRAWDOWN_PCT = float(os.getenv("PROFIT_LOCK_DRAWDOWN_PCT", "0.0"))
    REENTRY_COOLDOWN_SECONDS = int(os.getenv("REENTRY_COOLDOWN_SECONDS", "900"))
    LLM_ADVISOR_ENABLED = os.getenv("LLM_ADVISOR_ENABLED", "false").lower() == "true"
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    LLM_ADVISOR_MODEL = os.getenv("LLM_ADVISOR_MODEL", "gpt-5-mini")
    LLM_ADVISOR_INTERVAL_SECONDS = int(os.getenv("LLM_ADVISOR_INTERVAL_SECONDS", "900"))
    LLM_ADVISOR_MAX_AGE_SECONDS = int(os.getenv("LLM_ADVISOR_MAX_AGE_SECONDS", "1200"))
    LLM_ADVISOR_TIMEOUT_SECONDS = float(os.getenv("LLM_ADVISOR_TIMEOUT_SECONDS", "45"))
    LLM_MIN_CONFIDENCE = float(os.getenv("LLM_MIN_CONFIDENCE", "0.70"))
    LLM_MAX_RANK_ADJUSTMENT = float(os.getenv("LLM_MAX_RANK_ADJUSTMENT", "0.15"))

settings = Settings()
