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
    JobFinanceLine,
    JobStatus,
    Role,
    Skill,
    WorkerProfile,
)
from app.admin.forms import DisputeForm
from app.security import is_safe_redirect, role_required
from app.services import ServiceError
from app.services import applications as application_service
from app.services import disputes as dispute_service
from app.services.finance import summarize
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
        # Back to the job the worker was applying for, if they came from one.
        next_url = request.args.get("next")
        return redirect(next_url if is_safe_redirect(next_url) else url_for("worker.index"))

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
    # The confirmed-placement details (exact address, supervisor, instructions) are
    # shown only when this worker has an active assignment on the job.
    can_report = assignment is not None and job.status in dispute_service.OPEN_JOB_STATUSES \
        and job.work_date <= office_today()
    return render_template(
        "worker/job_detail.html", job=job, application=application,
        assignment=assignment, can_apply=can_apply, can_report=can_report,
        profile_problem=application_service.worker_profile_problem(current_user),
        dispute_form=DisputeForm(formdata=None),
    )


@bp.route("/jobs/<int:job_id>/apply", methods=["POST"])
@role_required(Role.WORKER)
def apply(job_id):
    job, _application, _assignment = _visible_job_or_404(job_id)
    try:
        application_service.apply(job.id, current_user.id)
    except ServiceError as error:
        flash(str(error), "danger")
    else:
        flash("Хүсэлт илгээгдлээ. Оффис хянаад баталгаажуулбал мэдэгдэл очно.", "success")
    return redirect(url_for("worker.job_detail", job_id=job_id))


@bp.route("/assignments/<int:assignment_id>/dispute", methods=["POST"])
@role_required(Role.WORKER)
def report_problem(assignment_id):
    # Only the worker's own assignment; anyone else's looks like it doesn't exist.
    assignment = db.session.scalar(
        db.select(Assignment).filter_by(id=assignment_id, worker_id=current_user.id))
    if assignment is None:
        abort(404)
    form = DisputeForm()
    if not form.validate_on_submit():
        flash("Гомдол илгээгдсэнгүй: " + " ".join(form.message.errors or ["маягт буруу байна."]), "danger")
    else:
        try:
            dispute_service.raise_dispute(assignment.id, current_user.id, form.message.data)
        except ServiceError as error:
            flash(str(error), "danger")
        else:
            flash("Гомдол илгээгдлээ. Оффис хянаад шийдвэрлэнэ.", "success")
    return redirect(url_for("worker.job_detail", job_id=assignment.job_id))


@bp.route("/applications")
@role_required(Role.WORKER)
def applications():
    my_applications = db.session.scalars(
        db.select(JobApplication)
        .filter_by(worker_id=current_user.id)
        .options(selectinload(JobApplication.job), selectinload(JobApplication.changes))
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
        application_service.withdraw(application.id, current_user.id)
    except ServiceError as error:
        flash(str(error), "danger")
    else:
        flash("Хүсэлтээ татан авлаа.", "info")
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
    # Only this worker's own line: their wage, paid so far and remaining.
    pay = {}
    for line in db.session.scalars(db.select(JobFinanceLine).filter_by(worker_id=current_user.id)):
        pay[line.assignment_id] = summarize(line.finance).line_summary(line.id)
    return render_template("worker/assignments.html", active=active, cancelled=cancelled, pay=pay)
