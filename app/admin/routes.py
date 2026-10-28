from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.orm import selectinload

from app.admin import bp
from app.admin.forms import (
    ApplicationInboxFilterForm,
    AssignmentForm,
    AttendanceForm,
    CancelPlacementForm,
    DisputeForm,
    JobFilterForm,
    JobStatusForm,
    RejectApplicationForm,
    ResolveDisputeForm,
)
from app.extensions import db
from app.models import (
    ApplicationStatus,
    Assignment,
    AttendanceDispute,
    AttendanceStatus,
    DisputeStatus,
    Job,
    JobApplication,
    JobStatus,
    Role,
    User,
)
from app.security import is_safe_redirect, role_required
from app.services import ServiceError
from app.services import applications as application_service
from app.services import disputes as dispute_service
from app.services.attendance import record_attendance
from app.services.finance import FinanceState, finance_state
from app.services import jobs as job_service
from app.services.staffing import (
    StaffingError,
    assign_worker,
    cancel_assignment,
    eligible_workers_query,
    has_overlapping_assignment,
)
from app.validation import office_today


@bp.route("/")
@role_required(Role.ADMIN)
def index():
    pending_count = db.session.scalar(
        db.select(db.func.count(Job.id)).filter_by(status=JobStatus.PENDING)
    )
    pending_applications = db.session.scalar(
        db.select(db.func.count(JobApplication.id)).filter_by(status=ApplicationStatus.PENDING)
    )
    open_disputes = db.session.scalar(
        db.select(db.func.count(AttendanceDispute.id)).filter_by(status=DisputeStatus.OPEN)
    )
    return render_template("admin/index.html", pending_count=pending_count,
                           pending_applications=pending_applications, open_disputes=open_disputes)


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
    pending_ids = {a.worker_id for a in job.pending_applications}
    # Workers who applied are listed first.
    candidates.sort(key=lambda worker: worker.id not in pending_ids)
    application_counts = application_service.counts_by_status(job)
    state = finance_state(job)
    return render_template(
        "admin/job_detail.html",
        finance_state_label=FinanceState.LABELS[state] if state else None,
        job=job,
        form=status_form or JobStatusForm(job),
        assignment_form=assignment_form,
        candidates=candidates,
        pending_ids=pending_ids,
        application_counts=application_counts,
        cancel_form=CancelPlacementForm(formdata=None),
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
        can_report=job.status in dispute_service.OPEN_JOB_STATUSES and job.work_date <= office_today(),
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


# --- applications ---------------------------------------------------------------------

def _back(default):
    """Return to the page the form came from (a path on this site only)."""
    target = request.form.get("back")
    return redirect(target if is_safe_redirect(target) else default)


@bp.route("/applications")
@role_required(Role.ADMIN)
def applications():
    """The office inbox: applications grouped by job, pending first by default."""
    jobs_with_applications = db.session.scalars(
        db.select(Job).where(Job.applications.any()).order_by(Job.work_date.desc(), Job.id.desc())
    ).all()
    form = ApplicationInboxFilterForm(jobs_with_applications, formdata=request.args or None)
    if request.args and not form.validate():
        flash("Шүүлтүүрийн утга буруу байна. Хянагдаж буй хүсэлтүүдийг харуулж байна.", "warning")
        form = ApplicationInboxFilterForm(jobs_with_applications, formdata=None)
    query = (
        db.select(JobApplication)
        .join(JobApplication.job)
        .options(
            selectinload(JobApplication.worker).selectinload(User.worker_profile),
            selectinload(JobApplication.changes),
        )
        .order_by(Job.work_date, Job.start_time, Job.id, JobApplication.created_at, JobApplication.id)
    )
    if form.job.data:
        query = query.where(JobApplication.job_id == int(form.job.data))
    if form.status.data:
        query = query.where(JobApplication.status == form.status.data)

    groups = {}
    for application in db.session.scalars(query):
        groups.setdefault(application.job, []).append(application)
    busy = {
        application.id
        for job, items in groups.items() for application in items
        if application.status == ApplicationStatus.PENDING
        and has_overlapping_assignment(application.worker_id, job)
    }
    open_disputes = db.session.scalar(
        db.select(db.func.count(AttendanceDispute.id)).filter_by(status=DisputeStatus.OPEN))
    return render_template(
        "admin/applications.html", form=form, groups=groups, busy=busy,
        reject_form=RejectApplicationForm(formdata=None), open_disputes=open_disputes,
        back=request.full_path.rstrip("?"),
    )


@bp.route("/jobs/<int:job_id>/applications")
@role_required(Role.ADMIN)
def job_applications(job_id):
    job = _job_or_404(job_id)
    return redirect(url_for("admin.applications", job=job.id, status=""))


@bp.route("/applications/<int:application_id>/accept", methods=["POST"])
@role_required(Role.ADMIN)
def accept_application(application_id):
    application = db.session.get(JobApplication, application_id)
    if application is None:
        abort(404)
    default = url_for("admin.applications", job=application.job_id)
    try:
        assignment = application_service.accept(application.id, current_user.id)
    except StaffingError as error:
        flash(f"Баталгаажсангүй: {error}", "danger")
    else:
        flash(f"{assignment.worker.full_name} баталгаажлаа (томилогдлоо). Ажилтанд мэдэгдэл очлоо.", "success")
    return _back(default)


@bp.route("/applications/<int:application_id>/reject", methods=["POST"])
@role_required(Role.ADMIN)
def reject_application(application_id):
    application = db.session.get(JobApplication, application_id)
    if application is None:
        abort(404)
    default = url_for("admin.applications", job=application.job_id)
    form = RejectApplicationForm()
    if not form.validate_on_submit():
        flash("Татгалзсангүй: " + " ".join(form.reason.errors or ["маягт буруу байна."]), "danger")
        return _back(default)
    try:
        application_service.reject(application.id, current_user.id, form.reason.data or "")
    except StaffingError as error:
        flash(f"Татгалзсангүй: {error}", "danger")
    else:
        flash("Хүсэлтийг татгалзлаа. Ажилтанд мэдэгдэл очлоо.", "info")
    return _back(default)


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
    job_id = assignment.job_id
    form = CancelPlacementForm()
    if not form.validate_on_submit():
        flash("Томилолт цуцлагдсангүй: " + " ".join(form.reason.errors or ["маягт буруу байна."]), "danger")
        return redirect(url_for("admin.job_detail", job_id=job_id))
    try:
        cancel_assignment(assignment, current_user, form.reason.data)
    except StaffingError as error:
        flash(str(error), "danger")
    else:
        flash("Томилолт цуцлагдлаа. Орон тоо чөлөөлөгдлөө.", "info")
    return redirect(url_for("admin.job_detail", job_id=job_id))


# --- disputes -------------------------------------------------------------------------

@bp.route("/assignments/<int:assignment_id>/disputes", methods=["POST"])
@role_required(Role.ADMIN)
def raise_dispute(assignment_id):
    """The office records a disagreement, e.g. a supervisor phoned about attendance."""
    assignment = db.session.get(Assignment, assignment_id)
    if assignment is None:
        abort(404)
    job_id = assignment.job_id
    form = DisputeForm()
    if not form.validate_on_submit():
        flash("Гомдол бүртгэгдсэнгүй: " + " ".join(form.message.errors or ["маягт буруу байна."]), "danger")
    else:
        try:
            dispute_service.raise_dispute(assignment.id, current_user.id, form.message.data)
        except ServiceError as error:
            flash(str(error), "danger")
        else:
            flash("Гомдол бүртгэгдлээ.", "success")
    return redirect(url_for("admin.job_attendance", job_id=job_id))


@bp.route("/disputes/<int:dispute_id>/resolve", methods=["POST"])
@role_required(Role.ADMIN)
def resolve_dispute(dispute_id):
    dispute = db.session.get(AttendanceDispute, dispute_id)
    if dispute is None:
        abort(404)
    job_id = dispute.assignment.job_id
    form = ResolveDisputeForm()
    if not form.validate_on_submit():
        flash("Шийдвэрлэгдсэнгүй: " + " ".join(form.resolution.errors or ["маягт буруу байна."]), "danger")
    else:
        try:
            dispute_service.resolve_dispute(dispute.id, current_user.id, form.resolution.data)
        except ServiceError as error:
            flash(str(error), "danger")
        else:
            flash("Гомдол шийдвэрлэгдлээ. Холбогдох хүмүүст мэдэгдэл очлоо.", "success")
    return redirect(url_for("admin.job_attendance", job_id=job_id))
