import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
    BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")
    PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() == "true"
    PAPER_TRADING_FEE_RATE = float(os.getenv("PAPER_TRADING_FEE_RATE", "0.001"))
    TOTAL_CAPITAL_USDT = float(os.getenv("TOTAL_CAPITAL_USDT", "100"))
    MONITOR_INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL_SECONDS", "60"))
    SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))
    INITIAL_DEPLOYMENT_RATIO = float(os.getenv("INITIAL_DEPLOYMENT_RATIO", "0.75"))
    MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
    MIN_TRADE_SIZE_USDT = float(os.getenv("MIN_TRADE_SIZE_USDT", "5"))
    STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.02"))
    PROFIT_LOCK_START_PCT = float(os.getenv("PROFIT_LOCK_START_PCT", "0.02"))
    PROFIT_LOCK_FLOOR_PCT = float(os.getenv("PROFIT_LOCK_FLOOR_PCT", "0.01"))
    PROFIT_LOCK_DRAWDOWN_PCT = float(os.getenv("PROFIT_LOCK_DRAWDOWN_PCT", "0.02"))
    REENTRY_COOLDOWN_SECONDS = int(os.getenv("REENTRY_COOLDOWN_SECONDS", "3600"))
    LLM_ADVISOR_ENABLED = os.getenv("LLM_ADVISOR_ENABLED", "false").lower() == "true"
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    LLM_ADVISOR_MODEL = os.getenv("LLM_ADVISOR_MODEL", "gpt-5-mini")
    LLM_ADVISOR_INTERVAL_SECONDS = int(os.getenv("LLM_ADVISOR_INTERVAL_SECONDS", "600"))
    LLM_ADVISOR_MAX_AGE_SECONDS = int(os.getenv("LLM_ADVISOR_MAX_AGE_SECONDS", "1200"))
    LLM_ADVISOR_TIMEOUT_SECONDS = float(os.getenv("LLM_ADVISOR_TIMEOUT_SECONDS", "45"))
    LLM_MIN_CONFIDENCE = float(os.getenv("LLM_MIN_CONFIDENCE", "0.70"))
    LLM_MAX_RANK_ADJUSTMENT = float(os.getenv("LLM_MAX_RANK_ADJUSTMENT", "0.15"))

settings = Settings()
