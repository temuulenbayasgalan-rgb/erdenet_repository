"""The public job board. No login needed; shows only public listing fields."""
from datetime import timedelta

from flask import abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.formatting import mn_date, mnt
from app.main import bp
from app.main.forms import BoardFilterForm
from app.models import Assignment, AssignmentStatus, JobApplication, Role
from app.security import role_required
from app.services import ServiceError
from app.services import applications as application_service
from app.services.applications import worker_profile_problem
from app.services.listings import job_by_code, public_card, public_jobs
from app.validation import office_phone_link, office_today


@bp.app_context_processor
def inject_office_phone():
    display, href = office_phone_link(current_app.config.get("OFFICE_PHONE"))
    return {
        "office_phone": display,
        "office_phone_href": href,
        "office_phone_missing_for_admin": display is None
        and current_user.is_authenticated and current_user.has_role(Role.ADMIN),
    }


def _apply_filters(cards, form):
    today = office_today()
    if form.q.data:
        needle = form.q.data.casefold()  # casefold works for Cyrillic, unlike SQLite LIKE
        cards = [c for c in cards if needle in f"{c['title']} {c['description']} {c['area']} {c['job_type'] or ''}".casefold()]
    if form.job_type.data:
        wanted = {choice.value: choice.label for choice in form.job_type.choices}[form.job_type.data]
        cards = [c for c in cards if c["job_type"] == wanted]
    if form.when.data == "today":
        cards = [c for c in cards if c["work_date"] == today]
    elif form.when.data == "tomorrow":
        cards = [c for c in cards if c["work_date"] == today + timedelta(days=1)]
    elif form.when.data == "week":
        cards = [c for c in cards if c["work_date"] <= today + timedelta(days=6)]
    if form.min_wage.data:
        cards = [c for c in cards if c["wage"] >= form.min_wage.data]
    return cards


@bp.route("/")
def index():
    form = BoardFilterForm(formdata=request.args)
    cards = [public_card(job) for job in public_jobs()]
    total = len(cards)
    if request.args and not form.validate():
        flash("Шүүлтүүрийн утга буруу байна. Бүх ажлыг харуулж байна.", "warning")
    else:
        cards = _apply_filters(cards, form)
    return render_template(
        "index.html", form=form, cards=cards, total=total,
        map_points=[_map_point(c) for c in cards if c["lat"] is not None],
        filtered=bool(request.args) and len(cards) != total,
    )


def _map_point(card):
    """What a map marker carries: a subset of the public card, nothing more."""
    return {
        "code": card["code"], "title": card["title"], "lat": card["lat"], "lng": card["lng"],
        "area": card["area"], "wage": mnt(card["wage"]), "date": mn_date(card["work_date"]),
        "time": f"{card['start_time']:%H:%M}–{card['end_time']:%H:%M}", "remaining": card["remaining"],
        "url": url_for("main.job", code=card["code"]),
    }


def _own_records(job):
    """The logged-in worker's application and active assignment for this job."""
    application = db.session.scalar(
        db.select(JobApplication).filter_by(job_id=job.id, worker_id=current_user.id))
    assignment = db.session.scalar(db.select(Assignment).filter_by(
        job_id=job.id, worker_id=current_user.id, status=AssignmentStatus.ACTIVE))
    return application, assignment


def _is_worker():
    return current_user.is_authenticated and current_user.has_role(Role.WORKER)


@bp.route("/jobs/<code>")
def job(code):
    if len(code) > 16:
        abort(404)
    found = job_by_code(code)
    if found is None:
        abort(404)
    application = assignment = None
    if _is_worker():
        application, assignment = _own_records(found)
    if not found.is_publicly_listed:
        # Filled, paused, past...: gone from the public board. A worker who applied
        # or was placed still has the job on their own (logged-in) page.
        if application is not None or assignment is not None:
            return redirect(url_for("worker.job_detail", job_id=found.id))
        abort(404)
    here = url_for("main.job", code=code)
    return render_template(
        "public_job.html", card=public_card(found), application=application, assignment=assignment,
        profile_problem=worker_profile_problem(current_user) if _is_worker() else None,
        login_url=url_for("auth.login", next=here), register_url=url_for("auth.register_worker", next=here),
    )


@bp.route("/jobs/<code>/apply", methods=["GET", "POST"])
@role_required(Role.WORKER)
def apply(code):
    """GET only exists for the login round trip (login returns here); it changes nothing."""
    if len(code) > 16:
        abort(404)
    found = job_by_code(code)
    if found is None:
        abort(404)
    if request.method == "GET":
        return redirect(url_for("main.job", code=code))
    try:
        application_service.apply(found.id, current_user.id)
    except ServiceError as error:
        flash(str(error), "danger")
        if found.is_publicly_listed:
            return redirect(url_for("main.job", code=code))
        return redirect(url_for("worker.jobs"))
    flash("Хүсэлт илгээгдлээ. Оффис хянаад баталгаажуулбал мэдэгдэл очно.", "success")
    return redirect(url_for("worker.job_detail", job_id=found.id))
