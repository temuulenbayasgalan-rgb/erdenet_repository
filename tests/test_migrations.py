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


M5_REVISION = "00e6588d75fe"
FINANCE_TABLES = {"commission_rates", "job_finances", "job_finance_lines", "ledger_entries"}


def test_finance_migration_keeps_legacy_completed_job_unpaid(migration_app):
    """A job completed before Milestone 6 gets no invented snapshot, commission or payment."""
    upgrade(directory=MIGRATIONS_DIR, revision=M5_REVISION)
    with db.engine.begin() as conn:
        _insert_basics(conn)
        conn.execute(text("UPDATE jobs SET status = 'completed' WHERE id = 1"))
        conn.execute(text(
            "INSERT INTO assignments (job_id, worker_id, agreed_wage, status, created_at, created_by_id, attendance) "
            "VALUES (1, 1, 60000, 'active', CURRENT_TIMESTAMP, 3, 'present')"
        ))
    upgrade(directory=MIGRATIONS_DIR)

    assert FINANCE_TABLES <= set(inspect(db.engine).get_table_names())
    with db.engine.connect() as conn:
        for table in FINANCE_TABLES:
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0, table
        assert conn.execute(text("SELECT status FROM jobs")).scalar() == "completed"
        assert conn.execute(text("SELECT agreed_wage, attendance FROM assignments")).one() == (60000, "present")

    # The app reports it as needing finance setup, not as paid.
    from app.models import Job
    from app.services.finance import FinanceState, finance_state
    assert finance_state(db.session.get(Job, 1)) == FinanceState.AWAITING_SETUP


def test_finance_constraints_in_migrated_schema(migration_app):
    upgrade(directory=MIGRATIONS_DIR)
    with db.engine.begin() as conn:
        _insert_basics(conn)
        conn.execute(text("INSERT INTO commission_rates (id, amount_per_worker, created_by_id, created_at) "
                          "VALUES (1, 10000, 3, CURRENT_TIMESTAMP)"))
        conn.execute(text(
            "INSERT INTO job_finances (id, job_id, commission_rate_id, commission_per_worker, total_wages, "
            "total_commission, total_due, calculation_version, created_by_id, created_at) "
            "VALUES (1, 1, 1, 10000, 60000, 10000, 70000, 1, 3, CURRENT_TIMESTAMP)"
        ))
    bad_statements = [
        # second snapshot for the same job
        "INSERT INTO job_finances (job_id, commission_rate_id, commission_per_worker, total_wages, total_commission, "
        "total_due, calculation_version, created_by_id, created_at) VALUES (1, 1, 0, 1, 0, 1, 1, 3, CURRENT_TIMESTAMP)",
        # zero amount
        "INSERT INTO ledger_entries (public_id, kind, finance_id, amount, method, occurred_at, created_at, created_by_id) "
        "VALUES ('a', 'receipt', 1, 0, 'cash', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 3)",
        # reversal without original
        "INSERT INTO ledger_entries (public_id, kind, finance_id, amount, method, reason, occurred_at, created_at, "
        "created_by_id) VALUES ('b', 'receipt_reversal', 1, 10, 'cash', 'x', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 3)",
        # payout without a worker line
        "INSERT INTO ledger_entries (public_id, kind, finance_id, amount, method, occurred_at, created_at, created_by_id) "
        "VALUES ('c', 'payout', 1, 10, 'cash', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 3)",
        # negative commission rate
        "INSERT INTO commission_rates (amount_per_worker, created_by_id, created_at) VALUES (-1, 3, CURRENT_TIMESTAMP)",
    ]
    for statement in bad_statements:
        with pytest.raises(IntegrityError):
            with db.engine.begin() as conn:
                conn.execute(text(statement))


def test_partial_unique_index_intact_after_finance_migration(migration_app):
    upgrade(directory=MIGRATIONS_DIR)
    with db.engine.connect() as conn:
        sql = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE name = 'uq_assignments_active_job_worker'")).scalar()
    assert "WHERE status = 'active'" in sql


def test_finance_migration_downgrades_cleanly(migration_app):
    upgrade(directory=MIGRATIONS_DIR)
    downgrade(directory=MIGRATIONS_DIR, revision=M5_REVISION)
    tables = set(inspect(db.engine).get_table_names())
    assert not FINANCE_TABLES & tables
    assert {"notifications", "assignments", "jobs"} <= tables


FINANCE_REVISION = "5a4f9d0cc97e"
LISTING_TABLES = {"job_listings", "listing_changes", "referrals"}


def test_listing_migration_keeps_existing_jobs_unpublished(migration_app):
    """Existing jobs get no listing, so nothing becomes public by itself."""
    upgrade(directory=MIGRATIONS_DIR, revision=FINANCE_REVISION)
    with db.engine.begin() as conn:
        _insert_basics(conn)
    upgrade(directory=MIGRATIONS_DIR)
    assert LISTING_TABLES <= set(inspect(db.engine).get_table_names())
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT title, status FROM jobs")).one() == ("Ажил", "accepted")
        for table in LISTING_TABLES:
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0
    from app.services.listings import public_jobs
    assert public_jobs() == []


def test_referral_constraints_in_migrated_schema(migration_app):
    upgrade(directory=MIGRATIONS_DIR)
    insert = ("INSERT INTO referrals (job_id, worker_id, caller_phone, caller_name, status, created_at, created_by_id) "
              "VALUES (1, {worker}, {phone}, {name}, '{status}', CURRENT_TIMESTAMP, 3)")
    with db.engine.begin() as conn:
        _insert_basics(conn)
        conn.execute(text(insert.format(worker="NULL", phone="'88001122'", name="NULL", status="cancelled")))
        conn.execute(text(insert.format(worker="NULL", phone="'88001122'", name="NULL", status="referred")))
        conn.execute(text(insert.format(worker=1, phone="NULL", name="NULL", status="referred")))
    bad = [
        insert.format(worker="NULL", phone="'88001122'", name="NULL", status="referred"),  # duplicate caller
        insert.format(worker=1, phone="NULL", name="NULL", status="referred"),             # duplicate worker
        insert.format(worker="NULL", phone="NULL", name="NULL", status="referred"),        # nobody identified
        insert.format(worker="NULL", phone="'88009999'", name="NULL", status="placed"),    # placed w/o assignment
        insert.format(worker="NULL", phone="'88008888'", name="NULL", status="hired"),     # unknown status
    ]
    for statement in bad:
        with pytest.raises(IntegrityError):
            with db.engine.begin() as conn:
                conn.execute(text(statement))


def test_listing_coordinate_check_in_migrated_schema(migration_app):
    upgrade(directory=MIGRATIONS_DIR)
    insert = ("INSERT INTO job_listings (job_id, public_code, public_area, latitude, longitude, updated_at, "
              "updated_by_id) VALUES (1, '{code}', 'Эрдэнэт', {lat}, {lng}, CURRENT_TIMESTAMP, 3)")
    with db.engine.begin() as conn:
        _insert_basics(conn)
        conn.execute(text(insert.format(code="a", lat="49.03", lng="104.05")))
    for code, lat, lng in (("b", "49.03", "NULL"), ("c", "95", "104")):
        with pytest.raises(IntegrityError):
            with db.engine.begin() as conn:
                conn.execute(text(insert.format(code=code, lat=lat, lng=lng).replace("(1,", "(2,")))


def test_partial_indexes_intact_after_listing_migration(migration_app):
    upgrade(directory=MIGRATIONS_DIR)
    with db.engine.connect() as conn:
        sql = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE name = 'uq_assignments_active_job_worker'")).scalar()
    assert "WHERE status = 'active'" in sql


def test_listing_migration_downgrades_cleanly(migration_app):
    upgrade(directory=MIGRATIONS_DIR)
    downgrade(directory=MIGRATIONS_DIR, revision=FINANCE_REVISION)
    tables = set(inspect(db.engine).get_table_names())
    assert not LISTING_TABLES & tables
    assert {"jobs", "ledger_entries", "assignments"} <= tables


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


# --- Milestone 8: application history, disputes, supervisor details ------------------------------

LISTING_REVISION = "a7316c1bed61"
M8_TABLES = {"application_changes", "attendance_disputes"}
PRESERVED_TABLES = ("users", "jobs", "job_listings", "job_applications", "assignments",
                    "attendance_changes", "referrals", "job_status_changes")


def _insert_marketplace_history(conn):
    """Realistic Milestone 7 data: a published listing, a legacy approved application,
    an active assignment with attendance, a placed referral and an open referral."""
    _insert_basics(conn)
    conn.execute(text(
        "INSERT INTO users (id, phone, password_hash, full_name, role, is_active, created_at) VALUES "
        "(4, '99110002', 'x', 'Хоёрдугаар', 'worker', 1, CURRENT_TIMESTAMP)"))
    conn.execute(text(
        "INSERT INTO job_listings (id, job_id, public_code, public_area, is_published, published_at, updated_at, "
        "updated_by_id) VALUES (1, 1, 'legacy01', 'Эрдэнэт', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 3)"))
    conn.execute(text(
        "INSERT INTO job_applications (id, job_id, worker_id, status, created_at, updated_at, reviewed_by_id, "
        "reviewed_at) VALUES (1, 1, 1, 'approved', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 3, CURRENT_TIMESTAMP), "
        "(2, 1, 4, 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL, NULL)"))
    conn.execute(text(
        "INSERT INTO assignments (id, job_id, worker_id, agreed_wage, status, created_at, created_by_id, attendance) "
        "VALUES (1, 1, 1, 60000, 'active', CURRENT_TIMESTAMP, 3, 'present')"))
    conn.execute(text(
        "INSERT INTO attendance_changes (assignment_id, from_status, to_status, changed_by_id, created_at) "
        "VALUES (1, 'not_checked', 'present', 3, CURRENT_TIMESTAMP)"))
    conn.execute(text(
        "INSERT INTO referrals (job_id, worker_id, caller_phone, status, created_at, created_by_id, assignment_id) "
        "VALUES (1, 1, NULL, 'placed', CURRENT_TIMESTAMP, 3, 1), "
        "(1, NULL, '88001122', 'referred', CURRENT_TIMESTAMP, 3, NULL)"))


def _rows(conn, table, columns=None):
    cols = ", ".join(columns) if columns else "*"
    return [tuple(r) for r in conn.execute(text(f"SELECT {cols} FROM {table} ORDER BY id"))]


def test_m8_migration_preserves_every_existing_row(migration_app):
    upgrade(directory=MIGRATIONS_DIR, revision=LISTING_REVISION)
    with db.engine.begin() as conn:
        _insert_marketplace_history(conn)
    with db.engine.connect() as conn:
        before = {t: _rows(conn, t) for t in PRESERVED_TABLES}
    upgrade(directory=MIGRATIONS_DIR)
    assert M8_TABLES <= set(inspect(db.engine).get_table_names())
    with db.engine.connect() as conn:
        for table, rows in before.items():
            columns = [c["name"] for c in inspect(db.engine).get_columns(table)][: len(rows[0]) if rows else None]
            assert _rows(conn, table, columns) == rows, table
        # New columns get harmless defaults; nothing is reinterpreted.
        assert conn.execute(text("SELECT cancel_reason FROM assignments")).scalar() == ""
        assert conn.execute(text(
            "SELECT supervisor_name, supervisor_phone, worker_instructions FROM job_listings")).one() == (None, None, "")
        for table in M8_TABLES:
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0


def test_m8_migration_legacy_records_still_work_with_the_models(migration_app):
    upgrade(directory=MIGRATIONS_DIR, revision=LISTING_REVISION)
    with db.engine.begin() as conn:
        _insert_marketplace_history(conn)
    upgrade(directory=MIGRATIONS_DIR)
    from app.models import Job, JobApplication
    from app.services.staffing import positions_taken
    job = db.session.get(Job, 1)
    # The placed referral is counted once (through its assignment), the open one holds a slot.
    assert positions_taken(1) == 2 and job.positions_remaining == 0
    legacy = db.session.get(JobApplication, 1)
    assert legacy.status == "approved" and legacy.assignment is None and legacy.changes == []
    assert db.session.get(JobApplication, 2).status == "pending"


def test_m8_constraints_in_migrated_schema(migration_app):
    upgrade(directory=MIGRATIONS_DIR)
    with db.engine.begin() as conn:
        _insert_marketplace_history(conn)
        conn.execute(text("INSERT INTO attendance_disputes (assignment_id, raised_by_id, raised_role, message, "
                          "status, created_at) VALUES (1, 1, 'worker', 'Ирсэн', 'open', CURRENT_TIMESTAMP)"))
    dispute = ("INSERT INTO attendance_disputes (assignment_id, raised_by_id, raised_role, message, status, "
               "created_at, resolved_by_id, resolved_at, resolution) VALUES (1, {by}, '{role}', '{msg}', '{status}', "
               "CURRENT_TIMESTAMP, {rby}, {rat}, '{res}')")
    bad = [
        dispute.format(by=1, role="worker", msg="Дахин", status="open", rby="NULL", rat="NULL", res=""),  # 2nd open
        dispute.format(by=2, role="boss", msg="x", status="open", rby="NULL", rat="NULL", res=""),
        dispute.format(by=2, role="employer", msg="", status="open", rby="NULL", rat="NULL", res=""),
        dispute.format(by=2, role="employer", msg="x", status="resolved", rby="NULL", rat="NULL", res="ok"),
        dispute.format(by=2, role="employer", msg="x", status="resolved", rby=3, rat="CURRENT_TIMESTAMP", res=""),
        "INSERT INTO application_changes (application_id, action, changed_by_id, created_at) "
        "VALUES (1, 'hired', 3, CURRENT_TIMESTAMP)",
    ]
    for statement in bad:
        with pytest.raises(IntegrityError):
            with db.engine.begin() as conn:
                conn.execute(text(statement))


def test_older_constraints_intact_after_m8_migration(migration_app):
    upgrade(directory=MIGRATIONS_DIR)
    with db.engine.connect() as conn:
        index = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE name = 'uq_assignments_active_job_worker'")).scalar()
        assignments = conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'assignments'")).scalar()
        listings = conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'job_listings'")).scalar()
    assert "WHERE status = 'active'" in index
    assert "ck_assignments_agreed_wage_positive" in assignments and "ck_assignments_status" in assignments
    assert "ck_job_listings_coordinates" in listings


def test_m8_migration_downgrades_cleanly_keeping_data(migration_app):
    upgrade(directory=MIGRATIONS_DIR, revision=LISTING_REVISION)
    with db.engine.begin() as conn:
        _insert_marketplace_history(conn)
    with db.engine.connect() as conn:
        before = {t: _rows(conn, t) for t in PRESERVED_TABLES}
    upgrade(directory=MIGRATIONS_DIR)
    downgrade(directory=MIGRATIONS_DIR, revision=LISTING_REVISION)
    inspector = inspect(db.engine)
    assert not M8_TABLES & set(inspector.get_table_names())
    assert "cancel_reason" not in {c["name"] for c in inspector.get_columns("assignments")}
    with db.engine.connect() as conn:
        for table, rows in before.items():
            assert _rows(conn, table) == rows, table
        assert "ck_assignments_agreed_wage_positive" in conn.execute(
            text("SELECT sql FROM sqlite_master WHERE name = 'assignments'")).scalar()
