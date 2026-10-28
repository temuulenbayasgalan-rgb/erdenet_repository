"""Admin pages for office-managed job listings and referrals.
Rules live in app/services/listings.py and app/services/referrals.py."""
from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.orm import selectinload

from app.admin import bp
from app.admin.listing_forms import (
    NEW_EMPLOYER,
    ListingForm,
    NewListingForm,
    PauseForm,
    PlaceReferralForm,
    ReasonForm,
    ReferralForm,
)
from app.extensions import db
from app.models import Job, JobListing, JobStatus, ListingState, Role, User
from app.security import role_required
from app.services import ServiceError, listings
from app.services import jobs as job_service
from app.services import referrals as referral_service


def _job_or_404(job_id):
    job = db.session.get(Job, job_id)
    if job is None:
        abort(404)
    return job


@bp.route("/listings")
@role_required(Role.ADMIN)
def listing_overview():
    """Every job the office manages, grouped by its public board state."""
    jobs = db.session.scalars(
        db.select(Job)
        .where(Job.status.in_((JobStatus.PENDING, JobStatus.ACCEPTED)))
        .options(selectinload(Job.listing), selectinload(Job.assignments), selectinload(Job.referrals))
        .order_by(Job.work_date, Job.start_time)
    ).all()
    order = [ListingState.PUBLISHED, ListingState.FILLED, ListingState.PAUSED,
             ListingState.UNPUBLISHED, ListingState.EXPIRED]
    groups = {state: [j for j in jobs if j.listing_state == state] for state in order}
    return render_template("admin/listings.html", groups=groups, ListingState=ListingState)


@bp.route("/listings/new", methods=["GET", "POST"])
@role_required(Role.ADMIN)
def new_listing():
    form = NewListingForm()
    status = 200
    if form.validate_on_submit():
        try:
            if form.employer.data == NEW_EMPLOYER:
                employer = listings.find_or_create_employer(
                    current_user, form.employer_phone.data, form.employer_name.data, form.employer_company.data,
                )
            else:
                employer = db.session.get(User, int(form.employer.data))
            job = listings.create_job(current_user, employer, form.listing_values(), publish=form.publish_now.data)
        except ServiceError as error:
            flash(f"Зар үүссэнгүй: {error}", "danger")
            status = 400
        else:
            flash("Зар үүслээ." + (" Нийтлэгдсэн." if form.publish_now.data else " Нийтлээгүй байна."), "success")
            return redirect(url_for("admin.job_detail", job_id=job.id))
    elif form.is_submitted():
        flash("Зар үүссэнгүй. Улаанаар тэмдэглэсэн талбаруудыг засна уу.", "danger")
        status = 400
    return render_template("admin/listing_form.html", form=form, job=None), status


@bp.route("/jobs/<int:job_id>/listing", methods=["GET", "POST"])
@role_required(Role.ADMIN)
def edit_listing(job_id):
    job = _job_or_404(job_id)
    form = ListingForm()
    if request.method == "GET":
        for field in ("title", "description", "location", "work_date", "start_time", "end_time",
                      "workers_needed", "wage_per_worker", "notes"):
            getattr(form, field).data = getattr(job, field)
        if job.listing is not None:
            form.public_area.data = job.listing.public_area
            form.latitude.data = job.listing.latitude
            form.longitude.data = job.listing.longitude
            form.skill.data = str(job.listing.skill_id) if job.listing.skill_id else ""
            form.internal_notes.data = job.listing.internal_notes
            form.supervisor_name.data = job.listing.supervisor_name
            form.supervisor_phone.data = job.listing.supervisor_phone
            form.worker_instructions.data = job.listing.worker_instructions
    status = 200
    if form.validate_on_submit():
        try:
            listings.update_job(job.id, current_user, form.listing_values())
        except ServiceError as error:
            flash(f"Хадгалагдсангүй: {error}", "danger")
            status = 400
        else:
            flash("Зар хадгалагдлаа.", "success")
            return redirect(url_for("admin.job_detail", job_id=job_id))
    elif form.is_submitted():
        flash("Хадгалагдсангүй. Улаанаар тэмдэглэсэн талбаруудыг засна уу.", "danger")
        status = 400
    return render_template("admin/listing_form.html", form=form, job=_job_or_404(job_id)), status


@bp.route("/jobs/<int:job_id>/listing/publish", methods=["POST"])
@role_required(Role.ADMIN)
def publish_listing(job_id):
    job = _job_or_404(job_id)
    try:
        listings.publish(job, current_user)
    except ServiceError as error:
        flash(str(error), "danger")
    else:
        flash("Зар нийтлэгдлээ. Нийтийн самбарт харагдана.", "success")
    return redirect(url_for("admin.job_detail", job_id=job_id))


@bp.route("/jobs/<int:job_id>/listing/pause", methods=["POST"])
@role_required(Role.ADMIN)
def pause_listing(job_id):
    job = _job_or_404(job_id)
    form = PauseForm()
    if not form.validate_on_submit():
        flash("Тайлбар 300 тэмдэгтээс ихгүй байна.", "danger")
    else:
        try:
            listings.pause(job, current_user, form.note.data)
        except ServiceError as error:
            flash(str(error), "danger")
        else:
            flash("Зар түр зогсоогдлоо. Нийтийн самбараас нуугдсан.", "info")
    return redirect(url_for("admin.job_detail", job_id=job_id))


@bp.route("/jobs/<int:job_id>/listing/cancel", methods=["POST"])
@role_required(Role.ADMIN)
def cancel_listing(job_id):
    """Cancel the job itself, with a required reason (assignments and referrals close too)."""
    job = _job_or_404(job_id)
    form = ReasonForm()
    if not form.validate_on_submit():
        flash("Цуцлах шалтгааныг 3-300 тэмдэгтээр бичнэ үү.", "danger")
    else:
        try:
            job_service.change_job_status(job, JobStatus.CANCELLED, current_user, note=form.reason.data)
        except ServiceError as error:
            flash(str(error), "danger")
        else:
            flash("Ажил, зар цуцлагдлаа.", "info")
    return redirect(url_for("admin.job_detail", job_id=job_id))


# --- referrals ---------------------------------------------------------------------------------

def _render_referrals(job, referral_form=None, code=200):
    return render_template(
        "admin/job_referrals.html", job=job,
        referral_form=referral_form or ReferralForm(formdata=None).fill_token(),
        cancel_form=ReasonForm(formdata=None),
        place_form=PlaceReferralForm(formdata=None, agreed_wage=job.wage_per_worker),
    ), code


@bp.route("/jobs/<int:job_id>/referrals")
@role_required(Role.ADMIN)
def job_referrals(job_id):
    return _render_referrals(_job_or_404(job_id))


@bp.route("/jobs/<int:job_id>/referrals", methods=["POST"])
@role_required(Role.ADMIN)
def create_referral(job_id):
    _job_or_404(job_id)
    form = ReferralForm()
    if not form.validate_on_submit():
        flash("Илгээлт бүртгэгдсэнгүй. Алдааг засна уу.", "danger")
        return _render_referrals(_job_or_404(job_id), form, 400)
    try:
        referral_service.create_referral(
            job_id, current_user.id, phone=form.phone.data, name=form.name.data, note=form.note.data,
            contact_shared=form.contact_shared.data, request_token=form.request_token.data,
        )
    except referral_service.DuplicateReferral as error:
        flash(str(error), "info")
    except ServiceError as error:
        flash(f"Илгээлт бүртгэгдсэнгүй: {error}", "danger")
        return _render_referrals(_job_or_404(job_id), form, 400)
    else:
        flash("Илгээлт бүртгэгдлээ.", "success")
    return redirect(url_for("admin.job_referrals", job_id=job_id))


@bp.route("/jobs/<int:job_id>/referrals/<int:referral_id>/contact-shared", methods=["POST"])
@role_required(Role.ADMIN)
def referral_contact_shared(job_id, referral_id):
    _job_or_404(job_id)
    try:
        referral_service.mark_contact_shared(job_id, referral_id, current_user)
    except ServiceError as error:
        flash(str(error), "danger")
    else:
        flash("Холбоо барих мэдээлэл өгсөн гэж бүртгэлээ.", "success")
    return redirect(url_for("admin.job_referrals", job_id=job_id))


@bp.route("/jobs/<int:job_id>/referrals/<int:referral_id>/cancel", methods=["POST"])
@role_required(Role.ADMIN)
def referral_cancel(job_id, referral_id):
    _job_or_404(job_id)
    form = ReasonForm()
    if not form.validate_on_submit():
        flash("Цуцлах шалтгааныг 3-300 тэмдэгтээр бичнэ үү.", "danger")
    else:
        try:
            referral_service.cancel_referral(job_id, referral_id, current_user, form.reason.data)
        except ServiceError as error:
            flash(str(error), "danger")
        else:
            flash("Илгээлт цуцлагдаж, орон тоо чөлөөлөгдлөө.", "info")
    return redirect(url_for("admin.job_referrals", job_id=job_id))


@bp.route("/jobs/<int:job_id>/referrals/<int:referral_id>/place", methods=["POST"])
@role_required(Role.ADMIN)
def referral_place(job_id, referral_id):
    _job_or_404(job_id)
    form = PlaceReferralForm()
    if not form.validate_on_submit():
        flash("Тохирсон цалинг бөглөнө үү.", "danger")
    else:
        try:
            referral_service.place_referral(job_id, referral_id, current_user, form.agreed_wage.data)
        except ServiceError as error:
            flash(f"Томилогдсонгүй: {error}", "danger")
        else:
            flash("Ажилтан томилогдлоо. Ирц, санхүүг томилолтоор бүртгэнэ.", "success")
    return redirect(url_for("admin.job_referrals", job_id=job_id))
