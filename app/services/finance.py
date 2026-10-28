"""Money for completed jobs: snapshots, employer receipts, worker payouts, reversals.

Rules in one place:
  * wages and commission are counted only for assignments still active with
    attendance "present";
  * employer owes   total_due = sum(wages) + commission_per_worker x present workers;
  * workers are paid only after the employer has paid total_due in full;
  * nothing is ever edited or deleted - a mistake is corrected by a reversal entry;
  * balances are always computed from the ledger, never stored separately.

Concurrency: every money-changing function first issues a no-op UPDATE on the row it
protects. On SQLite the first write in a transaction locks the whole database file
until commit, so a second request waits and then re-reads the updated totals (this is
tested with two real threads). On PostgreSQL the same UPDATE locks just that row.
Unique constraints back this up: one snapshot per job, one reversal per entry, one
entry per form submission.
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    AssignmentStatus,
    AttendanceStatus,
    CommissionRate,
    Job,
    JobFinance,
    JobFinanceLine,
    JobStatus,
    LedgerEntry,
    LedgerKind,
    PaymentMethod,
    Role,
    User,
)
from app.services import ServiceError


class FinanceError(ServiceError):
    """A money rule was broken. The message is Mongolian and safe to show."""


class DuplicateSubmission(FinanceError):
    """The same form was submitted twice; the first one was already recorded."""


# --- derived statuses --------------------------------------------------------------------

class PaymentState:
    NOT_PAID = "not_paid"
    PARTIAL = "partial"
    PAID = "paid"
    LABELS = {NOT_PAID: "Төлөгдөөгүй", PARTIAL: "Хэсэгчлэн төлөгдсөн", PAID: "Бүрэн төлөгдсөн"}
    BADGES = {NOT_PAID: "danger", PARTIAL: "warning", PAID: "success"}

    @classmethod
    def of(cls, paid, total):
        if paid <= 0:
            return cls.NOT_PAID
        return cls.PAID if paid >= total else cls.PARTIAL


class FinanceState:
    AWAITING_SETUP = "awaiting_setup"
    NEEDS_REVIEW = "needs_review"
    AWAITING_EMPLOYER = "awaiting_employer"
    READY_FOR_PAYOUTS = "ready_for_payouts"
    SETTLED = "settled"
    LABELS = {
        AWAITING_SETUP: "Санхүүгийн тооцоо хүлээж буй",
        NEEDS_REVIEW: "Хянах шаардлагатай (ирсэн ажилтангүй)",
        AWAITING_EMPLOYER: "Ажил олгогчийн төлбөр хүлээж буй",
        READY_FOR_PAYOUTS: "Ажилтны цалин олгоход бэлэн",
        SETTLED: "Тооцоо дууссан",
    }
    BADGES = {
        AWAITING_SETUP: "secondary",
        NEEDS_REVIEW: "danger",
        AWAITING_EMPLOYER: "warning",
        READY_FOR_PAYOUTS: "info",
        SETTLED: "success",
    }


@dataclass
class LineSummary:
    line: JobFinanceLine
    paid: int
    remaining: int
    state: str

    @property
    def state_label(self):
        return PaymentState.LABELS[self.state]

    @property
    def state_badge(self):
        return PaymentState.BADGES[self.state]


@dataclass
class FinanceSummary:
    finance: JobFinance
    collected: int
    outstanding: int
    wages_paid: int
    wages_outstanding: int
    employer_state: str
    state: str
    lines: list = field(default_factory=list)

    @property
    def fully_collected(self):
        return self.outstanding == 0

    @property
    def employer_state_label(self):
        return PaymentState.LABELS[self.employer_state]

    @property
    def employer_state_badge(self):
        return PaymentState.BADGES[self.employer_state]

    @property
    def state_label(self):
        return FinanceState.LABELS[self.state]

    @property
    def state_badge(self):
        return FinanceState.BADGES[self.state]

    def line_summary(self, line_id):
        return next((s for s in self.lines if s.line.id == line_id), None)


def summarize(finance):
    """Every balance for one snapshot, computed from its ledger entries."""
    rows = db.session.execute(
        select(LedgerEntry.kind, LedgerEntry.line_id, func.sum(LedgerEntry.amount))
        .where(LedgerEntry.finance_id == finance.id)
        .group_by(LedgerEntry.kind, LedgerEntry.line_id)
    ).all()
    collected = 0
    paid_by_line = {}
    for kind, line_id, total in rows:
        if kind == LedgerKind.RECEIPT:
            collected += total
        elif kind == LedgerKind.RECEIPT_REVERSAL:
            collected -= total
        elif kind == LedgerKind.PAYOUT:
            paid_by_line[line_id] = paid_by_line.get(line_id, 0) + total
        elif kind == LedgerKind.PAYOUT_REVERSAL:
            paid_by_line[line_id] = paid_by_line.get(line_id, 0) - total

    lines = []
    for line in finance.lines:
        paid = paid_by_line.get(line.id, 0)
        lines.append(LineSummary(line, paid, line.wage - paid, PaymentState.of(paid, line.wage)))
    wages_paid = sum(s.paid for s in lines)
    outstanding = finance.total_due - collected

    if outstanding > 0:
        state = FinanceState.AWAITING_EMPLOYER
    elif wages_paid < finance.total_wages:
        state = FinanceState.READY_FOR_PAYOUTS
    else:
        state = FinanceState.SETTLED

    return FinanceSummary(
        finance=finance,
        collected=collected,
        outstanding=outstanding,
        wages_paid=wages_paid,
        wages_outstanding=finance.total_wages - wages_paid,
        employer_state=PaymentState.of(collected, finance.total_due),
        state=state,
        lines=lines,
    )


def eligible_assignments(job):
    """Assignments that earn a wage (and commission): still active, attendance present."""
    return [
        a for a in job.assignments
        if a.status == AssignmentStatus.ACTIVE and a.attendance == AttendanceStatus.PRESENT
    ]


def finance_state(job):
    """Finance state of a completed job, or None if the job isn't completed."""
    if job.status != JobStatus.COMPLETED:
        return None
    if job.finance is None:
        return FinanceState.AWAITING_SETUP if eligible_assignments(job) else FinanceState.NEEDS_REVIEW
    return summarize(job.finance).state


# --- commission ------------------------------------------------------------------------------

def set_commission(amount, admin, note=""):
    """Record a new commission per present worker. Affects future snapshots only."""
    _require_admin(admin)
    if type(amount) is not int or not 0 <= amount <= CommissionRate.MAX_AMOUNT:
        raise FinanceError("Шимтгэл 0-1 000 000 ₮ бүхэл тоо байх ёстой.")
    rate = CommissionRate(amount_per_worker=amount, note=(note or "")[:300], created_by=admin)
    db.session.add(rate)
    db.session.commit()
    return rate


# --- snapshots -------------------------------------------------------------------------------

def build_snapshot(job, actor):
    """Create the snapshot inside the caller's transaction (no commit).

    Used by job completion (same transaction as the status change) and by
    create_snapshot for completed jobs that don't have one yet."""
    _require_admin(actor)
    if job.status != JobStatus.COMPLETED:
        raise FinanceError("Зөвхөн дууссан ажилд санхүүгийн тооцоо үүсгэнэ.")
    if db.session.scalar(select(JobFinance.id).filter_by(job_id=job.id)):
        raise FinanceError("Энэ ажилд санхүүгийн тооцоо аль хэдийн үүссэн байна.")
    rate = CommissionRate.current()
    if rate is None:
        raise FinanceError("Шимтгэлийн хэмжээ тохируулаагүй байна. Эхлээд санхүүгийн тохиргоонд шимтгэл оруулна уу.")
    eligible = eligible_assignments(job)
    if not eligible:
        raise FinanceError(
            "Ирсэн ажилтан алга тул төлөх дүн үүсэхгүй. Энэ ажлыг гараар хянана уу — "
            "ажил олгогчоос төлбөр авах, ажилтанд цалин олгох зүйлгүй."
        )

    commission = rate.amount_per_worker
    lines = [
        JobFinanceLine(assignment=a, worker_id=a.worker_id, wage=a.agreed_wage, commission=commission)
        for a in eligible
    ]
    total_wages = sum(line.wage for line in lines)
    total_commission = commission * len(lines)
    finance = JobFinance(
        job=job,
        commission_rate=rate,
        commission_per_worker=commission,
        total_wages=total_wages,
        total_commission=total_commission,
        total_due=total_wages + total_commission,
        calculation_version=JobFinance.CALCULATION_VERSION,
        created_by=actor,
        lines=lines,
    )
    db.session.add(finance)
    return finance


def create_snapshot(job_id, admin_id):
    """Create a snapshot for an already-completed job (for example one completed
    before a commission was configured). Commits."""
    try:
        _lock(Job, job_id)
        job = db.session.get(Job, job_id)
        admin = db.session.get(User, admin_id)
        if job is None:
            raise FinanceError("Ажил олдсонгүй.")
        finance = build_snapshot(job, admin)
        db.session.commit()
        return finance
    except FinanceError:
        db.session.rollback()
        raise
    except IntegrityError:
        db.session.rollback()
        raise FinanceError("Энэ ажилд санхүүгийн тооцоо аль хэдийн үүссэн байна.")
    except Exception:
        db.session.rollback()
        raise


# --- receipts, payouts, reversals --------------------------------------------------------------

def record_receipt(job_id, amount, method, occurred_at, admin_id, reference="", note="", request_token=None):
    """Money received from the employer for a completed job. Commits."""
    def post(finance, summary, admin):
        if summary.outstanding <= 0:
            raise FinanceError("Ажил олгогчийн төлбөр бүрэн төлөгдсөн байна.")
        _check_amount(amount)
        if amount > summary.outstanding:
            raise FinanceError(
                f"Дүн үлдэгдлээс их байна. Үлдэгдэл: {_mnt(summary.outstanding)}. "
                "Илүү төлөлт хүлээн авах боломжгүй."
            )
        return LedgerEntry(kind=LedgerKind.RECEIPT, finance=finance, amount=amount)

    return _post_entry(job_id, admin_id, request_token, method, occurred_at, reference, note, post)


def record_payout(job_id, line_id, amount, method, occurred_at, admin_id, reference="", note="", request_token=None):
    """Wage paid to one worker for their line on a completed job. Commits."""
    def post(finance, summary, admin):
        line = db.session.get(JobFinanceLine, line_id) if line_id is not None else None
        if line is None or line.finance_id != finance.id:
            raise FinanceError("Ажилтны мөр энэ ажилд хамаарахгүй.")
        assignment = line.assignment
        if assignment.status != AssignmentStatus.ACTIVE or assignment.attendance != AttendanceStatus.PRESENT:
            raise FinanceError("Энэ томилолтод цалин олгох боломжгүй.")
        if not summary.fully_collected:
            raise FinanceError(
                f"Ажил олгогчийн төлбөр бүрэн төлөгдөөгүй байна (үлдэгдэл {_mnt(summary.outstanding)}). "
                "Ажилтны цалинг бүрэн төлбөр орсны дараа олгоно."
            )
        remaining = summary.line_summary(line.id).remaining
        if remaining <= 0:
            raise FinanceError("Энэ ажилтны цалин бүрэн олгогдсон байна.")
        _check_amount(amount)
        if amount > remaining:
            raise FinanceError(f"Дүн ажилтны үлдэгдлээс их байна. Үлдэгдэл: {_mnt(remaining)}.")
        return LedgerEntry(kind=LedgerKind.PAYOUT, finance=finance, line=line, amount=amount)

    return _post_entry(job_id, admin_id, request_token, method, occurred_at, reference, note, post)


def reverse_entry(job_id, entry_id, reason, admin_id, request_token=None):
    """Cancel a posted receipt or payout with a new, linked reversal entry. Commits."""
    def post(finance, summary, admin):
        original = db.session.get(LedgerEntry, entry_id) if entry_id is not None else None
        if original is None or original.finance_id != finance.id:
            raise FinanceError("Гүйлгээ энэ ажилд хамаарахгүй.")
        if original.is_reversal:
            raise FinanceError("Буцаалтыг дахин буцаах боломжгүй.")
        if original.reversed_by is not None:
            raise FinanceError("Энэ гүйлгээ аль хэдийн буцаагдсан байна.")
        clean_reason = (reason or "").strip()
        if not 5 <= len(clean_reason) <= LedgerEntry.NOTE_MAX_LENGTH:
            raise FinanceError("Буцаах шалтгааныг 5-300 тэмдэгтээр бичнэ үү.")
        if original.kind == LedgerKind.RECEIPT and summary.wages_paid > 0:
            raise FinanceError(
                "Ажилтанд цалин олгосон тул орлогыг буцаах боломжгүй. "
                "Эхлээд холбогдох олголтуудыг буцаана уу."
            )
        return LedgerEntry(
            kind=LedgerKind.REVERSAL_OF[original.kind], finance=finance, line=original.line,
            amount=original.amount, method=original.method, reverses=original, reason=clean_reason,
        )

    return _post_entry(job_id, admin_id, request_token, None, None, "", "", post)


def _post_entry(job_id, admin_id, request_token, method, occurred_at, reference, note, make_entry):
    """Shared steps for every ledger posting: lock, re-read, check, insert, commit."""
    try:
        finance_id = db.session.scalar(select(JobFinance.id).filter_by(job_id=job_id))
        if finance_id is None:
            raise FinanceError("Энэ ажилд санхүүгийн тооцоо үүсээгүй байна.")
        _lock(JobFinance, finance_id)
        # Checked after the lock, so a double submit that waited for the first one
        # gets "already recorded" instead of a confusing amount error.
        if request_token and db.session.scalar(select(LedgerEntry.id).filter_by(request_token=request_token)):
            raise DuplicateSubmission("Энэ гүйлгээ аль хэдийн бүртгэгдсэн байна.")

        admin = db.session.get(User, admin_id)
        _require_admin(admin)
        finance = db.session.get(JobFinance, finance_id)
        summary = summarize(finance)
        entry = make_entry(finance, summary, admin)

        if method is not None:
            if method not in PaymentMethod.ALL:
                raise FinanceError("Төлбөрийн хэлбэр буруу байна.")
            entry.method = method
        if occurred_at is not None:
            entry.occurred_at = _check_occurred_at(occurred_at)
        elif entry.occurred_at is None:
            entry.occurred_at = datetime.now(timezone.utc)
        reference = (reference or "").strip()
        note = (note or "").strip()
        if len(reference) > LedgerEntry.REFERENCE_MAX_LENGTH or len(note) > LedgerEntry.NOTE_MAX_LENGTH:
            raise FinanceError("Лавлах дугаар 100, тэмдэглэл 300 тэмдэгтээс ихгүй байна.")
        entry.reference = reference
        entry.note = note
        entry.public_id = uuid.uuid4().hex
        entry.created_by = admin
        entry.request_token = request_token or None
        db.session.add(entry)
        db.session.commit()
        return entry
    except FinanceError:
        db.session.rollback()
        raise
    except IntegrityError:
        db.session.rollback()
        if request_token and db.session.scalar(select(LedgerEntry.id).filter_by(request_token=request_token)):
            raise DuplicateSubmission("Энэ гүйлгээ аль хэдийн бүртгэгдсэн байна.")
        raise FinanceError("Гүйлгээ бүртгэгдсэнгүй (давхардсан эсвэл буруу өгөгдөл). Дахин шалгана уу.")
    except Exception:
        # Anything unexpected: undo everything and release the write lock at once.
        db.session.rollback()
        raise


# --- helpers -------------------------------------------------------------------------------------

def _require_admin(user):
    if user is None or not user.has_role(Role.ADMIN):
        raise FinanceError("Зөвхөн админ санхүүгийн бүртгэл хийнэ.")


def _check_amount(amount):
    if type(amount) is not int or amount <= 0:
        raise FinanceError("Дүн эерэг бүхэл төгрөг байх ёстой.")


def _check_occurred_at(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise FinanceError("Гүйлгээний огноо, цаг буруу байна.")
    now = datetime.now(timezone.utc)
    if value > now + timedelta(minutes=5):
        raise FinanceError("Ирээдүйн огноогоор гүйлгээ бүртгэх боломжгүй.")
    if value < now - timedelta(days=366):
        raise FinanceError("Нэг жилээс өмнөх огноогоор гүйлгээ бүртгэх боломжгүй.")
    return value


def _lock(model, row_id):
    """No-op write that takes the write lock before any check (see module docstring)."""
    table = model.__table__
    # Set a column to itself. Jobs use updated_at (setting it explicitly also stops its
    # automatic "onupdate" timestamp from firing); other tables use the primary key.
    column = table.c.updated_at if "updated_at" in table.c else table.c.id
    db.session.execute(
        update(model).where(table.c.id == row_id).values({column.name: column}),
        execution_options={"synchronize_session": False},
    )
    db.session.expire_all()


def _mnt(amount):
    from app.formatting import mnt
    return mnt(amount)


def new_request_token():
    """A one-time token to put in a money form (see LedgerEntry.request_token)."""
    return uuid.uuid4().hex
