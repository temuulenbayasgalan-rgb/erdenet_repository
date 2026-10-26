from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.orm import selectinload

from app.admin import bp
from app.admin.forms import (
    ApplicationFilterForm,
    ApplicationReviewForm,
    AssignmentForm,
    AttendanceForm,
    JobFilterForm,
    JobStatusForm,
)
from app.extensions import db
from app.models import (
    ApplicationStatus,
    Assignment,
    AttendanceStatus,
    Job,
    JobApplication,
    JobStatus,
    Role,
    User,
)
from app.security import role_required
from app.services import ServiceError
from app.services.attendance import record_attendance
from app.services import jobs as job_service
from app.services.staffing import (
    StaffingError,
    assign_worker,
    cancel_assignment,
    eligible_workers_query,
    has_overlapping_assignment,
    review_application,
)


@bp.route("/")
@role_required(Role.ADMIN)
def index():
    pending_count = db.session.scalar(
        db.select(db.func.count(Job.id)).filter_by(status=JobStatus.PENDING)
    )
    return render_template("admin/index.html", pending_count=pending_count)


@bp.route("/workers")
@role_required(Role.ADMIN)
def workers():
    workers = db.session.scalars(
        db.select(User)
        .filter_by(role=Role.WORKER)
        .options(selectinload(User.worker_profile))
        .order_by(User.full_name)
    ).all()
    return render_template("admin/workers.html", workers=workers)


@bp.route("/workers/<int:user_id>")
@role_required(Role.ADMIN)
def worker_detail(user_id):
    worker = db.session.scalar(db.select(User).filter_by(id=user_id, role=Role.WORKER))
    if worker is None:
        abort(404)
    return render_template("admin/worker_detail.html", worker=worker)


@bp.route("/jobs")
@role_required(Role.ADMIN)
def jobs():
    form = JobFilterForm(formdata=request.args)
    query = db.select(Job).options(selectinload(Job.employer))

    if request.args and not form.validate():
        flash("Шүүлтүүрийн утга буруу байна. Бүх хүсэлтийг харуулж байна.", "warning")
    else:
        if form.status.data:
            query = query.filter_by(status=form.status.data)
        if form.work_date.data:
            query = query.filter_by(work_date=form.work_date.data)

    jobs = db.session.scalars(
        query.order_by(Job.work_date.desc(), Job.start_time, Job.id.desc())
    ).all()
    return render_template("admin/jobs.html", jobs=jobs, form=form)


def _job_or_404(job_id):
    job = db.session.get(Job, job_id)
    if job is None:
        abort(404)
    return job


def _render_job_detail(job, status_form=None, assignment_form=None, code=200):
    if assignment_form is None:
        assignment_form = AssignmentForm(formdata=None, agreed_wage=job.wage_per_worker)
    candidates = []
    if job.is_staffable and job.positions_remaining:
        candidates = db.session.scalars(eligible_workers_query(free_during=job)).all()
    approved_ids = {
        a.worker_id for a in job.applications if a.status == ApplicationStatus.APPROVED
    }
    # Applicants the office approved are listed first.
    candidates.sort(key=lambda worker: worker.id not in approved_ids)
    application_counts = {status: 0 for status in ApplicationStatus.ALL}
    for application in job.applications:
        application_counts[application.status] += 1
    return render_template(
        "admin/job_detail.html",
        job=job,
        form=status_form or JobStatusForm(job),
        assignment_form=assignment_form,
        candidates=candidates,
        approved_ids=approved_ids,
        application_counts=application_counts,
    ), code


@bp.route("/jobs/<int:job_id>")
@role_required(Role.ADMIN)
def job_detail(job_id):
    return _render_job_detail(_job_or_404(job_id))


@bp.route("/jobs/<int:job_id>/status", methods=["POST"])
@role_required(Role.ADMIN)
def change_job_status(job_id):
    job = _job_or_404(job_id)
    form = JobStatusForm(job)
    if form.validate_on_submit():
        try:
            job_service.change_job_status(job, form.new_status.data, current_user, note=form.note.data or "")
        except ServiceError as error:
            flash(f"Төлөв өөрчлөгдсөнгүй: {error}", "danger")
            return _render_job_detail(_job_or_404(job_id), code=400)
        flash(f"Төлөв «{job.status_label}» болж өөрчлөгдлөө.", "success")
        return redirect(url_for("admin.job_detail", job_id=job.id))

    flash("Төлөв өөрчлөгдсөнгүй.", "danger")
    return _render_job_detail(job, status_form=form, code=400)


# --- attendance ---------------------------------------------------------------------

@bp.route("/jobs/<int:job_id>/attendance")
@role_required(Role.ADMIN)
def job_attendance(job_id):
    job = _job_or_404(job_id)
    return render_template(
        "admin/job_attendance.html", job=job, recordable=AttendanceStatus.RECORDABLE,
        attendance_labels=AttendanceStatus.LABELS,
        completion_problem=job.completion_problem() if job.status == JobStatus.IN_PROGRESS else None,
    )


@bp.route("/assignments/<int:assignment_id>/attendance", methods=["POST"])
@role_required(Role.ADMIN)
def set_attendance(assignment_id):
    assignment = db.session.get(Assignment, assignment_id)
    if assignment is None:
        abort(404)
    job_id = assignment.job_id
    form = AttendanceForm()
    if not form.validate_on_submit():
        flash("Ирцийн төлөв буруу байна.", "danger")
    else:
        try:
            record_attendance(assignment, form.attendance.data, current_user)
        except ServiceError as error:
            flash(str(error), "danger")
        else:
            flash(f"{assignment.worker.full_name}: {assignment.attendance_label}", "success")
    return redirect(url_for("admin.job_attendance", job_id=job_id))


# --- applications --------------------------------------------------------------------

@bp.route("/jobs/<int:job_id>/applications")
@role_required(Role.ADMIN)
def job_applications(job_id):
    job = _job_or_404(job_id)
    form = ApplicationFilterForm(formdata=request.args)
    query = (
        db.select(JobApplication)
        .filter_by(job_id=job.id)
        .options(selectinload(JobApplication.worker).selectinload(User.worker_profile))
        .order_by(JobApplication.created_at, JobApplication.id)
    )
    if request.args and not form.validate():
        flash("Шүүлтүүрийн утга буруу байна. Бүх өргөдлийг харуулж байна.", "warning")
    elif form.status.data:
        query = query.filter_by(status=form.status.data)
    applications = db.session.scalars(query).all()

    assigned_ids = {a.worker_id for a in job.active_assignments}
    busy_ids = {
        a.worker_id for a in applications
        if a.worker_id not in assigned_ids and has_overlapping_assignment(a.worker_id, job)
    }
    return render_template(
        "admin/job_applications.html", job=job, form=form, applications=applications,
        assigned_ids=assigned_ids, busy_ids=busy_ids,
    )


@bp.route("/applications/<int:application_id>/review", methods=["POST"])
@role_required(Role.ADMIN)
def review(application_id):
    application = db.session.get(JobApplication, application_id)
    if application is None:
        abort(404)
    form = ApplicationReviewForm()
    if not form.validate_on_submit():
        flash("Шийдвэр буруу байна.", "danger")
    else:
        try:
            review_application(application, form.decision.data, current_user)
        except StaffingError as error:
            flash(str(error), "danger")
        else:
            flash(f"Өргөдөл «{application.status_label}» боллоо.", "success")
    return redirect(url_for("admin.job_applications", job_id=application.job_id))


# --- assignments ----------------------------------------------------------------------

@bp.route("/jobs/<int:job_id>/assignments", methods=["POST"])
@role_required(Role.ADMIN)
def create_assignment(job_id):
    job = _job_or_404(job_id)
    form = AssignmentForm()
    if not form.validate_on_submit():
        flash("Томилолт хийгдсэнгүй. Ажилтан болон цалингаа шалгана уу.", "danger")
        return _render_job_detail(job, assignment_form=form, code=400)
    try:
        assignment = assign_worker(job.id, form.worker_id.data, form.agreed_wage.data, current_user.id)
    except StaffingError as error:
        flash(f"Томилолт хийгдсэнгүй: {error}", "danger")
        return _render_job_detail(_job_or_404(job_id), assignment_form=form, code=400)

    flash(f"{assignment.worker.full_name} томилогдлоо.", "success")
    back_to = request.form.get("back_to")
    if back_to == "applications":
        return redirect(url_for("admin.job_applications", job_id=job_id))
    return redirect(url_for("admin.job_detail", job_id=job_id))


@bp.route("/assignments/<int:assignment_id>/cancel", methods=["POST"])
@role_required(Role.ADMIN)
def cancel_assignment_view(assignment_id):
    assignment = db.session.get(Assignment, assignment_id)
    if assignment is None:
        abort(404)
    try:
        cancel_assignment(assignment, current_user)
    except StaffingError as error:
        flash(str(error), "danger")
    else:
        flash("Томилолт цуцлагдлаа.", "info")
    return redirect(url_for("admin.job_detail", job_id=assignment.job_id))
