"""Assignment and application rules in app/services/staffing.py."""
import threading
from datetime import time, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app import create_app
from app.extensions import db
from app.models import (
    ApplicationStatus,
    Assignment,
    AssignmentStatus,
    Availability,
    Job,
    JobApplication,
    JobStatus,
    Role,
    User,
)
from app.services import staffing
from app.services.staffing import StaffingError, apply_to_job, assign_worker, eligible_workers_query
from app.validation import office_today
from tests.conftest import seed_default_skills, tomorrow


@pytest.fixture
def employer(make_user):
    return make_user(phone="99330001", role=Role.EMPLOYER, full_name="Эрдэнэ Трейд ХХК")


@pytest.fixture
def job(make_job, employer):
    return make_job(employer, status=JobStatus.ACCEPTED, workers_needed=2,
                    start_time=time(9), end_time=time(13))


def active_count(job):
    return db.session.scalar(
        db.select(db.func.count(Assignment.id)).filter_by(job_id=job.id, status=AssignmentStatus.ACTIVE)
    )


# --- successful assignment ---------------------------------------------------------

def test_assignment_is_created_with_integer_wage(job, make_worker, assign, admin_user):
    worker = make_worker("99110001")
    assignment = assign(job, worker, agreed_wage=65000)
    assert assignment.status == AssignmentStatus.ACTIVE
    assert assignment.agreed_wage == 65000 and isinstance(assignment.agreed_wage, int)
    assert assignment.created_by_id == admin_user.id
    assert assignment.created_at is not None


def test_assignment_approves_pending_application(job, make_worker, assign, admin_user):
    worker = make_worker("99110001")
    application = apply_to_job(job, worker)
    assign(job, worker)
    db.session.expire_all()
    assert application.status == ApplicationStatus.APPROVED
    assert application.reviewed_by_id == admin_user.id


def test_worker_without_application_can_be_assigned(job, make_worker, assign):
    assign(job, make_worker("99110001"))
    assert active_count(job) == 1


# --- each rule ------------------------------------------------------------------------

def test_non_admin_cannot_assign(job, make_worker, employer):
    worker = make_worker("99110001")
    with pytest.raises(StaffingError, match="Зөвхөн админ"):
        assign_worker(job.id, worker.id, 60000, employer.id)
    with pytest.raises(StaffingError, match="Зөвхөн админ"):
        assign_worker(job.id, worker.id, 60000, worker.id)
    assert active_count(job) == 0


@pytest.mark.parametrize("status", [
    JobStatus.PENDING, JobStatus.IN_PROGRESS, JobStatus.COMPLETED, JobStatus.CANCELLED,
])
def test_only_accepted_jobs_can_be_staffed(make_job, employer, make_worker, assign, status):
    other_job = make_job(employer, status=status)
    with pytest.raises(StaffingError, match="Зөвхөн зөвшөөрсөн"):
        assign(other_job, make_worker("99110001"))


def test_past_job_cannot_be_staffed(make_job, employer, make_worker, assign):
    past = make_job(employer, status=JobStatus.ACCEPTED, work_date=office_today() - timedelta(days=1))
    with pytest.raises(StaffingError, match="хугацаа нь өнгөрөөгүй"):
        assign(past, make_worker("99110001"))


def test_missing_job_or_worker(job, admin_user, make_worker):
    with pytest.raises(StaffingError, match="Ажил олдсонгүй"):
        assign_worker(99999, make_worker("99110001").id, 60000, admin_user.id)
    with pytest.raises(StaffingError, match="Ажилтан олдсонгүй"):
        assign_worker(job.id, 99999, 60000, admin_user.id)


def test_non_worker_account_cannot_be_assigned(job, employer, assign):
    with pytest.raises(StaffingError, match="Ажилтан олдсонгүй"):
        assign(job, employer)


def test_inactive_worker_is_rejected(job, make_worker, assign):
    with pytest.raises(StaffingError, match="идэвхгүй"):
        assign(job, make_worker("99110001", is_active=False))


@pytest.mark.parametrize("availability", [Availability.UNAVAILABLE, Availability.TEMPORARILY_UNAVAILABLE])
def test_unavailable_worker_is_rejected(job, make_worker, assign, availability):
    with pytest.raises(StaffingError, match="боломжгүй гэж тэмдэглэсэн"):
        assign(job, make_worker("99110001", availability=availability))


def test_worker_without_skills_is_rejected(job, make_worker, assign):
    with pytest.raises(StaffingError, match="профайл бүрэн бус"):
        assign(job, make_worker("99110001", skills=()))


def test_worker_without_profile_is_rejected(job, make_user, assign):
    with pytest.raises(StaffingError, match="профайл бүрэн бус"):
        assign(job, make_user(phone="99110001", role=Role.WORKER))


def test_worker_with_only_retired_skill_is_rejected(job, make_worker, skills, assign):
    worker = make_worker("99110001", skills=("cleaning",))
    skills["cleaning"].is_active = False
    db.session.commit()
    with pytest.raises(StaffingError, match="профайл бүрэн бус"):
        assign(job, worker)


def test_duplicate_assignment_is_rejected(job, make_worker, assign):
    worker = make_worker("99110001")
    assign(job, worker)
    with pytest.raises(StaffingError, match="аль хэдийн томилогдсон"):
        assign(job, worker)
    assert active_count(job) == 1


def test_database_blocks_duplicate_active_assignment(job, make_worker, admin_user):
    worker = make_worker("99110001")
    for _ in range(2):
        db.session.add(Assignment(job=job, worker=worker, agreed_wage=60000, created_by=admin_user))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_worker_count_limit(job, make_worker, assign):
    assign(job, make_worker("99110001"))
    assign(job, make_worker("99110002"))
    with pytest.raises(StaffingError, match="орон тоо дүүрсэн"):
        assign(job, make_worker("99110003"))
    assert active_count(job) == 2


@pytest.mark.parametrize("wage", [0, 999, 5_000_001, -60000, 60000.0, "60000", None, True])
def test_invalid_wage_is_rejected(job, make_worker, admin_user, wage):
    with pytest.raises(StaffingError, match="Цалин"):
        assign_worker(job.id, make_worker("99110001").id, wage, admin_user.id)
    assert active_count(job) == 0


# --- overlap ---------------------------------------------------------------------------

@pytest.mark.parametrize("start, end", [
    (time(9), time(13)),     # identical
    (time(8), time(10)),     # starts before, ends inside
    (time(12), time(15)),    # starts inside, ends after
    (time(10), time(11)),    # inside
    (time(7), time(18)),     # covers
])
def test_overlapping_assignment_is_rejected(job, make_job, employer, make_worker, assign, start, end):
    worker = make_worker("99110001")
    assign(job, worker)  # 09:00-13:00
    other = make_job(employer, status=JobStatus.ACCEPTED, start_time=start, end_time=end)
    with pytest.raises(StaffingError, match="өөр ажилд томилогдсон"):
        assign(other, worker)


@pytest.mark.parametrize("start, end", [(time(13), time(17)), (time(6), time(9))])
def test_back_to_back_assignments_are_allowed(job, make_job, employer, make_worker, assign, start, end):
    worker = make_worker("99110001")
    assign(job, worker)  # 09:00-13:00
    other = make_job(employer, status=JobStatus.ACCEPTED, start_time=start, end_time=end)
    assign(other, worker)
    assert db.session.scalar(db.select(db.func.count(Assignment.id)).filter_by(worker_id=worker.id)) == 2


def test_same_time_on_another_day_is_allowed(job, make_job, employer, make_worker, assign):
    worker = make_worker("99110001")
    assign(job, worker)
    other = make_job(employer, status=JobStatus.ACCEPTED, work_date=tomorrow() + timedelta(days=1))
    assign(other, worker)


def test_cancelled_assignment_frees_the_worker(job, make_job, employer, make_worker, assign, admin_user):
    worker = make_worker("99110001")
    first = assign(job, worker)
    staffing.cancel_assignment(first, admin_user)
    other = make_job(employer, status=JobStatus.ACCEPTED)  # same time
    assign(other, worker)
    # ... and the cancelled one can be re-created later, as a new row.
    assign(job, make_worker("99110002"))
    assert active_count(job) == 1


def test_reassigning_after_cancellation_creates_new_row(job, make_worker, assign, admin_user):
    worker = make_worker("99110001")
    staffing.cancel_assignment(assign(job, worker), admin_user)
    assign(job, worker)
    rows = db.session.scalars(db.select(Assignment).filter_by(job_id=job.id, worker_id=worker.id)).all()
    assert sorted(r.status for r in rows) == ["active", "cancelled"]


def test_cancel_twice_is_rejected(job, make_worker, assign, admin_user):
    assignment = assign(job, make_worker("99110001"))
    staffing.cancel_assignment(assignment, admin_user)
    with pytest.raises(StaffingError):
        staffing.cancel_assignment(assignment, admin_user)


def test_non_admin_cannot_cancel(job, make_worker, assign, employer):
    assignment = assign(job, make_worker("99110001"))
    with pytest.raises(StaffingError):
        staffing.cancel_assignment(assignment, employer)
    assert assignment.status == AssignmentStatus.ACTIVE


def test_cancelling_job_cancels_its_assignments(job, make_worker, assign, admin_user):
    assignment = assign(job, make_worker("99110001"))
    job = db.session.get(Job, job.id)
    job.change_status(JobStatus.CANCELLED, changed_by=admin_user, note="Ажил олгогч цуцалсан")
    db.session.commit()
    db.session.expire_all()
    assert assignment.status == AssignmentStatus.CANCELLED
    assert assignment.cancelled_by_id == admin_user.id


# --- eligibility query --------------------------------------------------------------------

def test_eligible_workers_query(job, make_worker, assign, make_user):
    ready = make_worker("99110001", full_name="Бэлэн Ажилтан")
    make_worker("99110002", is_active=False)
    make_worker("99110003", availability=Availability.UNAVAILABLE)
    make_worker("99110004", skills=())
    make_user(phone="99110005", role=Role.WORKER)  # no profile
    busy = make_worker("99110006", full_name="Завгүй Ажилтан")
    assign(job, busy)

    everyone = db.session.scalars(eligible_workers_query()).all()
    assert {w.phone for w in everyone} == {ready.phone, busy.phone}
    free = db.session.scalars(eligible_workers_query(free_during=job)).all()
    assert [w.phone for w in free] == [ready.phone]


# --- applications -----------------------------------------------------------------------

def test_apply_creates_pending_application(job, make_worker):
    application = apply_to_job(job, make_worker("99110001"))
    assert application.status == ApplicationStatus.PENDING


def test_duplicate_application_is_rejected(job, make_worker):
    worker = make_worker("99110001")
    apply_to_job(job, worker)
    with pytest.raises(StaffingError, match="өргөдөл гаргасан"):
        apply_to_job(job, worker)
    assert db.session.scalar(db.select(db.func.count(JobApplication.id))) == 1


def test_database_blocks_duplicate_application(job, make_worker):
    worker = make_worker("99110001")
    db.session.add_all([JobApplication(job=job, worker=worker), JobApplication(job=job, worker=worker)])
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.IN_PROGRESS, JobStatus.CANCELLED])
def test_cannot_apply_to_job_that_is_not_open(make_job, employer, make_worker, status):
    with pytest.raises(StaffingError, match="өргөдөл авахаа больсон"):
        apply_to_job(make_job(employer, status=status), make_worker("99110001"))


def test_cannot_apply_to_full_job(make_job, employer, make_worker, assign):
    full = make_job(employer, status=JobStatus.ACCEPTED, workers_needed=1)
    assign(full, make_worker("99110001"))
    with pytest.raises(StaffingError, match="өргөдөл авахаа больсон"):
        apply_to_job(full, make_worker("99110002"))


def test_incomplete_or_unavailable_worker_cannot_apply(job, make_worker):
    with pytest.raises(StaffingError, match="ур чадвараа сонгоно"):
        apply_to_job(job, make_worker("99110001", skills=()))
    with pytest.raises(StaffingError, match="Ажил хийх боломжтой"):
        apply_to_job(job, make_worker("99110002", availability=Availability.TEMPORARILY_UNAVAILABLE))


def test_withdraw_and_reapply_reuses_row(job, make_worker):
    worker = make_worker("99110001")
    application = apply_to_job(job, worker)
    staffing.withdraw_application(application)
    assert application.status == ApplicationStatus.WITHDRAWN
    again = apply_to_job(job, worker)
    assert again.id == application.id
    assert again.status == ApplicationStatus.PENDING


@pytest.mark.parametrize("status", [ApplicationStatus.APPROVED, ApplicationStatus.REJECTED])
def test_reviewed_application_cannot_be_withdrawn_or_reopened(job, make_worker, admin_user, status):
    worker = make_worker("99110001")
    application = apply_to_job(job, worker)
    staffing.review_application(application, status, admin_user)
    with pytest.raises(StaffingError):
        staffing.withdraw_application(application)
    with pytest.raises(StaffingError):
        apply_to_job(job, worker)
    assert application.status == status


def test_only_admin_reviews_and_only_pending(job, make_worker, admin_user):
    worker = make_worker("99110001")
    application = apply_to_job(job, worker)
    with pytest.raises(StaffingError, match="Зөвхөн админ"):
        staffing.review_application(application, ApplicationStatus.APPROVED, worker)
    with pytest.raises(StaffingError):
        staffing.review_application(application, ApplicationStatus.WITHDRAWN, admin_user)
    staffing.review_application(application, ApplicationStatus.REJECTED, admin_user)
    with pytest.raises(StaffingError, match="аль хэдийн шийдвэрлэгдсэн"):
        staffing.review_application(application, ApplicationStatus.APPROVED, admin_user)


# --- concurrency ---------------------------------------------------------------------------

def test_concurrent_assignments_do_not_overbook(tmp_path, monkeypatch):
    """Two admins assign different workers to the last position at the same moment.

    The check is slowed down so both requests are inside assign_worker together.
    Without the lock both would see one free position and the job would be overbooked.
    """
    db_file = (tmp_path / "concurrent.sqlite3").as_posix()
    app = create_app("testing", {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}"})
    with app.app_context():
        db.create_all()
        seed_default_skills()
        from app.models import Skill, WorkerProfile
        cleaning = db.session.scalar(db.select(Skill).filter_by(code="cleaning"))
        admin = User(phone="99000001", full_name="Админ", role=Role.ADMIN, password_hash="x")
        employer = User(phone="99330001", full_name="Компани", role=Role.EMPLOYER, password_hash="x")
        workers = []
        for phone in ("99110001", "99110002"):
            worker = User(phone=phone, full_name="Ажилтан", role=Role.WORKER, password_hash="x")
            worker.worker_profile = WorkerProfile(skills=[cleaning])
            workers.append(worker)
        job = Job(employer=employer, title="Ажил", description="Тайлбар тайлбар", location="Хаяг",
                  work_date=tomorrow(), start_time=time(9), end_time=time(13),
                  workers_needed=1, wage_per_worker=60000, status=JobStatus.ACCEPTED)
        db.session.add_all([admin, employer, job, *workers])
        db.session.commit()
        ids = dict(job=job.id, admin=admin.id, workers=[w.id for w in workers])

    real_overlap_check = staffing.has_overlapping_assignment

    def slow_overlap_check(*args):
        result = real_overlap_check(*args)
        threading.Event().wait(0.3)  # hold the critical section open
        return result

    monkeypatch.setattr(staffing, "has_overlapping_assignment", slow_overlap_check)

    barrier = threading.Barrier(2)
    outcomes = []

    def attempt(worker_id):
        with app.app_context():
            barrier.wait()
            try:
                assign_worker(ids["job"], worker_id, 60000, ids["admin"])
                outcomes.append("ok")
            except StaffingError as error:
                outcomes.append(str(error))
            finally:
                db.session.remove()

    threads = [threading.Thread(target=attempt, args=(wid,)) for wid in ids["workers"]]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sorted(outcomes) == sorted(["ok", "Ажлын орон тоо дүүрсэн байна."])
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(Assignment.id))) == 1
        db.engine.dispose()
