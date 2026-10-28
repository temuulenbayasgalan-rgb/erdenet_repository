"""Job status changes and the notifications they trigger.

The allowed moves and the completion rule live on the Job model (change_status,
completion_problem). This module adds who may make each move, and notifications.
"""
from app.extensions import db
from app.models import CommissionRate, JobStatus, Role, TransitionError
from app.services import ServiceError, applications, finance, notifications

ADMIN_ONLY_STATUSES = (JobStatus.ACCEPTED, JobStatus.IN_PROGRESS, JobStatus.COMPLETED)


def change_job_status(job, new_status, actor, note=""):
    """Office changes a job's status. Commits job, history, cascaded assignment
    cancellations and notifications in one transaction."""
    if not actor.has_role(Role.ADMIN):
        raise ServiceError("Зөвхөн админ ажлын төлөв өөрчилнө.")
    _apply(job, new_status, actor, note)


def cancel_job_by_employer(job, employer):
    """Employer cancels their own request while it is still pending."""
    if job.employer_id != employer.id:
        raise ServiceError("Энэ хүсэлтийг цуцлах боломжгүй.")
    if not job.employer_can_cancel:
        raise ServiceError("Энэ хүсэлтийг цуцлах боломжгүй. Товчоотой холбогдоно уу.")
    _apply(job, JobStatus.CANCELLED, employer, "Ажил олгогч цуцалсан")


def _apply(job, new_status, actor, note):
    if new_status in ADMIN_ONLY_STATUSES and not actor.has_role(Role.ADMIN):
        raise ServiceError("Зөвхөн админ ажлын төлөв өөрчилнө.")
    to_be_cancelled = list(job.active_assignments) if new_status == JobStatus.CANCELLED else []
    try:
        job.change_status(new_status, changed_by=actor, note=note)
    except TransitionError as error:
        db.session.rollback()
        raise ServiceError(str(error))

    if new_status == JobStatus.ACCEPTED:
        notifications.employer_job_accepted(job)
    elif new_status == JobStatus.COMPLETED:
        # Freeze the money in the same transaction, if the office has set a commission.
        # Without one the job is completed anyway and shows "awaiting financial setup".
        if CommissionRate.current() is not None:
            try:
                finance.build_snapshot(job, actor)
            except ServiceError:
                db.session.rollback()  # the completion is undone too
                raise
        notifications.employer_job_completed(job)
    elif new_status == JobStatus.CANCELLED:
        if actor.id != job.employer_id:  # the employer doesn't need telling about their own action
            notifications.employer_job_cancelled(job)
        for assignment in to_be_cancelled:
            notifications.worker_assignment_cancelled(assignment)
        applications.close_for_cancelled_job(job, actor, to_be_cancelled)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
