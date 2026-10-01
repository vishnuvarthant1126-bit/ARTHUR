"""SQLite database connection.

SQLite is a full SQL database stored in one file (data/arthur.db) - no
server to install. SQLAlchemy lets us describe tables as Python classes
(see models.py) and swap to PostgreSQL later by changing one URL.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import Base


class Database:
    def __init__(self, path: Path | str, *, busy_timeout_seconds: float = 5.0) -> None:
        # check_same_thread=False: we call the DB from worker threads (asyncio.to_thread).
        # timeout: how long SQLite itself waits for another connection's lock.
        connect_args = {"check_same_thread": False, "timeout": busy_timeout_seconds}
        if str(path) == ":memory:":
            # In-memory database for tests. StaticPool = one shared connection,
            # otherwise every connection would see its own empty database.
            self.engine: Engine = create_engine(
                "sqlite://", connect_args=connect_args, poolclass=StaticPool
            )
        else:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.engine = create_engine(f"sqlite:///{path}", connect_args=connect_args)
        event.listen(self.engine, "connect", _sqlite_pragmas)
        # One lock for all database work in this process. SQLite allows a single writer;
        # a second one waits by sleep-and-retry (not a fair queue) and gives up with
        # "database is locked" after the timeout - seen in the load test after a 6 s wait.
        # Queuing here means SQLite never has to make anyone wait. Each use is a few
        # milliseconds, so the queue stays short. (RLock: the same thread may nest.)
        self._lock = threading.RLock()
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def create_tables(self) -> None:
        """Create any missing tables. Safe to call on every startup."""
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """A unit of work: commits if the block succeeds, rolls back if it fails."""
        with self._lock, self._sessions() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    def close(self) -> None:
        self.engine.dispose()


def _sqlite_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
