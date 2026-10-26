from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models import (
    ApplicationStatus,
    Assignment,
    AssignmentStatus,
    Job,
    JobApplication,
    JobStatus,
    Role,
    Skill,
    WorkerProfile,
)
from app.security import role_required
from app.services.staffing import StaffingError, apply_to_job, withdraw_application
from app.validation import office_today
from app.worker import bp
from app.worker.forms import WorkerProfileForm


def _own_profile():
    """The logged-in worker's profile. There is no profile id in any URL,
    so a worker can never reach someone else's profile."""
    profile = current_user.worker_profile
    if profile is None:
        # Accounts created before profiles existed get one on first visit.
        profile = WorkerProfile(user=current_user)
        db.session.add(profile)
        db.session.commit()
    return profile


def _own_application(job_id):
    return db.session.scalar(
        db.select(JobApplication).filter_by(job_id=job_id, worker_id=current_user.id)
    )


def _own_active_assignment(job_id):
    return db.session.scalar(db.select(Assignment).filter_by(
        job_id=job_id, worker_id=current_user.id, status=AssignmentStatus.ACTIVE,
    ))


@bp.route("/")
@role_required(Role.WORKER)
def index():
    return render_template("worker/index.html", profile=_own_profile())


@bp.route("/profile/edit", methods=["GET", "POST"])
@role_required(Role.WORKER)
def edit_profile():
    profile = _own_profile()
    form = WorkerProfileForm()

    if request.method == "GET":
        form.skills.data = [skill.id for skill in profile.skills]
        form.availability.data = profile.availability
        form.description.data = profile.description

    if form.validate_on_submit():
        profile.skills = db.session.scalars(
            db.select(Skill).where(Skill.id.in_(form.skills.data), Skill.is_active.is_(True))
        ).all()
        profile.availability = form.availability.data
        profile.description = form.description.data or ""
        db.session.commit()
        flash("Профайл амжилттай хадгалагдлаа.", "success")
        return redirect(url_for("worker.index"))

    if form.is_submitted():
        flash("Профайл хадгалагдсангүй. Улаанаар тэмдэглэсэн талбаруудыг засна уу.", "danger")
    return render_template("worker/edit_profile.html", form=form)


# --- jobs and applications -----------------------------------------------------------

@bp.route("/jobs")
@role_required(Role.WORKER)
def jobs():
    candidates = db.session.scalars(
        db.select(Job)
        .where(Job.status == JobStatus.ACCEPTED, Job.work_date >= office_today())
        .options(selectinload(Job.assignments))
        .order_by(Job.work_date, Job.start_time, Job.id)
    ).all()
    open_jobs = [job for job in candidates if job.accepting_applications]
    my_applications = {
        application.job_id: application
        for application in db.session.scalars(
            db.select(JobApplication).filter_by(worker_id=current_user.id)
        )
    }
    return render_template(
        "worker/jobs.html", jobs=open_jobs, applications=my_applications, profile=_own_profile(),
    )


def _visible_job_or_404(job_id):
    """Workers see open jobs, plus jobs they applied to or were assigned to. Anything
    else (pending requests, other people's jobs) looks like it doesn't exist."""
    job = db.session.get(Job, job_id)
    if job is None:
        abort(404)
    application = _own_application(job_id)
    assignment = _own_active_assignment(job_id)
    if not (job.accepting_applications or application or assignment):
        abort(404)
    return job, application, assignment


@bp.route("/jobs/<int:job_id>")
@role_required(Role.WORKER)
def job_detail(job_id):
    job, application, assignment = _visible_job_or_404(job_id)
    can_apply = job.accepting_applications and assignment is None and (
        application is None or application.status == ApplicationStatus.WITHDRAWN
    )
    return render_template(
        "worker/job_detail.html", job=job, application=application,
        assignment=assignment, can_apply=can_apply,
    )


@bp.route("/jobs/<int:job_id>/apply", methods=["POST"])
@role_required(Role.WORKER)
def apply(job_id):
    job, _application, _assignment = _visible_job_or_404(job_id)
    try:
        apply_to_job(job, current_user)
    except StaffingError as error:
        flash(str(error), "danger")
    else:
        flash("Өргөдөл илгээгдлээ. Товчоо шийдвэрлээд мэдэгдэнэ.", "success")
    return redirect(url_for("worker.job_detail", job_id=job_id))


@bp.route("/applications")
@role_required(Role.WORKER)
def applications():
    my_applications = db.session.scalars(
        db.select(JobApplication)
        .filter_by(worker_id=current_user.id)
        .options(selectinload(JobApplication.job))
        .order_by(JobApplication.updated_at.desc(), JobApplication.id.desc())
    ).all()
    return render_template("worker/applications.html", applications=my_applications)


@bp.route("/applications/<int:application_id>/withdraw", methods=["POST"])
@role_required(Role.WORKER)
def withdraw(application_id):
    application = db.session.scalar(
        db.select(JobApplication).filter_by(id=application_id, worker_id=current_user.id)
    )
    if application is None:
        abort(404)
    try:
        withdraw_application(application)
    except StaffingError as error:
        flash(str(error), "danger")
    else:
        flash("Өргөдлөө татан авлаа.", "info")
    return redirect(url_for("worker.applications"))


@bp.route("/assignments")
@role_required(Role.WORKER)
def assignments():
    mine = db.session.scalars(
        db.select(Assignment)
        .join(Assignment.job)
        .filter(Assignment.worker_id == current_user.id)
        .options(selectinload(Assignment.job))
        .order_by(Job.work_date.desc(), Job.start_time)
    ).all()
    active = [a for a in mine if a.status == AssignmentStatus.ACTIVE]
    cancelled = [a for a in mine if a.status != AssignmentStatus.ACTIVE]
    return render_template("worker/assignments.html", active=active, cancelled=cancelled)
