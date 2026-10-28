"""Durable state for scheduled paper-trading cycles.

The scheduler starts a new Python process for every invocation. This store
keeps the portfolio ledger outside that process so positions and risk controls
survive between runs. SQLite is convenient locally; a PostgreSQL URL can be
supplied through ``DATABASE_URL`` for cloud deployment.
"""

import json
import os
import hashlib
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, Text, create_engine, select, text
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


class ScopedPortfolioRecord(Base):
    __tablename__ = "portfolio_state_scoped"
    scope = Column(String(160), primary_key=True)
    payload = Column(Text, nullable=False)
    updated_at = Column(Text, nullable=False)


class OrderJournalRecord(Base):
    __tablename__ = "order_journal"
    client_id = Column(String(36), primary_key=True)
    scope = Column(String(160), nullable=False, index=True)
    payload = Column(Text, nullable=False)


class PortfolioStateStore:
    def __init__(self, database_url=None, mode="paper", account_id="default"):
        if mode not in {"paper", "live", "testnet"}:
            raise ValueError("Unknown portfolio mode")
        if not account_id or len(account_id) > 100:
            raise ValueError("PORTFOLIO_ACCOUNT_ID must contain 1–100 characters")
        self.scope = f"{mode}:{account_id}"
        self.mode = mode
        self._lock_connection = None
        self._locked = False
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
            record = session.get(ScopedPortfolioRecord, self.scope)
            if record:
                return json.loads(record.payload)
            # Preserve the existing paper portfolio. Never import it into live.
            if self.scope == "paper:default":
                legacy = session.get(PortfolioStateRecord, 1)
                return json.loads(legacy.payload) if legacy else None
            return None
        finally:
            session.close()

    def save(self, payload, applied_orders=(), performance=None):
        self.assert_lock_for_live()
        session = self.session_factory()
        try:
            record = session.get(ScopedPortfolioRecord, self.scope)
            encoded = json.dumps(payload, sort_keys=True)
            updated_at = datetime.now(timezone.utc).isoformat()
            if record is None:
                record = ScopedPortfolioRecord(
                    scope=self.scope,
                    payload=encoded,
                    updated_at=updated_at,
                )
                session.add(record)
            else:
                record.payload = encoded
                record.updated_at = updated_at
            if performance is not None:
                if self.mode != "paper":
                    raise ValueError("Paper performance cannot be recorded for a live portfolio")
                session.add(PaperPerformanceRecord(
                    recorded_at=updated_at,
                    payload=json.dumps({**performance, "scope": self.scope}, sort_keys=True),
                ))
            for client_id in applied_orders:
                journal = session.get(OrderJournalRecord, client_id)
                if journal is None or journal.scope != self.scope:
                    raise RuntimeError("Order journal scope mismatch")
                order = json.loads(journal.payload)
                order["applied"] = True
                journal.payload = json.dumps(order)
            session.commit()
        finally:
            session.close()

    def assert_lock_for_live(self):
        if self.mode == "paper":
            return
        if not self._locked:
            raise RuntimeError("Live state and orders require the portfolio lock")
        if self._lock_connection is not None:
            # A lost transaction must prevent any further exchange submission.
            self._lock_connection.execute(text("SELECT 1"))

    @contextmanager
    def cycle_lock(self):
        """One portfolio writer across schedulers, monitors and local processes."""
        if self._locked:
            raise RuntimeError("Portfolio lock is already held")
        handle = None
        connection = None
        try:
            if self.engine.dialect.name == "postgresql":
                connection = self.engine.connect()
                connection.begin()
                lock_id = int.from_bytes(hashlib.sha256(self.scope.encode()).digest()[:8], "big", signed=True)
                acquired = connection.execute(
                    text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock_id}
                ).scalar()
                if not acquired:
                    raise RuntimeError("Another cycle is managing this portfolio")
                self._lock_connection = connection
            elif self.engine.dialect.name == "sqlite":
                path = self.engine.url.database
                if path and path != ":memory:":
                    handle = open(path + "." + hashlib.sha256(self.scope.encode()).hexdigest()[:16] + ".lock", "a+")
                    try:
                        if sys.platform == "win32":
                            import msvcrt
                            handle.seek(0, 2)
                            if handle.tell() == 0:
                                handle.write("0")
                                handle.flush()
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        raise RuntimeError("Another cycle is managing this portfolio") from None
            else:
                raise RuntimeError("Portfolio locking supports PostgreSQL and SQLite only")
            self._locked = True
            yield
        finally:
            self._locked = False
            self._lock_connection = None
            if connection is not None:
                connection.rollback()
                connection.close()
            if handle is not None:
                handle.close()

    def journal_orders(self):
        with self.session_factory() as session:
            records = session.scalars(select(OrderJournalRecord).where(
                OrderJournalRecord.scope == self.scope
            )).all()
            return [json.loads(record.payload) for record in records]

    def put_order(self, payload):
        self.assert_lock_for_live()
        with self.session_factory() as session:
            record = session.get(OrderJournalRecord, payload["client_id"])
            if record is not None and record.scope != self.scope:
                raise RuntimeError("Order journal scope mismatch")
            if record is None:
                record = OrderJournalRecord(client_id=payload["client_id"], scope=self.scope)
                session.add(record)
            record.payload = json.dumps(payload)
            session.commit()
