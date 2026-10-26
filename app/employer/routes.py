from datetime import timedelta

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.employer import bp
from app.employer.forms import (
    MAX_DAYS_AHEAD,
    CompanyNameForm,
    JobRequestForm,
    WorkerDirectoryFilterForm,
)
from app.extensions import db
from app.models import Job, JobStatus, JobStatusChange, Role, User
from app.security import role_required
from app.services import ServiceError
from app.services.jobs import cancel_job_by_employer
from app.services.staffing import eligible_workers_query, is_publicly_listed
from app.validation import office_today


def _own_job_or_404(job_id):
    """Look the job up together with its owner, so another employer's id gives 404."""
    job = db.session.scalar(db.select(Job).filter_by(id=job_id, employer_id=current_user.id))
    if job is None:
        abort(404)
    return job


@bp.route("/")
@role_required(Role.EMPLOYER)
def index():
    jobs = db.session.scalars(
        db.select(Job)
        .filter_by(employer_id=current_user.id)
        .order_by(Job.work_date.desc(), Job.start_time, Job.id.desc())
    ).all()
    return render_template("employer/index.html", jobs=jobs)


@bp.route("/jobs/new", methods=["GET", "POST"])
@role_required(Role.EMPLOYER)
def new_job():
    form = JobRequestForm()
    if form.validate_on_submit():
        # Only the fields declared on the form are read. Owner and status are set
        # here, so a crafted form can't choose them.
        job = Job(
            employer=current_user,
            title=form.title.data,
            description=form.description.data,
            location=form.location.data,
            work_date=form.work_date.data,
            start_time=form.start_time.data,
            end_time=form.end_time.data,
            workers_needed=form.workers_needed.data,
            wage_per_worker=form.wage_per_worker.data,
            notes=form.notes.data or "",
            status=JobStatus.PENDING,
        )
        job.status_changes.append(JobStatusChange(to_status=JobStatus.PENDING, changed_by=current_user))
        db.session.add(job)
        db.session.commit()
        flash("Хүсэлт амжилттай илгээгдлээ. Товчоо удахгүй холбогдоно.", "success")
        return redirect(url_for("employer.job_detail", job_id=job.id))

    if form.is_submitted():
        flash("Хүсэлт илгээгдсэнгүй. Улаанаар тэмдэглэсэн талбаруудыг засна уу.", "danger")
    today = office_today()
    return render_template(
        "employer/job_form.html",
        form=form,
        min_date=today.isoformat(),
        max_date=(today + timedelta(days=MAX_DAYS_AHEAD)).isoformat(),
    )


@bp.route("/jobs/<int:job_id>")
@role_required(Role.EMPLOYER)
def job_detail(job_id):
    return render_template("employer/job_detail.html", job=_own_job_or_404(job_id))


@bp.route("/jobs/<int:job_id>/cancel", methods=["POST"])
@role_required(Role.EMPLOYER)
def cancel_job(job_id):
    job = _own_job_or_404(job_id)
    try:
        cancel_job_by_employer(job, current_user)
    except ServiceError as error:
        flash(str(error), "danger")
    else:
        flash("Хүсэлт цуцлагдлаа.", "info")
    return redirect(url_for("employer.job_detail", job_id=job_id))


@bp.route("/profile", methods=["GET", "POST"])
@role_required(Role.EMPLOYER)
def profile():
    form = CompanyNameForm()
    if request.method == "GET":
        form.company_name.data = current_user.company_name
    if form.validate_on_submit():
        current_user.company_name = form.company_name.data or None
        db.session.commit()
        flash("Байгууллагын нэр хадгалагдлаа.", "success")
        return redirect(url_for("employer.index"))
    if form.is_submitted():
        flash("Хадгалагдсангүй. Алдааг засна уу.", "danger")
    return render_template("employer/profile.html", form=form)


# --- worker directory ------------------------------------------------------------------

@bp.route("/workers")
@role_required(Role.EMPLOYER)
def workers():
    my_upcoming_jobs = db.session.scalars(
        db.select(Job)
        .filter(
            Job.employer_id == current_user.id,
            Job.status.in_((JobStatus.PENDING, JobStatus.ACCEPTED)),
            Job.work_date >= office_today(),
        )
        .order_by(Job.work_date, Job.start_time)
    ).all()
    form = WorkerDirectoryFilterForm(my_upcoming_jobs, formdata=request.args)

    skill_id = None
    period_job = None
    if request.args and not form.validate():
        flash("Шүүлтүүрийн утга буруу байна. Бүх ажилтныг харуулж байна.", "warning")
    else:
        if form.skill.data:
            skill_id = int(form.skill.data)
        if form.job.data:
            period_job = next(job for job in my_upcoming_jobs if str(job.id) == form.job.data)

    listed = db.session.scalars(eligible_workers_query(skill_id=skill_id, free_during=period_job)).all()
    return render_template("employer/workers.html", workers=listed, form=form, period_job=period_job)


@bp.route("/workers/<int:user_id>")
@role_required(Role.EMPLOYER)
def worker_profile(user_id):
    """Public profile only. Workers who aren't listed in the directory look like they don't exist."""
    worker = db.session.get(User, user_id)
    if not is_publicly_listed(worker):
        abort(404)
    return render_template("employer/worker_profile.html", worker=worker)
