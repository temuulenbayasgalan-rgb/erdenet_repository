"""Employer receipts, worker payouts and reversals (service level)."""
import threading
from datetime import datetime, time, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app import create_app
from app.extensions import db
from app.models import Job, LedgerEntry, LedgerKind, Role
from app.services import finance as finance_service
from app.services.finance import (
    DuplicateSubmission,
    FinanceError,
    FinanceState,
    PaymentState,
    record_payout,
    record_receipt,
    reverse_entry,
    summarize,
)

NOW = None  # filled per call: occurred_at must not be in the future


def now():
    return datetime.now(timezone.utc) - timedelta(minutes=1)


@pytest.fixture
def job(make_completed_job):
    """Two present workers (60 000 and 80 000), commission 10 000 each: employer owes 160 000."""
    return make_completed_job(workers=[("present", 60000), ("present", 80000)], commission=10000)


def summary(job):
    db.session.expire_all()
    return summarize(db.session.get(Job, job.id).finance)


def receive(job, admin, amount, **kwargs):
    return record_receipt(job.id, amount, "cash", now(), admin.id, **kwargs)


def pay(job, admin, line_index, amount, **kwargs):
    line = summary(job).lines[line_index].line
    return record_payout(job.id, line.id, amount, "bank_transfer", now(), admin.id, **kwargs)


# --- receipts --------------------------------------------------------------------------------

def test_single_full_receipt(job, admin_user):
    entry = receive(job, admin_user, 160000, reference="ХААН-123", note="Бэлнээр")
    s = summary(job)
    assert (s.collected, s.outstanding, s.employer_state, s.state) == \
        (160000, 0, PaymentState.PAID, FinanceState.READY_FOR_PAYOUTS)
    assert entry.created_by_id == admin_user.id
    assert entry.kind == LedgerKind.RECEIPT and entry.reference == "ХААН-123"
    assert len(entry.public_id) == 32


def test_partial_and_multiple_receipts(job, admin_user):
    receive(job, admin_user, 50000)
    s = summary(job)
    assert (s.collected, s.outstanding, s.employer_state) == (50000, 110000, PaymentState.PARTIAL)
    assert s.state == FinanceState.AWAITING_EMPLOYER
    receive(job, admin_user, 100000)
    receive(job, admin_user, 10000)
    s = summary(job)
    assert (s.collected, s.outstanding, s.employer_state) == (160000, 0, PaymentState.PAID)


def test_overpayment_is_rejected(job, admin_user):
    receive(job, admin_user, 150000)
    with pytest.raises(FinanceError, match="үлдэгдлээс их"):
        receive(job, admin_user, 10001)
    receive(job, admin_user, 10000)
    with pytest.raises(FinanceError, match="бүрэн төлөгдсөн"):
        receive(job, admin_user, 1)
    assert summary(job).collected == 160000


@pytest.mark.parametrize("amount", [0, -100, 1000.5, "1000", None, True])
def test_invalid_receipt_amount(job, admin_user, amount):
    with pytest.raises(FinanceError, match="эерэг бүхэл"):
        receive(job, admin_user, amount)
    assert summary(job).collected == 0


def test_receipt_for_job_without_snapshot(make_completed_job, make_job, finance_employer, admin_user):
    no_snapshot = make_completed_job(commission=None)
    with pytest.raises(FinanceError, match="санхүүгийн тооцоо үүсээгүй"):
        receive(no_snapshot, admin_user, 1000)
    pending = make_job(finance_employer)
    with pytest.raises(FinanceError, match="санхүүгийн тооцоо үүсээгүй"):
        receive(pending, admin_user, 1000)


@pytest.mark.parametrize("role", [Role.WORKER, Role.EMPLOYER])
def test_only_admin_records_money(job, make_user, role):
    user = make_user(phone="99440001", role=role)
    with pytest.raises(FinanceError, match="Зөвхөн админ"):
        record_receipt(job.id, 1000, "cash", now(), user.id)
    assert summary(job).collected == 0


def test_invalid_method_and_dates(job, admin_user):
    with pytest.raises(FinanceError, match="Төлбөрийн хэлбэр"):
        record_receipt(job.id, 1000, "crypto", now(), admin_user.id)
    with pytest.raises(FinanceError, match="Ирээдүйн"):
        record_receipt(job.id, 1000, "cash", datetime.now(timezone.utc) + timedelta(hours=2), admin_user.id)
    with pytest.raises(FinanceError, match="Нэг жилээс"):
        record_receipt(job.id, 1000, "cash", datetime.now(timezone.utc) - timedelta(days=400), admin_user.id)
    with pytest.raises(FinanceError, match="огноо"):
        record_receipt(job.id, 1000, "cash", datetime(2026, 1, 1), admin_user.id)  # no timezone
    assert summary(job).collected == 0


def test_duplicate_submission_records_once(job, admin_user):
    receive(job, admin_user, 50000, request_token="token-1")
    with pytest.raises(DuplicateSubmission):
        receive(job, admin_user, 50000, request_token="token-1")
    assert summary(job).collected == 50000
    assert db.session.scalar(db.select(db.func.count(LedgerEntry.id))) == 1


# --- payouts -------------------------------------------------------------------------------------

def test_payout_blocked_before_full_collection(job, admin_user):
    with pytest.raises(FinanceError, match="бүрэн төлөгдөөгүй"):
        pay(job, admin_user, 0, 1000)
    receive(job, admin_user, 159999)
    with pytest.raises(FinanceError, match="үлдэгдэл 1 ₮"):
        pay(job, admin_user, 0, 1000)
    assert summary(job).wages_paid == 0


def test_payout_after_full_collection(job, admin_user):
    receive(job, admin_user, 160000)
    entry = pay(job, admin_user, 0, 60000)
    s = summary(job)
    assert entry.kind == LedgerKind.PAYOUT and entry.line_id == s.lines[0].line.id
    assert (s.lines[0].paid, s.lines[0].remaining, s.lines[0].state) == (60000, 0, PaymentState.PAID)
    assert s.lines[1].state == PaymentState.NOT_PAID
    assert s.wages_outstanding == 80000
    assert s.state == FinanceState.READY_FOR_PAYOUTS


def test_partial_payouts_then_settled(job, admin_user):
    receive(job, admin_user, 160000)
    pay(job, admin_user, 1, 30000)
    s = summary(job)
    assert (s.lines[1].paid, s.lines[1].remaining, s.lines[1].state) == (30000, 50000, PaymentState.PARTIAL)
    pay(job, admin_user, 1, 50000)
    pay(job, admin_user, 0, 60000)
    s = summary(job)
    assert s.wages_outstanding == 0
    assert s.state == FinanceState.SETTLED


def test_excess_payout_is_rejected(job, admin_user):
    receive(job, admin_user, 160000)
    with pytest.raises(FinanceError, match="ажилтны үлдэгдлээс их"):
        pay(job, admin_user, 0, 60001)
    pay(job, admin_user, 0, 60000)
    with pytest.raises(FinanceError, match="бүрэн олгогдсон"):
        pay(job, admin_user, 0, 1)
    assert summary(job).lines[0].paid == 60000


def test_commission_is_never_paid_out(job, admin_user):
    """The office keeps the commission: workers can receive at most their wages."""
    receive(job, admin_user, 160000)
    pay(job, admin_user, 0, 60000)
    pay(job, admin_user, 1, 80000)
    s = summary(job)
    assert s.collected - s.wages_paid == s.finance.total_commission == 20000


def test_payout_line_must_belong_to_the_job(job, make_completed_job, admin_user):
    other = make_completed_job(workers=[("present", 50000)], commission=10000, title="Өөр ажил")
    receive(job, admin_user, 160000)
    other_line = summarize(other.finance).lines[0].line
    with pytest.raises(FinanceError, match="хамаарахгүй"):
        record_payout(job.id, other_line.id, 1000, "cash", now(), admin_user.id)
    with pytest.raises(FinanceError, match="хамаарахгүй"):
        record_payout(job.id, 99999, 1000, "cash", now(), admin_user.id)
    assert summary(job).wages_paid == 0


def test_absent_worker_has_no_line_to_pay(make_completed_job, admin_user):
    job = make_completed_job(workers=[("present", 60000), ("absent", 60000)])
    assert len(job.finance.lines) == 1  # nothing exists that a payout could be recorded against


# --- reversals --------------------------------------------------------------------------------------

def test_receipt_reversal_restores_balance_and_keeps_history(job, admin_user):
    original = receive(job, admin_user, 100000)
    reversal = reverse_entry(job.id, original.id, "Буруу дүн оруулсан", admin_user.id)
    s = summary(job)
    assert (s.collected, s.outstanding, s.employer_state) == (0, 160000, PaymentState.NOT_PAID)
    assert reversal.kind == LedgerKind.RECEIPT_REVERSAL
    assert reversal.reverses_id == original.id and reversal.amount == 100000
    assert reversal.reason == "Буруу дүн оруулсан" and reversal.created_by_id == admin_user.id
    db.session.expire_all()
    kept = db.session.get(LedgerEntry, original.id)
    assert kept.amount == 100000 and kept.reversed_by.id == reversal.id  # original untouched


def test_entry_can_be_reversed_only_once(job, admin_user):
    original = receive(job, admin_user, 100000)
    reversal = reverse_entry(job.id, original.id, "Алдаатай бүртгэл", admin_user.id)
    with pytest.raises(FinanceError, match="аль хэдийн буцаагдсан"):
        reverse_entry(job.id, original.id, "Дахин буцаах", admin_user.id)
    with pytest.raises(FinanceError, match="Буцаалтыг дахин"):
        reverse_entry(job.id, reversal.id, "Буцаалтыг буцаах", admin_user.id)
    assert summary(job).collected == 0


def test_database_blocks_second_reversal(job, admin_user):
    original = receive(job, admin_user, 100000)
    reverse_entry(job.id, original.id, "Алдаатай бүртгэл", admin_user.id)
    db.session.add(LedgerEntry(public_id="x" * 32, kind=LedgerKind.RECEIPT_REVERSAL, finance_id=original.finance_id,
                               amount=100000, method="cash", reverses_id=original.id, reason="давхар",
                               occurred_at=now(), created_by_id=admin_user.id))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


@pytest.mark.parametrize("reason", ["", "   ", "abc", "a" * 301])
def test_reversal_needs_meaningful_reason(job, admin_user, reason):
    original = receive(job, admin_user, 100000)
    with pytest.raises(FinanceError, match="шалтгаан"):
        reverse_entry(job.id, original.id, reason, admin_user.id)
    assert summary(job).collected == 100000


def test_receipt_reversal_blocked_while_payouts_depend_on_it(job, admin_user):
    receipt = receive(job, admin_user, 160000)
    payout = pay(job, admin_user, 0, 60000)
    with pytest.raises(FinanceError, match="Эхлээд холбогдох олголтуудыг буцаана"):
        reverse_entry(job.id, receipt.id, "Банкны гүйлгээ буцсан", admin_user.id)
    reverse_entry(job.id, payout.id, "Ажилтанд буруу олгосон", admin_user.id)
    reverse_entry(job.id, receipt.id, "Банкны гүйлгээ буцсан", admin_user.id)
    s = summary(job)
    assert (s.collected, s.wages_paid, s.state) == (0, 0, FinanceState.AWAITING_EMPLOYER)


def test_payout_reversal_restores_worker_balance(job, admin_user):
    receive(job, admin_user, 160000)
    payout = pay(job, admin_user, 0, 40000)
    reversal = reverse_entry(job.id, payout.id, "Өөр ажилтанд олгосон байсан", admin_user.id)
    s = summary(job)
    assert (s.lines[0].paid, s.lines[0].remaining) == (0, 60000)
    assert reversal.kind == LedgerKind.PAYOUT_REVERSAL and reversal.line_id == payout.line_id
    pay(job, admin_user, 0, 60000)  # can be paid correctly afterwards
    assert summary(job).lines[0].state == PaymentState.PAID


def test_reversal_entry_must_belong_to_the_job(job, make_completed_job, admin_user):
    other = make_completed_job(workers=[("present", 50000)], commission=10000, title="Өөр ажил")
    foreign = record_receipt(other.id, 10000, "cash", now(), admin_user.id)
    with pytest.raises(FinanceError, match="хамаарахгүй"):
        reverse_entry(job.id, foreign.id, "Буруу ажлаас", admin_user.id)
    assert summarize(db.session.get(Job, other.id).finance).collected == 10000


def test_history_survives_reversals(job, admin_user):
    receipt = receive(job, admin_user, 160000)
    payout = pay(job, admin_user, 0, 60000)
    reverse_entry(job.id, payout.id, "Буруу олгосон", admin_user.id)
    kinds = [e.kind for e in db.session.scalars(db.select(LedgerEntry).order_by(LedgerEntry.id))]
    assert kinds == ["receipt", "payout", "payout_reversal"]
    assert receipt.id and payout.id


# --- a failed step leaves nothing behind --------------------------------------------------------------

def test_failed_posting_rolls_back(job, admin_user, monkeypatch):
    real_uuid4 = finance_service.uuid.uuid4

    def broken():
        raise RuntimeError("ID generator failed")

    monkeypatch.setattr(finance_service.uuid, "uuid4", broken)
    with pytest.raises(RuntimeError):
        receive(job, admin_user, 50000)
    monkeypatch.setattr(finance_service.uuid, "uuid4", real_uuid4)
    # The service rolled back itself (no manual rollback here) and released the lock.
    assert db.session.scalar(db.select(db.func.count(LedgerEntry.id))) == 0
    receive(job, admin_user, 50000)  # and the system still works
    assert summary(job).collected == 50000


# --- concurrency ----------------------------------------------------------------------------------------

def _two_threads(app, call_a, call_b):
    barrier = threading.Barrier(2)
    outcomes = []

    def run(call):
        with app.app_context():
            barrier.wait()
            try:
                call()
                outcomes.append("ok")
            except FinanceError as error:
                outcomes.append(type(error).__name__)
            finally:
                db.session.remove()

    threads = [threading.Thread(target=run, args=(c,)) for c in (call_a, call_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    return sorted(outcomes)


@pytest.fixture
def file_db_world(tmp_path, monkeypatch):
    """A completed, fully paid job in a real SQLite file, so two threads can race."""
    from app.models import (Assignment, AttendanceStatus, CommissionRate, JobFinance, JobFinanceLine,
                            JobStatus, User)
    from app.validation import office_today

    db_file = (tmp_path / "finance.sqlite3").as_posix()
    app = create_app("testing", {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}"})
    with app.app_context():
        db.create_all()
        admin = User(phone="99000001", full_name="Админ", role=Role.ADMIN, password_hash="x")
        employer = User(phone="99330001", full_name="Компани", role=Role.EMPLOYER, password_hash="x")
        worker = User(phone="99110001", full_name="Ажилтан", role=Role.WORKER, password_hash="x")
        job = Job(employer=employer, title="Ажил", description="Тайлбар тайлбар", location="Хаяг",
                  work_date=office_today(), start_time=time(9), end_time=time(13),
                  workers_needed=1, wage_per_worker=60000, status=JobStatus.COMPLETED)
        assignment = Assignment(job=job, worker=worker, agreed_wage=60000, created_by=admin,
                                attendance=AttendanceStatus.PRESENT)
        rate = CommissionRate(amount_per_worker=10000, created_by=admin)
        line = JobFinanceLine(assignment=assignment, worker=worker, wage=60000, commission=10000)
        finance = JobFinance(job=job, commission_rate=rate, commission_per_worker=10000, total_wages=60000,
                             total_commission=10000, total_due=70000, calculation_version=1,
                             created_by=admin, lines=[line])
        db.session.add_all([admin, employer, worker, job, assignment, rate, finance])
        db.session.commit()
        ids = dict(job=job.id, line=line.id, admin=admin.id)
        record_receipt(ids["job"], 70000, "cash", now(), ids["admin"])

    # Hold the critical section open so both requests are inside together.
    real_summarize = finance_service.summarize

    def slow_summarize(finance):
        result = real_summarize(finance)
        threading.Event().wait(0.3)
        return result

    monkeypatch.setattr(finance_service, "summarize", slow_summarize)
    yield app, ids
    with app.app_context():
        db.engine.dispose()


def test_concurrent_payouts_cannot_exceed_wage(file_db_world):
    """Two admins pay 40 000 each against a 60 000 wage at the same moment."""
    app, ids = file_db_world
    call = lambda: record_payout(ids["job"], ids["line"], 40000, "cash", now(), ids["admin"])  # noqa: E731
    assert _two_threads(app, call, call) == ["FinanceError", "ok"]
    with app.app_context():
        paid = db.session.scalar(db.select(db.func.sum(LedgerEntry.amount)).filter_by(kind=LedgerKind.PAYOUT))
        assert paid == 40000


def test_concurrent_double_submit_records_once(file_db_world):
    """The same form (same token) arriving twice at once."""
    app, ids = file_db_world
    call = lambda: record_payout(ids["job"], ids["line"], 10000, "cash", now(), ids["admin"],  # noqa: E731
                                 request_token="same-form")
    assert _two_threads(app, call, call) == ["DuplicateSubmission", "ok"]
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(LedgerEntry.id)).filter_by(kind=LedgerKind.PAYOUT)) == 1


def test_concurrent_reversals_reverse_once(file_db_world):
    app, ids = file_db_world
    with app.app_context():
        entry_id = db.session.scalar(db.select(LedgerEntry.id).filter_by(kind=LedgerKind.RECEIPT))
    call = lambda: reverse_entry(ids["job"], entry_id, "Давхар буцаалт шалгах", ids["admin"])  # noqa: E731
    assert _two_threads(app, call, call) == ["FinanceError", "ok"]
    with app.app_context():
        assert db.session.scalar(
            db.select(db.func.count(LedgerEntry.id)).filter_by(kind=LedgerKind.RECEIPT_REVERSAL)) == 1
