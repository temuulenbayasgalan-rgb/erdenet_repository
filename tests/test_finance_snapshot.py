"""Commission settings and the frozen financial snapshot of a completed job."""
import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    AttendanceStatus,
    CommissionRate,
    Job,
    JobFinance,
    JobFinanceLine,
    JobStatus,
    LedgerEntry,
    Notification,
    Role,
)
from app.services import ServiceError
from app.services import finance as finance_service
from app.services.finance import FinanceError, FinanceState, finance_state, summarize


# --- commission setting ---------------------------------------------------------------------

def test_commission_is_append_only_and_latest_is_current(admin_user):
    finance_service.set_commission(10000, admin_user, "Эхний")
    finance_service.set_commission(12000, admin_user)
    rates = db.session.scalars(db.select(CommissionRate).order_by(CommissionRate.id)).all()
    assert [r.amount_per_worker for r in rates] == [10000, 12000]
    assert CommissionRate.current().amount_per_worker == 12000
    assert rates[0].created_by_id == admin_user.id


def test_zero_commission_is_allowed(admin_user):
    finance_service.set_commission(0, admin_user)
    assert CommissionRate.current().amount_per_worker == 0


@pytest.mark.parametrize("amount", [-1, 1_000_001, 10000.0, "10000", None, True])
def test_invalid_commission_is_rejected(admin_user, amount):
    with pytest.raises(FinanceError, match="Шимтгэл"):
        finance_service.set_commission(amount, admin_user)
    assert CommissionRate.current() is None


@pytest.mark.parametrize("role", [Role.WORKER, Role.EMPLOYER])
def test_only_admin_sets_commission(make_user, role):
    with pytest.raises(FinanceError, match="Зөвхөн админ"):
        finance_service.set_commission(5000, make_user(phone="99440001", role=role))


def test_database_rejects_negative_commission(admin_user):
    db.session.add(CommissionRate(amount_per_worker=-5, created_by=admin_user))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


# --- calculations -------------------------------------------------------------------------------

def test_snapshot_totals_single_worker(make_completed_job, admin_user):
    job = make_completed_job(workers=[("present", 60000)], commission=10000)
    f = job.finance
    assert (f.total_wages, f.commission_per_worker, f.total_commission, f.total_due) == (60000, 10000, 10000, 70000)
    assert f.created_by_id == admin_user.id and f.created_at is not None
    assert f.calculation_version == JobFinance.CALCULATION_VERSION
    assert f.commission_rate_id == CommissionRate.current().id
    assert all(isinstance(v, int) for v in (f.total_wages, f.total_commission, f.total_due))


def test_snapshot_uses_each_agreed_wage(make_completed_job):
    job = make_completed_job(workers=[("present", 60000), ("present", 75000), ("present", 55000)], commission=8000)
    f = job.finance
    assert sorted(line.wage for line in f.lines) == [55000, 60000, 75000]
    assert all(line.commission == 8000 for line in f.lines)
    assert f.total_wages == 190000
    assert f.total_commission == 24000
    assert f.total_due == 214000


@pytest.mark.parametrize("excluded", ["absent", "excused", "cancelled"])
def test_only_present_workers_are_counted(make_completed_job, excluded):
    job = make_completed_job(workers=[("present", 60000), (excluded, 90000)], commission=10000)
    f = job.finance
    assert len(f.lines) == 1
    assert f.lines[0].wage == 60000
    assert (f.total_wages, f.total_commission, f.total_due) == (60000, 10000, 70000)


def test_lines_link_real_assignments(make_completed_job):
    job = make_completed_job(workers=[("present", 60000), ("absent", 60000)])
    [line] = job.finance.lines
    assert line.assignment.job_id == job.id
    assert line.assignment.attendance == AttendanceStatus.PRESENT
    assert line.worker_id == line.assignment.worker_id


def test_commission_change_does_not_touch_existing_snapshots(make_completed_job, admin_user):
    first = make_completed_job(commission=10000, title="Эхний ажил")
    finance_service.set_commission(25000, admin_user)
    second = make_completed_job(commission=None, title="Хоёр дахь ажил")
    db.session.expire_all()
    first = db.session.get(Job, first.id)
    assert first.finance.commission_per_worker == 10000
    assert first.finance.total_due == 70000
    assert second.finance.commission_per_worker == 25000
    assert second.finance.total_due == 85000


def test_completion_creates_no_money_movement(make_completed_job):
    job = make_completed_job()
    summary = summarize(job.finance)
    assert summary.collected == 0 and summary.wages_paid == 0
    assert db.session.scalar(db.select(db.func.count(LedgerEntry.id))) == 0
    assert finance_state(job) == FinanceState.AWAITING_EMPLOYER


def test_employer_completion_notification_states_amount_due(make_completed_job, finance_employer):
    make_completed_job(workers=[("present", 60000)], commission=10000)
    note = db.session.scalar(db.select(Notification).filter_by(recipient_id=finance_employer.id,
                                                               title="Ажил дууслаа"))
    assert "70 000 ₮" in note.message


# --- awaiting setup / no eligible workers -----------------------------------------------------------

def test_without_commission_job_awaits_setup(make_completed_job, admin_user):
    job = make_completed_job(commission=None)
    assert job.status == JobStatus.COMPLETED
    assert job.finance is None
    assert finance_state(job) == FinanceState.AWAITING_SETUP
    with pytest.raises(FinanceError, match="Шимтгэлийн хэмжээ тохируулаагүй"):
        finance_service.create_snapshot(job.id, admin_user.id)

    finance_service.set_commission(9000, admin_user)
    finance = finance_service.create_snapshot(job.id, admin_user.id)
    assert finance.total_due == 69000
    assert finance_state(db.session.get(Job, job.id)) == FinanceState.AWAITING_EMPLOYER


def test_legacy_job_without_present_workers_needs_review(make_job, finance_employer, admin_user):
    """A completed job from before the Milestone 5 completion rule, nobody present."""
    finance_service.set_commission(10000, admin_user)
    legacy = make_job(finance_employer, status=JobStatus.COMPLETED)
    assert finance_state(legacy) == FinanceState.NEEDS_REVIEW
    with pytest.raises(FinanceError, match="Ирсэн ажилтан алга"):
        finance_service.create_snapshot(legacy.id, admin_user.id)
    assert db.session.scalar(db.select(db.func.count(JobFinance.id))) == 0


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.ACCEPTED, JobStatus.IN_PROGRESS, JobStatus.CANCELLED])
def test_snapshot_only_for_completed_jobs(make_job, finance_employer, admin_user, status):
    finance_service.set_commission(10000, admin_user)
    job = make_job(finance_employer, status=status)
    with pytest.raises(FinanceError, match="Зөвхөн дууссан"):
        finance_service.create_snapshot(job.id, admin_user.id)
    assert finance_state(job) is None


# --- no duplicates, no recalculation -------------------------------------------------------------

def test_second_snapshot_is_refused(make_completed_job, admin_user):
    job = make_completed_job()
    with pytest.raises(FinanceError, match="аль хэдийн үүссэн"):
        finance_service.create_snapshot(job.id, admin_user.id)
    assert db.session.scalar(db.select(db.func.count(JobFinance.id))) == 1


def test_database_blocks_duplicate_snapshot(make_completed_job, admin_user):
    job = make_completed_job()
    db.session.add(JobFinance(job_id=job.id, commission_rate_id=CommissionRate.current().id,
                              commission_per_worker=0, total_wages=1, total_commission=0, total_due=1,
                              calculation_version=1, created_by_id=admin_user.id))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_database_rejects_inconsistent_totals(make_completed_job, admin_user):
    job = make_completed_job(commission=None)
    finance_service.set_commission(1000, admin_user)
    db.session.add(JobFinance(job_id=job.id, commission_rate_id=CommissionRate.current().id,
                              commission_per_worker=1000, total_wages=60000, total_commission=1000,
                              total_due=99999, calculation_version=1, created_by_id=admin_user.id))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_assignment_can_only_be_on_one_line(make_completed_job):
    job = make_completed_job()
    [line] = job.finance.lines
    db.session.add(JobFinanceLine(finance_id=line.finance_id, assignment_id=line.assignment_id,
                                  worker_id=line.worker_id, wage=1, commission=0))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_snapshot_failure_rolls_back_completion(make_job, make_worker, assign, admin_user, finance_employer, monkeypatch):
    """Completion and its snapshot are one transaction: if the snapshot fails,
    the job is not left completed without one."""
    from app.services.attendance import record_attendance
    from app.services.jobs import change_job_status
    from app.validation import office_today

    finance_service.set_commission(10000, admin_user)
    job = make_job(finance_employer, status=JobStatus.ACCEPTED, work_date=office_today())
    assignment = assign(job, make_worker("99110001"))
    change_job_status(db.session.get(Job, job.id), JobStatus.IN_PROGRESS, admin_user)
    record_attendance(assignment, "present", admin_user)

    def broken(job, actor):
        raise FinanceError("тест: тооцоо үүссэнгүй")

    monkeypatch.setattr(finance_service, "build_snapshot", broken)
    with pytest.raises(ServiceError):
        change_job_status(db.session.get(Job, job.id), JobStatus.COMPLETED, admin_user)
    db.session.expire_all()
    assert db.session.get(Job, job.id).status == JobStatus.IN_PROGRESS
    assert db.session.scalar(db.select(db.func.count(JobFinance.id))) == 0
    assert db.session.scalar(db.select(db.func.count(Notification.id)).filter_by(title="Ажил дууслаа")) == 0
