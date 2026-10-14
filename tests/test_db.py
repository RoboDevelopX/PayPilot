import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from app.db import init_db, make_engine, make_session_factory, session_scope
from app.models import AuditEvent


def test_audit_log_is_append_only():
    engine = make_engine("sqlite://")
    init_db(engine)
    sessions = make_session_factory(engine)
    with session_scope(sessions) as s:
        s.add(AuditEvent(actor="system", event="test", detail={"x": 1}))
    with pytest.raises(DatabaseError, match="append-only"), engine.begin() as conn:
        conn.execute(text("UPDATE audit_events SET event='tampered'"))
    with pytest.raises(DatabaseError, match="append-only"), engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_events"))
