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
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, sessionmaker


Base = declarative_base()


class PortfolioStateRecord(Base):
    __tablename__ = "portfolio_state"

    id = Column(Integer, primary_key=True)
    payload = Column(Text, nullable=False)
    updated_at = Column(Text, nullable=False)


class PaperPerformanceRecord(Base):
    __tablename__ = "paper_performance"
    id = Column(Integer, primary_key=True, autoincrement=True)
    recorded_at = Column(Text, nullable=False)
    payload = Column(Text, nullable=False)


class PortfolioStateStore:
    def __init__(self, database_url=None):
        database_url = database_url or os.getenv(
            "DATABASE_URL", "sqlite:///./data/portfolio_state.db"
        )
        # Connection dialogs may select psycopg v3. This application ships
        # psycopg2-binary, so use that driver while preserving credentials and
        # connection options (including SSL) in the supplied URL.
        parsed_url = make_url(database_url)
        if parsed_url.drivername in (
            "postgres", "postgresql", "postgresql+psycopg"
        ):
            database_url = parsed_url.set(drivername="postgresql+psycopg2")
        if parsed_url.get_backend_name() == "sqlite":
            sqlite_path = parsed_url.database or ""
            parent_directory = os.path.dirname(sqlite_path)
            if parent_directory:
                os.makedirs(parent_directory, exist_ok=True)
        connect_args = {"check_same_thread": False} if parsed_url.get_backend_name() == "sqlite" else {}
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

    def save(self, payload, performance=None):
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
            if performance is not None:
                session.add(PaperPerformanceRecord(
                    recorded_at=updated_at, payload=json.dumps(performance, sort_keys=True)
                ))
            session.commit()
        finally:
            session.close()
