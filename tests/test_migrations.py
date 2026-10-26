from pathlib import Path

import pytest
from flask_migrate import downgrade, upgrade
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app import create_app
from app.extensions import db
from app.models import DEFAULT_SKILLS

MIGRATIONS_DIR = str(Path(__file__).resolve().parent.parent / "migrations")
USERS_REVISION = "62d578f528f3"


@pytest.fixture
def migration_app(tmp_path):
    db_file = (tmp_path / "migrate.sqlite3").as_posix()
    app = create_app("testing", {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}"})
    with app.app_context():
        yield app
        db.engine.dispose()


def test_migrations_create_and_remove_users_table(migration_app):
    upgrade(directory=MIGRATIONS_DIR)
    inspector = inspect(db.engine)
    columns = {c["name"] for c in inspector.get_columns("users")}
    assert {"id", "phone", "password_hash", "full_name", "role", "is_active", "created_at"} <= columns
    assert any(ix["unique"] and ix["column_names"] == ["phone"] for ix in inspector.get_indexes("users"))

    downgrade(directory=MIGRATIONS_DIR, revision="base")
    assert "users" not in inspect(db.engine).get_table_names()


def test_migrations_create_profile_tables_and_seed_skills(migration_app):
    upgrade(directory=MIGRATIONS_DIR)
    tables = set(inspect(db.engine).get_table_names())
    assert {"skills", "worker_profiles", "worker_skills"} <= tables

    with db.engine.connect() as conn:
        rows = conn.execute(text("SELECT code, name FROM skills ORDER BY sort_order")).all()
    # The model's list and the migration's frozen copy must stay identical.
    assert [tuple(row) for row in rows] == list(DEFAULT_SKILLS)


def test_migration_backfills_profiles_for_existing_workers(migration_app):
    upgrade(directory=MIGRATIONS_DIR, revision=USERS_REVISION)
    with db.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (phone, password_hash, full_name, role, is_active, created_at) VALUES "
            "('99110001', 'x', 'Ажилтан', 'worker', 1, CURRENT_TIMESTAMP), "
            "('99330001', 'x', 'Ажил олгогч', 'employer', 1, CURRENT_TIMESTAMP)"
        ))

    upgrade(directory=MIGRATIONS_DIR)
    with db.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT u.phone, p.availability FROM worker_profiles p JOIN users u ON u.id = p.user_id"
        )).all()
    assert [tuple(row) for row in rows] == [("99110001", "available")]


PROFILES_REVISION = "69a231cb3c3f"


def test_migrations_create_job_tables(migration_app):
    upgrade(directory=MIGRATIONS_DIR)
    inspector = inspect(db.engine)
    job_columns = {c["name"] for c in inspector.get_columns("jobs")}
    assert {
        "employer_id", "title", "description", "location", "work_date", "start_time", "end_time",
        "workers_needed", "wage_per_worker", "notes", "status", "created_at", "updated_at",
    } <= job_columns
    wage = next(c for c in inspector.get_columns("jobs") if c["name"] == "wage_per_worker")
    assert str(wage["type"]) == "INTEGER"
    assert "job_status_changes" in inspector.get_table_names()


JOBS_REVISION = "3fbff240cd66"


def _insert_basics(conn):
    """One worker, one employer/admin, and one accepted job, written with plain SQL."""
    conn.execute(text(
        "INSERT INTO users (id, phone, password_hash, full_name, role, is_active, created_at) VALUES "
        "(1, '99110001', 'x', 'Ажилтан', 'worker', 1, CURRENT_TIMESTAMP), "
        "(2, '99330001', 'x', 'Компани', 'employer', 1, CURRENT_TIMESTAMP), "
        "(3, '99000001', 'x', 'Админ', 'admin', 1, CURRENT_TIMESTAMP)"
    ))
    conn.execute(text(
        "INSERT INTO jobs (id, employer_id, title, description, location, work_date, start_time, end_time, "
        "workers_needed, wage_per_worker, status, created_at, updated_at) VALUES "
        "(1, 2, 'Ажил', 'Тайлбар', 'Хаяг', '2030-01-01', '09:00:00.000000', '13:00:00.000000', 2, 60000, "
        "'accepted', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    ))


def test_staffing_migration_keeps_existing_data(migration_app):
    upgrade(directory=MIGRATIONS_DIR, revision=JOBS_REVISION)
    with db.engine.begin() as conn:
        _insert_basics(conn)
    upgrade(directory=MIGRATIONS_DIR)
    tables = set(inspect(db.engine).get_table_names())
    assert {"job_applications", "assignments"} <= tables
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT title, status FROM jobs")).one() == ("Ажил", "accepted")


def test_application_uniqueness_constraint(migration_app):
    upgrade(directory=MIGRATIONS_DIR)
    insert = text("INSERT INTO job_applications (job_id, worker_id, status, created_at, updated_at) "
                  "VALUES (1, 1, 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    with db.engine.begin() as conn:
        _insert_basics(conn)
        conn.execute(insert)
    with pytest.raises(IntegrityError):
        with db.engine.begin() as conn:
            conn.execute(insert)


def test_only_one_active_assignment_per_worker_and_job(migration_app):
    upgrade(directory=MIGRATIONS_DIR)
    insert = ("INSERT INTO assignments (job_id, worker_id, agreed_wage, status, created_at, created_by_id) "
              "VALUES (1, 1, 60000, '{status}', CURRENT_TIMESTAMP, 3)")
    with db.engine.begin() as conn:
        _insert_basics(conn)
        conn.execute(text(insert.format(status="cancelled")))
        conn.execute(text(insert.format(status="cancelled")))  # history rows are allowed
        conn.execute(text(insert.format(status="active")))
    with pytest.raises(IntegrityError):
        with db.engine.begin() as conn:
            conn.execute(text(insert.format(status="active")))
    with pytest.raises(IntegrityError):
        with db.engine.begin() as conn:
            conn.execute(text(insert.format(status="active").replace("60000", "0")))


def test_staffing_migration_downgrades_cleanly(migration_app):
    upgrade(directory=MIGRATIONS_DIR)
    downgrade(directory=MIGRATIONS_DIR, revision=JOBS_REVISION)
    tables = set(inspect(db.engine).get_table_names())
    assert "jobs" in tables
    assert not {"job_applications", "assignments"} & tables


STAFFING_REVISION = "d62bf80e0911"


def test_milestone5_migration_preserves_existing_records(migration_app):
    """Data written before the migration is still there afterwards; old assignments
    start as 'not checked' and old employers have no company name."""
    upgrade(directory=MIGRATIONS_DIR, revision=STAFFING_REVISION)
    with db.engine.begin() as conn:
        _insert_basics(conn)
        conn.execute(text(
            "INSERT INTO job_applications (job_id, worker_id, status, created_at, updated_at) "
            "VALUES (1, 1, 'approved', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        conn.execute(text(
            "INSERT INTO assignments (job_id, worker_id, agreed_wage, status, created_at, created_by_id) "
            "VALUES (1, 1, 60000, 'active', CURRENT_TIMESTAMP, 3)"
        ))

    upgrade(directory=MIGRATIONS_DIR)
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM users")).scalar() == 3
        assert conn.execute(text("SELECT company_name FROM users WHERE role = 'employer'")).scalar() is None
        assert conn.execute(text("SELECT status FROM job_applications")).scalar() == "approved"
        assert conn.execute(text("SELECT agreed_wage, status, attendance FROM assignments")).one() == \
            (60000, "active", "not_checked")
    tables = set(inspect(db.engine).get_table_names())
    assert {"notifications", "attendance_changes"} <= tables


def test_partial_unique_index_survives_milestone5_round_trip(migration_app):
    """Adding (and on downgrade, dropping) assignment columns must not turn the
    'one active assignment' index into a plain unique index."""
    upgrade(directory=MIGRATIONS_DIR)
    downgrade(directory=MIGRATIONS_DIR, revision=STAFFING_REVISION)
    upgrade(directory=MIGRATIONS_DIR)
    insert =("INSERT INTO assignments (job_id, worker_id, agreed_wage, status, created_at, created_by_id) "
              "VALUES (1, 1, 60000, '{status}', CURRENT_TIMESTAMP, 3)")
    with db.engine.begin() as conn:
        _insert_basics(conn)
        conn.execute(text(insert.format(status="cancelled")))
        conn.execute(text(insert.format(status="cancelled")))
        conn.execute(text(insert.format(status="active")))
    with pytest.raises(IntegrityError):
        with db.engine.begin() as conn:
            conn.execute(text(insert.format(status="active")))


def test_notification_uniqueness_in_migrated_schema(migration_app):
    upgrade(directory=MIGRATIONS_DIR)
    insert = text("INSERT INTO notifications (recipient_id, event_key, title, message, created_at) "
                  "VALUES (1, 'job:1:accepted', 't', 'm', CURRENT_TIMESTAMP)")
    with db.engine.begin() as conn:
        _insert_basics(conn)
        conn.execute(insert)
    with pytest.raises(IntegrityError):
        with db.engine.begin() as conn:
            conn.execute(insert)


def test_milestone5_migration_downgrades_cleanly(migration_app):
    upgrade(directory=MIGRATIONS_DIR)
    downgrade(directory=MIGRATIONS_DIR, revision=STAFFING_REVISION)
    inspector = inspect(db.engine)
    assert not {"notifications", "attendance_changes"} & set(inspector.get_table_names())
    assert "attendance" not in {c["name"] for c in inspector.get_columns("assignments")}
    assert "company_name" not in {c["name"] for c in inspector.get_columns("users")}


def test_job_migration_downgrades_cleanly(migration_app):
    upgrade(directory=MIGRATIONS_DIR)
    downgrade(directory=MIGRATIONS_DIR, revision=PROFILES_REVISION)
    tables = set(inspect(db.engine).get_table_names())
    assert "worker_profiles" in tables
    assert not {"jobs", "job_status_changes"} & tables


def test_profile_migration_downgrades_cleanly(migration_app):
    upgrade(directory=MIGRATIONS_DIR)
    downgrade(directory=MIGRATIONS_DIR, revision=USERS_REVISION)
    tables = set(inspect(db.engine).get_table_names())
    assert "users" in tables
    assert not {"skills", "worker_profiles", "worker_skills"} & tables
