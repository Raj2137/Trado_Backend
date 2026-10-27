"""Durable state for scheduled paper-trading cycles.

The scheduler starts a new Python process for every invocation. This store
keeps the portfolio ledger outside that process so positions and risk controls
survive between runs. SQLite is convenient locally; a PostgreSQL URL can be
supplied through ``DATABASE_URL`` for cloud deployment.
"""

import json
import os
from datetime import datetime, timezone

from sqlalchemy import Column, Integer, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


Base = declarative_base()


class PortfolioStateRecord(Base):
    __tablename__ = "portfolio_state"

    id = Column(Integer, primary_key=True)
    payload = Column(Text, nullable=False)
    updated_at = Column(Text, nullable=False)


class PortfolioStateStore:
    def __init__(self, database_url=None):
        database_url = database_url or os.getenv(
            "DATABASE_URL", "sqlite:///./data/portfolio_state.db"
        )
        if database_url.startswith("sqlite:///"):
            sqlite_path = database_url.removeprefix("sqlite:///")
            parent_directory = os.path.dirname(sqlite_path)
            if parent_directory:
                os.makedirs(parent_directory, exist_ok=True)
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        self.engine = create_engine(database_url, connect_args=connect_args)
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)

    def load(self):
        session = self.session_factory()
        try:
            record = session.get(PortfolioStateRecord, 1)
            return json.loads(record.payload) if record else None
        finally:
            session.close()

    def save(self, payload):
        session = self.session_factory()
        try:
            record = session.get(PortfolioStateRecord, 1)
            encoded = json.dumps(payload, sort_keys=True)
            updated_at = datetime.now(timezone.utc).isoformat()
            if record is None:
                record = PortfolioStateRecord(
                    id=1,
                    payload=encoded,
                    updated_at=updated_at,
                )
                session.add(record)
            else:
                record.payload = encoded
                record.updated_at = updated_at
            session.commit()
        finally:
            session.close()
