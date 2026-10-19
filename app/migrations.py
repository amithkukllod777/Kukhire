"""Ordered, ledger-tracked schema migrations.

Files in ``migrations/`` named ``NNNN_description.sql`` or ``NNNN_description.py`` are applied in
filename order. Each applied migration is recorded in ``schema_migrations`` with the SHA-256 of
its file, so:

* a fresh database runs everything, in order;
* an existing database runs only what it has not run;
* editing a migration that has already been applied is refused rather than silently ignored
  (history is append-only — add a new migration instead).

``.sql`` migrations are split on statement-ending semicolons and must not contain procedural code.
``.py`` migrations expose ``upgrade(connection)`` and are used where the change needs
introspection or data movement that plain SQL cannot express portably (SQLite for local
development, PostgreSQL for production).
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_NAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.(sql|py)$")


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    checksum: str

    @property
    def kind(self) -> str:
        return self.path.suffix.lstrip(".")


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found: list[Migration] = []
    seen: set[str] = set()
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name.startswith("__"):
            continue
        match = _NAME_RE.match(path.name)
        if not match:
            continue
        if match.group(1) in seen:
            raise MigrationError(f"Two migrations share the number {match.group(1)}")
        seen.add(match.group(1))
        found.append(Migration(path.stem, path, hashlib.sha256(path.read_bytes()).hexdigest()))
    return found


def _statements(sql: str) -> list[str]:
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def _load_python(migration: Migration):
    spec = importlib.util.spec_from_file_location(f"kukhire_migration_{migration.version}", migration.path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec and spec.loader
    spec.loader.exec_module(module)
    if not callable(getattr(module, "upgrade", None)):
        raise MigrationError(f"{migration.path.name} must define upgrade(connection)")
    return module


def _ensure_ledger(conn: Connection) -> None:
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version VARCHAR(120) PRIMARY KEY, "
            "checksum VARCHAR(64) NOT NULL, "
            "applied_at VARCHAR(40) NOT NULL)"
        )
    )


def applied_versions(engine: Engine) -> dict[str, str]:
    with engine.begin() as conn:
        _ensure_ledger(conn)
        rows = conn.execute(text("SELECT version, checksum FROM schema_migrations")).all()
    return {r[0]: r[1] for r in rows}


def pending(engine: Engine, directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Migrations not yet applied. Raises if an applied one was edited after the fact."""
    done = applied_versions(engine)
    todo: list[Migration] = []
    for migration in discover(directory):
        if migration.version in done:
            if done[migration.version] != migration.checksum:
                raise MigrationError(
                    f"{migration.path.name} was edited after it was applied "
                    f"(ledger {done[migration.version][:12]} != file {migration.checksum[:12]}). "
                    "Never edit an applied migration; add a new one."
                )
        else:
            todo.append(migration)
    return todo


def migrate(engine: Engine, directory: Path = MIGRATIONS_DIR, *, dry_run: bool = False) -> list[str]:
    """Apply pending migrations in order. Returns the versions applied (or that would be)."""
    todo = pending(engine, directory)
    if dry_run:
        return [m.version for m in todo]
    applied: list[str] = []
    for migration in todo:
        # One transaction per migration: PostgreSQL rolls the whole file back on failure.
        with engine.begin() as conn:
            _ensure_ledger(conn)
            if migration.kind == "sql":
                for statement in _statements(migration.path.read_text(encoding="utf-8")):
                    conn.execute(text(statement))
            else:
                _load_python(migration).upgrade(conn)
            conn.execute(
                text("INSERT INTO schema_migrations (version, checksum, applied_at) VALUES (:v, :c, :t)"),
                {"v": migration.version, "c": migration.checksum, "t": datetime.now(timezone.utc).isoformat()},
            )
        applied.append(migration.version)
    return applied
