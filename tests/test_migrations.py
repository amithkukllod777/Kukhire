"""Migrations: fresh database, upgrade from the original schema, and history integrity."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from sqlalchemy import MetaData, Table, inspect, text
from sqlalchemy.exc import IntegrityError

import app.db_models  # noqa: F401  (populate Base.metadata)
import app.screening_models  # noqa: F401
from app import migrations
from app.database import Base, build_engine

# SHA-256 of migrations/0001_email_screening.sql as it exists on main. It was never applied
# anywhere we can see, but "never edit a migration" is only a rule if something enforces it.
ORIGINAL_0001_SHA = "0984c836c1847aa31b0afaf63c3ce7c0f4e293b6a1e33613a74af793ea8de2b8"


def test_original_migration_0001_is_untouched():
    path = migrations.MIGRATIONS_DIR / "0001_email_screening.sql"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == ORIGINAL_0001_SHA


def test_migrations_are_numbered_without_gaps_or_duplicates():
    found = migrations.discover()
    assert [m.version[:4] for m in found] == [f"{i:04d}" for i in range(len(found))]


def test_fresh_database_initializes_and_is_idempotent(database_url):
    engine = build_engine(database_url)
    applied = migrations.migrate(engine)
    assert applied == [m.version for m in migrations.discover()]
    assert migrations.migrate(engine) == []  # nothing left, nothing re-run
    assert set(migrations.applied_versions(engine)) == set(applied)
    assert migrations.pending(engine) == []


def test_every_mapped_column_exists_in_the_migrated_schema(engine):
    """The ORM and the migrations must describe the same tables (schema-drift guard)."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    problems = []
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            problems.append(f"missing table {table.name}")
            continue
        have = {c["name"] for c in inspector.get_columns(table.name)}
        problems += [f"{table.name}.{c.name}" for c in table.columns if c.name not in have]
    assert problems == []


def test_dry_run_reports_pending_without_applying(database_url):
    engine = build_engine(database_url)
    assert migrations.migrate(engine, dry_run=True) == [m.version for m in migrations.discover()]
    assert "organizations" not in inspect(engine).get_table_names()


def test_editing_an_applied_migration_is_refused(database_url, tmp_path: Path):
    work = tmp_path / "migrations"
    shutil.copytree(migrations.MIGRATIONS_DIR, work, ignore=shutil.ignore_patterns("__pycache__"))
    engine = build_engine(database_url)
    migrations.migrate(engine, work)
    (work / "0000_core_schema.sql").write_text((work / "0000_core_schema.sql").read_text() + "\n-- tampered\n")
    with pytest.raises(migrations.MigrationError, match="edited after it was applied"):
        migrations.pending(engine, work)


def test_unknown_files_are_ignored_but_duplicate_numbers_are_an_error(tmp_path: Path):
    (tmp_path / "0000_a.sql").write_text("SELECT 1;")
    (tmp_path / "0000_b.sql").write_text("SELECT 1;")
    (tmp_path / "notes.txt").write_text("ignored")
    with pytest.raises(migrations.MigrationError, match="share the number"):
        migrations.discover(tmp_path)


# --- upgrade from the ORIGINAL schema -------------------------------------------------------
def _legacy_state(database_url: str, tmp_path: Path):
    """A database at 0000+0001 holding data created under the old global-candidate model."""
    first_two = tmp_path / "legacy_migrations"
    first_two.mkdir()
    for name in ("0000_core_schema.sql", "0001_email_screening.sql"):
        shutil.copy(migrations.MIGRATIONS_DIR / name, first_two / name)
    engine = build_engine(database_url)
    assert migrations.migrate(engine, first_two) == ["0000_core_schema", "0001_email_screening"]

    md = MetaData()
    orgs, jobs, cands, apps = (Table(n, md, autoload_with=engine) for n in ("organizations", "jobs", "candidates", "applications"))
    with engine.begin() as conn:
        conn.execute(orgs.insert(), [{"id": "org-a", "name": "A"}, {"id": "org-b", "name": "B"}])
        conn.execute(jobs.insert(), [
            {"id": "job-a", "organization_id": "org-a", "title": "A job", "description": "d", "scoring_weights": {}},
            {"id": "job-b", "organization_id": "org-b", "title": "B job", "description": "d", "scoring_weights": {}},
        ])
        conn.execute(cands.insert(), [
            {"id": "c-only-a", "full_name": "Only A", "email": "a@x.test", "parsed_resume": {}},
            # The old schema had ONE row per email for every tenant: this person applied in both.
            {"id": "c-shared", "full_name": "Shared Person", "email": "shared@x.test", "parsed_resume": {"k": "v"}},
            {"id": "c-orphan", "full_name": "No Applications", "email": "orphan@x.test", "parsed_resume": {}},
        ])
        conn.execute(apps.insert(), [
            {"id": "app-1", "job_id": "job-a", "candidate_id": "c-only-a", "stage": "applied", "evaluation": {}},
            {"id": "app-2", "job_id": "job-a", "candidate_id": "c-shared", "stage": "interview", "evaluation": {}},
            {"id": "app-3", "job_id": "job-b", "candidate_id": "c-shared", "stage": "applied", "evaluation": {}},
        ])
    return engine


def test_upgrade_assigns_owners_splits_shared_candidates_and_keeps_orphans(database_url, tmp_path, capsys):
    engine = _legacy_state(database_url, tmp_path)
    applied = migrations.migrate(engine)
    assert applied[0] == "0002_tenant_scoped_ats"

    with engine.connect() as conn:
        owner = dict(conn.execute(text("SELECT id, organization_id FROM candidates")).all())
        app_org = dict(conn.execute(text("SELECT id, organization_id FROM applications")).all())
        total = conn.execute(text("SELECT COUNT(*) FROM candidates")).scalar()

    # Applications inherit their job's organization.
    assert app_org == {"app-1": "org-a", "app-2": "org-a", "app-3": "org-b"}
    # A candidate whose applications all sit in one org belongs to that org.
    assert owner["c-only-a"] == "org-a"
    # A candidate shared across orgs was SPLIT: each org has its own row, applications repointed.
    assert owner["c-shared"] == "org-a"
    assert total == 4  # 3 originals + 1 copy; nothing deleted
    with engine.connect() as conn:
        b_candidate = conn.execute(text("SELECT candidate_id FROM applications WHERE id='app-3'")).scalar()
        assert b_candidate != "c-shared"
        row = conn.execute(text("SELECT organization_id, full_name, email FROM candidates WHERE id=:i"), {"i": b_candidate}).one()
        assert tuple(row) == ("org-b", "Shared Person", "shared@x.test")
        # The one with no applications is NOT guessed into an org: kept, owner unknown.
        assert owner["c-orphan"] is None
    assert "candidates_unowned': 1" in capsys.readouterr().out


def test_upgrade_replaces_global_email_uniqueness_with_per_organization(database_url, tmp_path):
    engine = _legacy_state(database_url, tmp_path)
    migrations.migrate(engine)
    # Ask the database's own catalog: SQLAlchemy cannot reflect expression indexes on SQLite.
    catalog = (
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='candidates'"
        if engine.dialect.name == "sqlite"
        else "SELECT indexname FROM pg_indexes WHERE tablename='candidates'"
    )
    with engine.connect() as conn:
        names = {r[0] for r in conn.execute(text(catalog))}
    assert "uq_candidate_email_normalized" not in names
    assert "uq_candidate_org_email" in names

    def add(cid, org, email):
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO candidates (id, organization_id, full_name, email, parsed_resume) "
                     "VALUES (:i, :o, 'N', :e, '{}')"),
                {"i": cid, "o": org, "e": email},
            )

    add("x1", "org-a", "dup@x.test")
    add("x2", "org-b", "DUP@x.test")  # same email, other tenant: allowed (case-insensitive key)
    with pytest.raises(IntegrityError):
        add("x3", "org-a", "Dup@X.test")  # same tenant, same email: refused


def test_upgrade_is_safe_to_run_on_an_already_upgraded_schema(engine):
    """Re-running 0002's body (e.g. after a ledger restore) must not fail or duplicate anything."""
    import importlib.util

    path = migrations.MIGRATIONS_DIR / "0002_tenant_scoped_ats.py"
    spec = importlib.util.spec_from_file_location("m0002_again", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.begin() as conn:
        module.upgrade(conn)
        module.upgrade(conn)
