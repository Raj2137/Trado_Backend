import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
    BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")
    PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() == "true"
    PAPER_TRADING_FEE_RATE = float(os.getenv("PAPER_TRADING_FEE_RATE", "0.001"))

settings = Settings()
