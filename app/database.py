from collections.abc import Generator

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def build_engine(database_url: str) -> Engine:
    """Create an engine for `database_url`. Importing this module never connects."""
    kwargs: dict = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(database_url, **kwargs)
    if database_url.startswith("sqlite"):
        # SQLite ignores foreign keys unless asked; Postgres always enforces them, so make
        # local development behave the same way.
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db(request: Request) -> Generator[Session, None, None]:
    """Request-scoped session from the app that is serving the request."""
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()
