"""Workers apply to published jobs; office staff accept or reject.

Accepting *is* the placement: in one locked transaction it rechecks the job (status,
date, paused, capacity) and the worker (active, available, no overlapping job) and
creates the assignment through services.staffing.place_worker. A pending application
never holds a position, so many people may apply for one place.

Every step is written to the application's history (ApplicationChange).
"""
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    ApplicationChange,
    ApplicationStatus,
    Assignment,
    AssignmentStatus,
    Availability,
    Job,
    JobApplication,
    ListingState,
    Role,
    User,
)
from app.services import notifications
from app.services.locking import lock_row
from app.services.staffing import (
    StaffingError,
    _lock_for_assignment,
    has_overlapping_assignment,
    place_worker,
    record_placement_cancelled,
)

REASON_MAX_LENGTH = 300


class ApplicationError(StaffingError):
    """An application rule was broken. The message is Mongolian and safe to show."""


class DuplicateApplication(ApplicationError):
    """The worker already has an application for this job (or a double tap sent it twice)."""


CLOSED_MESSAGES = {
    ListingState.FILLED: "Энэ ажлын орон тоо дүүрсэн байна.",
    ListingState.PAUSED: "Энэ зарыг оффис түр зогсоосон байна.",
    ListingState.EXPIRED: "Энэ ажлын хугацаа өнгөрсөн байна.",
    ListingState.CANCELLED: "Энэ ажил цуцлагдсан байна.",
}
NOT_OPEN = "Энэ ажилд хүсэлт авахаа больсон байна."


def worker_profile_problem(worker):
    """Why this worker can't apply yet (worker wording), or None."""
    profile = worker.worker_profile
    if profile is None or not any(skill.is_active for skill in profile.skills):
        return "Хүсэлт илгээхийн өмнө профайлдаа ур чадвараа сонгоно уу."
    if profile.availability != Availability.AVAILABLE:
        return "Хүсэлт илгээхийн тулд профайлдаа «Ажил хийх боломжтой» гэж тэмдэглэнэ үү."
    return None


def apply(job_id, worker_id):
    """The worker applies to an open, published job. Commits; returns the application."""
    try:
        lock_row(Job, job_id)
        job = db.session.get(Job, job_id)
        worker = db.session.get(User, worker_id)
        if worker is None or worker.role != Role.WORKER or not worker.is_active:
            raise ApplicationError("Зөвхөн ажилтны бүртгэлээр хүсэлт илгээнэ.")
        if job is None:
            raise ApplicationError("Ажил олдсонгүй.")
        if not job.accepting_applications:
            raise ApplicationError(CLOSED_MESSAGES.get(job.listing_state, NOT_OPEN))
        problem = worker_profile_problem(worker)
        if problem:
            raise ApplicationError(problem)
        if db.session.scalar(select(Assignment.id).filter_by(
            job_id=job.id, worker_id=worker.id, status=AssignmentStatus.ACTIVE,
        )):
            raise ApplicationError("Та энэ ажилд аль хэдийн томилогдсон байна.")
        if has_overlapping_assignment(worker.id, job):
            raise ApplicationError("Та энэ цагт өөр ажилд томилогдсон байна.")

        application = db.session.scalar(select(JobApplication).filter_by(job_id=job.id, worker_id=worker.id))
        note = ""
        if application is not None:
            if application.status == ApplicationStatus.PENDING:
                raise DuplicateApplication("Та энэ ажилд хүсэлт илгээсэн байна. Товчоо хянаж байна.")
            if application.status != ApplicationStatus.WITHDRAWN:
                raise ApplicationError("Энэ ажилд илгээсэн таны хүсэлт аль хэдийн шийдвэрлэгдсэн байна.")
            # Re-applying after withdrawing reuses the row (one row per job and worker).
            application.status = ApplicationStatus.PENDING
            application.reviewed_by = None
            application.reviewed_at = None
            note = "Дахин илгээсэн"
        else:
            application = JobApplication(job=job, worker=worker)
            db.session.add(application)
        application.record(ApplicationChange.APPLIED, worker, note=note)
        db.session.commit()
        return application
    except IntegrityError:
        # Two taps at once: the unique (job, worker) constraint caught the second.
        db.session.rollback()
        raise DuplicateApplication("Та энэ ажилд хүсэлт илгээсэн байна. Товчоо хянаж байна.")
    except Exception:
        db.session.rollback()
        raise


def _locked_application(application_id, lock_worker=False):
    """Lock the application's job (and worker), then return a fresh copy, or None."""
    application = db.session.get(JobApplication, application_id)
    if application is None:
        return None
    job_id, worker_id = application.job_id, application.worker_id
    if lock_worker:
        _lock_for_assignment(job_id, worker_id)
    else:
        lock_row(Job, job_id)
    return db.session.get(JobApplication, application_id)


def withdraw(application_id, worker_id):
    """The worker withdraws their own pending application. Commits."""
    try:
        application = _locked_application(application_id)
        if application is None or application.worker_id != worker_id:
            raise ApplicationError("Хүсэлт олдсонгүй.")
        if application.status != ApplicationStatus.PENDING:
            raise ApplicationError("Зөвхөн хянагдаж буй хүсэлтийг татан авах боломжтой.")
        application.status = ApplicationStatus.WITHDRAWN
        application.record(ApplicationChange.WITHDRAWN, application.worker)
        db.session.commit()
        return application
    except Exception:
        db.session.rollback()
        raise


def _check_admin(admin):
    if admin is None or not admin.has_role(Role.ADMIN):
        raise ApplicationError("Зөвхөн оффисын ажилтан хүсэлт шийдвэрлэнэ.")


def accept(application_id, admin_id):
    """Accept a pending application: creates the assignment (confirmed placement) at
    the job's wage, after rechecking everything under the lock. Commits."""
    try:
        application = _locked_application(application_id, lock_worker=True)
        admin = db.session.get(User, admin_id)
        _check_admin(admin)
        if application is None:
            raise ApplicationError("Хүсэлт олдсонгүй.")
        if application.status != ApplicationStatus.PENDING:
            # Also stops a double click, or two staff accepting the same person.
            raise ApplicationError(
                f"Энэ хүсэлт аль хэдийн шийдвэрлэгдсэн байна («{application.status_label}»).")
        job = application.job
        # The wage comes from the job, never from the form.
        assignment = place_worker(job, application.worker, job.wage_per_worker, admin)
        db.session.commit()
        return assignment
    except IntegrityError:
        db.session.rollback()
        raise ApplicationError("Энэ ажилтан энэ ажилд аль хэдийн томилогдсон байна.")
    except Exception:
        db.session.rollback()
        raise


def reject(application_id, admin_id, reason=""):
    """Reject a pending application. The optional reason is shown to the worker. Commits."""
    try:
        reason = (reason or "").strip()
        if len(reason) > REASON_MAX_LENGTH:
            raise ApplicationError("Шалтгаан 300 тэмдэгтээс ихгүй байна.")
        application = _locked_application(application_id)
        admin = db.session.get(User, admin_id)
        _check_admin(admin)
        if application is None:
            raise ApplicationError("Хүсэлт олдсонгүй.")
        if application.status != ApplicationStatus.PENDING:
            raise ApplicationError(
                f"Энэ хүсэлт аль хэдийн шийдвэрлэгдсэн байна («{application.status_label}»).")
        _reject(application, admin, reason)
        db.session.commit()
        return application
    except Exception:
        db.session.rollback()
        raise


def _reject(application, actor, reason):
    application.review(ApplicationStatus.REJECTED, actor)
    application.record(ApplicationChange.REJECTED, actor, note=reason)
    notifications.application_rejected(application, reason)


def close_for_cancelled_job(job, actor, cancelled_assignments):
    """When a job is cancelled: pending applications are rejected (and the workers
    told), and applications whose placement was cancelled get a history entry.
    Runs inside the caller's transaction."""
    for application in job.pending_applications:
        _reject(application, actor, "Ажил цуцлагдсан")
    for assignment in cancelled_assignments:
        record_placement_cancelled(assignment, actor, "Ажил цуцлагдсан")


def counts_by_status(job):
    counts = {status: 0 for status in ApplicationStatus.ALL}
    for application in job.applications:
        counts[application.status] += 1
    return counts
