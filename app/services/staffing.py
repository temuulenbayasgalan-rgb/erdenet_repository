"""Who may apply to, and be assigned to, which job.

Every rule here is checked on the server. The pages only *show* what is allowed;
the functions below decide.
"""
from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError

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
    Skill,
    User,
    WorkerProfile,
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


# --- applications ----------------------------------------------------------------------

def apply_to_job(job, worker):
    """The worker applies to an open job. Returns the application."""
    if not job.accepting_applications:
        raise StaffingError("Энэ ажилд өргөдөл авахаа больсон байна.")
    profile = worker.worker_profile
    if profile is None or not any(skill.is_active for skill in profile.skills):
        raise StaffingError("Өргөдөл гаргахын өмнө профайлдаа ур чадвараа сонгоно уу.")
    if profile.availability != Availability.AVAILABLE:
        raise StaffingError("Өргөдөл гаргахын тулд профайлдаа «Ажил хийх боломжтой» гэж тэмдэглэнэ үү.")

    if db.session.scalar(select(Assignment.id).filter_by(
        job_id=job.id, worker_id=worker.id, status=AssignmentStatus.ACTIVE,
    )):
        raise StaffingError("Та энэ ажилд аль хэдийн томилогдсон байна.")

    application = db.session.scalar(select(JobApplication).filter_by(job_id=job.id, worker_id=worker.id))
    if application is not None:
        if application.status != ApplicationStatus.WITHDRAWN:
            raise StaffingError("Та энэ ажилд өргөдөл гаргасан байна.")
        # Re-applying after withdrawing reuses the same row (one row per job and worker).
        application.status = ApplicationStatus.PENDING
        application.reviewed_by = None
        application.reviewed_at = None
    else:
        application = JobApplication(job=job, worker=worker)
        db.session.add(application)

    try:
        db.session.commit()
    except IntegrityError:
        # A double tap sent the same application twice; the unique constraint caught it.
        db.session.rollback()
        raise StaffingError("Та энэ ажилд өргөдөл гаргасан байна.")
    return application


def withdraw_application(application):
    if application.status != ApplicationStatus.PENDING:
        raise StaffingError("Зөвхөн хянагдаж буй өргөдлийг татан авах боломжтой.")
    application.status = ApplicationStatus.WITHDRAWN
    db.session.commit()


def review_application(application, decision, admin):
    if not admin.has_role(Role.ADMIN):
        raise StaffingError("Зөвхөн админ өргөдөл шийдвэрлэнэ.")
    if application.status != ApplicationStatus.PENDING:
        raise StaffingError("Энэ өргөдөл аль хэдийн шийдвэрлэгдсэн байна.")
    try:
        application.review(decision, admin)
    except ValueError:
        raise StaffingError("Шийдвэр буруу байна.")
    db.session.commit()


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


def assign_worker(job_id, worker_id, agreed_wage, admin_id):
    """Create an active assignment after checking every rule. Commits, or raises StaffingError."""
    try:
        _lock_for_assignment(job_id, worker_id)
        admin = db.session.get(User, admin_id)
        job = db.session.get(Job, job_id)
        worker = db.session.get(User, worker_id)

        if admin is None or not admin.has_role(Role.ADMIN):
            raise StaffingError("Зөвхөн админ ажилтан томилно.")
        if job is None:
            raise StaffingError("Ажил олдсонгүй.")
        if not job.is_staffable:
            raise StaffingError("Зөвхөн зөвшөөрсөн, хугацаа нь өнгөрөөгүй хүсэлтэд ажилтан томилно.")
        problem = _worker_problem(worker)
        if problem:
            raise StaffingError(problem)
        if db.session.scalar(select(Assignment.id).filter_by(
            job_id=job.id, worker_id=worker.id, status=AssignmentStatus.ACTIVE,
        )):
            raise StaffingError("Энэ ажилтан энэ ажилд аль хэдийн томилогдсон байна.")
        if has_overlapping_assignment(worker.id, job):
            raise StaffingError("Энэ ажилтан тухайн цагт өөр ажилд томилогдсон байна.")
        active_count = db.session.scalar(
            select(db.func.count(Assignment.id)).filter_by(job_id=job.id, status=AssignmentStatus.ACTIVE)
        )
        if active_count >= job.workers_needed:
            raise StaffingError("Ажлын орон тоо дүүрсэн байна.")
        if type(agreed_wage) is not int or not MIN_WAGE <= agreed_wage <= MAX_WAGE:
            raise StaffingError(WAGE_RANGE_MESSAGE)

        assignment = Assignment(
            job=job, worker=worker, agreed_wage=agreed_wage,
            status=AssignmentStatus.ACTIVE, created_by=admin,
        )
        db.session.add(assignment)

        # Keep the worker's application consistent with the office's decision.
        application = db.session.scalar(select(JobApplication).filter_by(
            job_id=job.id, worker_id=worker.id, status=ApplicationStatus.PENDING,
        ))
        if application is not None:
            application.review(ApplicationStatus.APPROVED, admin)

        db.session.flush()  # gives the assignment its id for the notification key
        notifications.worker_assigned(assignment)
        db.session.commit()
        return assignment
    except StaffingError:
        db.session.rollback()
        raise
    except IntegrityError:
        db.session.rollback()
        raise StaffingError("Энэ ажилтан энэ ажилд аль хэдийн томилогдсон байна.")


def cancel_assignment(assignment, admin):
    if not admin.has_role(Role.ADMIN):
        raise StaffingError("Зөвхөн админ томилолт цуцална.")
    if assignment.status != AssignmentStatus.ACTIVE:
        raise StaffingError("Энэ томилолт аль хэдийн цуцлагдсан байна.")
    if assignment.job.status == JobStatus.COMPLETED:
        # Its attendance is part of the completed record (and later of payments).
        raise StaffingError("Дууссан ажлын томилолтыг цуцлах боломжгүй.")
    assignment.cancel(admin)
    notifications.worker_assignment_cancelled(assignment)
    db.session.commit()
