from datetime import datetime

class Position:
    def __init__(self, symbol, entry_price, quantity):
        self.symbol = symbol
        self.entry_price = entry_price
        self.quantity = quantity
        self.opened_at = datetime.utcnow()
        self.is_open = True
