"""Disagreements about attendance or completed work.

The worker, the job's employer or office staff (for example on behalf of a supervisor
who phoned) can report one on an assignment. Only office staff resolve it, with a
written resolution. While any dispute on a job is open the job can't be completed
(Job.completion_problem), so no disputed attendance flows into the finance snapshot
unreviewed. Attendance itself is still corrected only through the office's attendance
page, which keeps its own history.
"""
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    Assignment,
    AssignmentStatus,
    AttendanceDispute,
    DisputeStatus,
    Job,
    JobStatus,
    Role,
    User,
    utcnow,
)
from app.services import ServiceError, notifications
from app.services.locking import lock_row
from app.validation import office_today

MIN_LENGTH = 5
OPEN_JOB_STATUSES = (JobStatus.ACCEPTED, JobStatus.IN_PROGRESS, JobStatus.COMPLETED)


class DisputeError(ServiceError):
    pass


class DuplicateDispute(DisputeError):
    pass


def _clean(text, what):
    text = (text or "").strip()
    if not MIN_LENGTH <= len(text) <= AttendanceDispute.TEXT_MAX_LENGTH:
        raise DisputeError(f"{what} {MIN_LENGTH}-{AttendanceDispute.TEXT_MAX_LENGTH} тэмдэгт байна.")
    return text


def can_raise(user, assignment):
    """Who may report a problem on this assignment."""
    if user.has_role(Role.ADMIN):
        return True
    if user.has_role(Role.WORKER):
        return assignment.worker_id == user.id
    if user.has_role(Role.EMPLOYER):
        return assignment.job.employer_id == user.id
    return False


def raise_dispute(assignment_id, user_id, message):
    """Record a disagreement. Commits; returns the dispute."""
    try:
        message = _clean(message, "Тайлбар")
        assignment = db.session.get(Assignment, assignment_id)
        if assignment is None:
            raise DisputeError("Томилолт олдсонгүй.")
        lock_row(Job, assignment.job_id)
        assignment = db.session.get(Assignment, assignment_id)
        user = db.session.get(User, user_id)
        if user is None or not can_raise(user, assignment):
            raise DisputeError("Томилолт олдсонгүй.")
        job = assignment.job
        if assignment.status != AssignmentStatus.ACTIVE:
            raise DisputeError("Цуцлагдсан томилолтод гомдол бүртгэх боломжгүй. Оффис руу залгана уу.")
        if job.status not in OPEN_JOB_STATUSES or job.work_date > office_today():
            raise DisputeError("Ирц, гүйцэтгэлийн гомдлыг ажлын өдрөөс эхлэн бүртгэнэ.")
        if db.session.scalar(select(AttendanceDispute.id).filter_by(
                assignment_id=assignment.id, raised_by_id=user.id, status=DisputeStatus.OPEN)):
            raise DuplicateDispute("Таны гомдол бүртгэгдсэн байна. Оффис хянаж байна.")
        dispute = AttendanceDispute(assignment=assignment, raised_by=user, raised_role=user.role, message=message)
        db.session.add(dispute)
        db.session.flush()
        notifications.dispute_opened(dispute)
        db.session.commit()
        return dispute
    except IntegrityError:
        db.session.rollback()
        raise DuplicateDispute("Таны гомдол бүртгэгдсэн байна. Оффис хянаж байна.")
    except Exception:
        db.session.rollback()
        raise


def resolve_dispute(dispute_id, admin_id, resolution):
    """Office staff close a dispute with a written resolution. Commits."""
    try:
        resolution = _clean(resolution, "Шийдвэрийн тайлбар")
        dispute = db.session.get(AttendanceDispute, dispute_id)
        if dispute is None:
            raise DisputeError("Гомдол олдсонгүй.")
        lock_row(Job, dispute.assignment.job_id)
        dispute = db.session.get(AttendanceDispute, dispute_id)
        admin = db.session.get(User, admin_id)
        if admin is None or not admin.has_role(Role.ADMIN):
            raise DisputeError("Зөвхөн оффисын ажилтан гомдол шийдвэрлэнэ.")
        if dispute.status != DisputeStatus.OPEN:
            raise DisputeError("Энэ гомдол аль хэдийн шийдвэрлэгдсэн байна.")
        dispute.status = DisputeStatus.RESOLVED
        dispute.resolution = resolution
        dispute.resolved_by = admin
        dispute.resolved_at = utcnow()
        notifications.dispute_resolved(dispute)
        db.session.commit()
        return dispute
    except Exception:
        db.session.rollback()
        raise
