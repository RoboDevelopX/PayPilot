"""SQLAlchemy engine and session factory.

SQLite is enough for a single-instance prototype. The audit table is made
append-only *in the database itself* with triggers, so even a buggy code path
cannot rewrite history.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


APPEND_ONLY_TRIGGERS = [
    """CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
       BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;""",
    """CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
       BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;""",
]


def make_engine(url: str) -> Engine:
    kwargs: dict = {}
    if url.startswith("sqlite"):
        # FastAPI runs sync endpoints in a thread pool, so allow cross-thread use.
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool  # one shared in-memory DB (tests)
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    return engine


def init_db(engine: Engine) -> None:
    from app import models  # noqa: F401  (registers tables on Base.metadata)

    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for ddl in APPEND_ONLY_TRIGGERS:
            conn.execute(text(ddl))


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
