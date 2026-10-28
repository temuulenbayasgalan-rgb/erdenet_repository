"""Who may be assigned to which job. (Applying lives in services.applications.)

Every rule here is checked on the server. The pages only *show* what is allowed;
the functions below decide.
"""
from sqlalchemy import exists, select, update
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
    JobStatus,
    ListingState,
    Referral,
    ReferralStatus,
    Role,
    Skill,
    User,
    WorkerProfile,
    utcnow,
)
from app.services import ServiceError, notifications
from app.validation import MAX_WAGE, MIN_WAGE, WAGE_RANGE_MESSAGE


class StaffingError(ServiceError):
    """A staffing rule was broken. The message is Mongolian and safe to show to the user."""


# --- scheduling --------------------------------------------------------------------

def overlap_clause(worker_id, work_date, start_time, end_time):
    """SQL condition: the worker has an active assignment that overlaps this period.

    Two periods overlap when they are on the same date and each starts before the
    other ends. A job ending at 12:00 and another starting at 12:00 do not overlap.
    `worker_id` may be a number or a column (for use inside a larger query).
    """
    return exists().where(
        Assignment.worker_id == worker_id,
        Assignment.status == AssignmentStatus.ACTIVE,
        Assignment.job_id == Job.id,
        Job.work_date == work_date,
        Job.start_time < end_time,
        Job.end_time > start_time,
    )


def has_overlapping_assignment(worker_id, job):
    return db.session.scalar(
        select(overlap_clause(worker_id, job.work_date, job.start_time, job.end_time))
    )


# --- eligible workers ----------------------------------------------------------------

def eligible_workers_query(skill_id=None, free_during=None):
    """Workers who could be offered work: active account, at least one active skill,
    marked available, and (if `free_during` is a job) not busy during that job."""
    query = (
        select(User)
        .join(User.worker_profile)
        .where(
            User.role == Role.WORKER,
            User.is_active.is_(True),
            WorkerProfile.availability == Availability.AVAILABLE,
            WorkerProfile.skills.any(Skill.is_active.is_(True)),
        )
    )
    if skill_id is not None:
        query = query.where(WorkerProfile.skills.any(Skill.id == skill_id))
    if free_during is not None:
        job = free_during
        query = query.where(~overlap_clause(User.id, job.work_date, job.start_time, job.end_time))
    return query.order_by(User.full_name, User.id)


def is_publicly_listed(user):
    """Whether an employer may see this worker's public profile."""
    return user is not None and db.session.scalar(
        select(eligible_workers_query().where(User.id == user.id).exists())
    )


def _worker_problem(worker):
    """Why this account can't be assigned work right now (admin wording), or None."""
    if worker is None or worker.role != Role.WORKER:
        return "Ажилтан олдсонгүй."
    if not worker.is_active:
        return "Ажилтны бүртгэл идэвхгүй байна."
    profile = worker.worker_profile
    if profile is None or not any(skill.is_active for skill in profile.skills):
        return "Ажилтны профайл бүрэн бус байна (ур чадвар сонгоогүй)."
    if profile.availability != Availability.AVAILABLE:
        return "Ажилтан одоогоор ажил хийх боломжгүй гэж тэмдэглэсэн байна."
    return None


# --- assignments ------------------------------------------------------------------------

def _lock_for_assignment(job_id, worker_id):
    """Take write locks before checking anything.

    On SQLite the first write in a transaction locks the whole database until commit,
    so a second admin assigning at the same moment waits and then sees our result.
    On PostgreSQL these updates lock the job and worker rows. The updates set each
    column to its own value, so nothing actually changes.
    """
    options = {"synchronize_session": False}
    db.session.execute(
        update(Job).where(Job.id == job_id).values(updated_at=Job.updated_at),
        execution_options=options,
    )
    db.session.execute(
        update(User).where(User.id == worker_id).values(is_active=User.is_active),
        execution_options=options,
    )
    # Forget anything loaded before the lock, so the checks below read fresh rows.
    db.session.expire_all()


def place_worker(job, worker, agreed_wage, admin):
    """Create the active assignment after checking every rule. The caller must have
    taken the locks (_lock_for_assignment) and commits or rolls back.

    Used by the office's direct assignment and by accepting an application, so both
    follow exactly the same capacity, availability and overlap rules."""
    if admin is None or not admin.has_role(Role.ADMIN):
        raise StaffingError("Зөвхөн админ ажилтан томилно.")
    if job is None:
        raise StaffingError("Ажил олдсонгүй.")
    if not job.is_staffable:
        raise StaffingError("Зөвхөн зөвшөөрсөн, хугацаа нь өнгөрөөгүй хүсэлтэд ажилтан томилно.")
    if job.listing_state == ListingState.PAUSED:
        raise StaffingError("Зар түр зогсоосон байна. Томилохын өмнө зарыг дахин нийтэлнэ үү.")
    problem = _worker_problem(worker)
    if problem:
        raise StaffingError(problem)
    if db.session.scalar(select(Assignment.id).filter_by(
        job_id=job.id, worker_id=worker.id, status=AssignmentStatus.ACTIVE,
    )):
        raise StaffingError("Энэ ажилтан энэ ажилд аль хэдийн томилогдсон байна.")
    if has_overlapping_assignment(worker.id, job):
        raise StaffingError("Энэ ажилтан тухайн цагт өөр ажилд томилогдсон байна.")
    # If the office referred this worker to the job, the assignment takes over
    # that referral's position instead of using a second one.
    open_referral = db.session.scalar(select(Referral).filter_by(
        job_id=job.id, worker_id=worker.id, status=ReferralStatus.REFERRED,
    ))
    taken = positions_taken(job.id) - (1 if open_referral else 0)
    if taken >= job.workers_needed:
        raise StaffingError("Ажлын орон тоо дүүрсэн байна.")
    if type(agreed_wage) is not int or not MIN_WAGE <= agreed_wage <= MAX_WAGE:
        raise StaffingError(WAGE_RANGE_MESSAGE)

    assignment = Assignment(
        job=job, worker=worker, agreed_wage=agreed_wage,
        status=AssignmentStatus.ACTIVE, created_by=admin,
    )
    db.session.add(assignment)
    if open_referral is not None:
        open_referral.status = ReferralStatus.PLACED
        open_referral.assignment = assignment
    if job.listing is not None:
        job.listing.updated_at = utcnow()

    # A pending application from this worker is accepted by this placement.
    application = db.session.scalar(select(JobApplication).filter_by(
        job_id=job.id, worker_id=worker.id, status=ApplicationStatus.PENDING,
    ))
    if application is not None:
        application.review(ApplicationStatus.APPROVED, admin)
        application.record(ApplicationChange.ACCEPTED, admin, assignment=assignment)

    db.session.flush()  # gives the assignment its id for the notification key
    notifications.worker_assigned(assignment)
    return assignment


def assign_worker(job_id, worker_id, agreed_wage, admin_id):
    """The office assigns a worker directly. Commits, or raises StaffingError."""
    try:
        _lock_for_assignment(job_id, worker_id)
        assignment = place_worker(
            db.session.get(Job, job_id), db.session.get(User, worker_id), agreed_wage,
            db.session.get(User, admin_id),
        )
        db.session.commit()
        return assignment
    except StaffingError:
        db.session.rollback()
        raise
    except IntegrityError:
        db.session.rollback()
        raise StaffingError("Энэ ажилтан энэ ажилд аль хэдийн томилогдсон байна.")


CANCEL_REASON_MIN_LENGTH = 3


def cancel_assignment(assignment, admin, reason):
    """The office cancels a placement. A reason is required and kept with the
    assignment; a linked application gets a "placement cancelled" history entry.
    The position becomes free again. Commits."""
    try:
        reason = (reason or "").strip()
        if not admin.has_role(Role.ADMIN):
            raise StaffingError("Зөвхөн админ томилолт цуцална.")
        if not CANCEL_REASON_MIN_LENGTH <= len(reason) <= 300:
            raise StaffingError("Цуцлах шалтгааныг 3-300 тэмдэгтээр бичнэ үү.")
        assignment_id, admin_id = assignment.id, admin.id
        _lock_for_assignment(assignment.job_id, assignment.worker_id)
        assignment = db.session.get(Assignment, assignment_id)
        admin = db.session.get(User, admin_id)
        if assignment.status != AssignmentStatus.ACTIVE:
            raise StaffingError("Энэ томилолт аль хэдийн цуцлагдсан байна.")
        if assignment.job.status == JobStatus.COMPLETED:
            # Its attendance is part of the completed record (and later of payments).
            raise StaffingError("Дууссан ажлын томилолтыг цуцлах боломжгүй.")
        assignment.cancel(admin, reason)
        record_placement_cancelled(assignment, admin, reason)
        referral = assignment.referral
        if referral is not None and referral.status in ReferralStatus.ACTIVE:
            referral.close(admin, "Томилолт цуцлагдсан")
        if assignment.job.listing is not None:
            assignment.job.listing.updated_at = utcnow()
        notifications.worker_assignment_cancelled(assignment)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def record_placement_cancelled(assignment, actor, reason):
    """Add the history entry on the application this assignment came from, if any."""
    change = db.session.scalar(select(ApplicationChange).filter_by(
        assignment_id=assignment.id, action=ApplicationChange.ACCEPTED,
    ))
    if change is not None:
        change.application.record(ApplicationChange.PLACEMENT_CANCELLED, actor, note=reason, assignment=assignment)


def positions_taken(job_id):
    """Positions in use: active assignments plus referrals not yet turned into one.
    The one rule shared by assignments, referrals and the public board."""
    assignments = db.session.scalar(
        select(db.func.count(Assignment.id)).filter_by(job_id=job_id, status=AssignmentStatus.ACTIVE)
    )
    referrals = db.session.scalar(
        select(db.func.count(Referral.id)).filter_by(job_id=job_id, status=ReferralStatus.REFERRED)
    )
    return assignments + referrals
