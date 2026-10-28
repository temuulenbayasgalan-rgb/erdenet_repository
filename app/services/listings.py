"""Office-managed job listings: create, edit, publish, pause, and the public board query.

Only the fields listed in PUBLIC_FIELDS ever leave this module for public pages.
"""
import secrets
from datetime import timedelta

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models import (
    Job,
    JobListing,
    JobStatus,
    JobStatusChange,
    ListingChange,
    Role,
    Skill,
    User,
    utcnow,
)
from app.services import ServiceError, notifications
from app.services.locking import lock_row
from app.services.staffing import positions_taken
from app.validation import MAX_WAGE, MIN_WAGE, WAGE_RANGE_MESSAGE, is_valid_phone, normalize_phone, office_today

MAX_DAYS_AHEAD = 90
EDITABLE_STATUSES = (JobStatus.PENDING, JobStatus.ACCEPTED)
# Changing these after someone was sent to the job would change what they agreed to.
LOCKED_WHEN_STAFFED = ("work_date", "start_time", "end_time", "wage_per_worker")
FIELD_LABELS = {
    "title": "ажлын нэр", "description": "тайлбар", "location": "яг хаяг", "work_date": "огноо",
    "start_time": "эхлэх цаг", "end_time": "дуусах цаг", "workers_needed": "ажилтны тоо",
    "wage_per_worker": "цалин", "notes": "тэмдэглэл", "public_area": "нийтийн байршил",
    "latitude": "газрын зургийн цэг", "longitude": "газрын зургийн цэг", "skill_id": "ажлын төрөл",
    "internal_notes": "дотоод тэмдэглэл", "supervisor_name": "хянагчийн нэр",
    "supervisor_phone": "хянагчийн утас", "worker_instructions": "ажилтанд өгөх заавар",
}
JOB_FIELDS = ("title", "description", "location", "work_date", "start_time", "end_time",
              "workers_needed", "wage_per_worker", "notes")
LISTING_FIELDS = ("public_area", "latitude", "longitude", "skill_id", "internal_notes",
                  "supervisor_name", "supervisor_phone", "worker_instructions")
# Stored as "" rather than NULL when empty.
TEXT_FIELDS = ("internal_notes", "worker_instructions")
# A change to one of these is sent to applicants and placed workers. (Office-only
# fields, the map pin and the worker count are not worth a notification.)
NOTIFY_FIELDS = ("title", "description", "location", "work_date", "start_time", "end_time",
                 "wage_per_worker", "notes", "public_area", "supervisor_name", "supervisor_phone",
                 "worker_instructions")


class ListingError(ServiceError):
    pass


# --- validation shared by create and edit ----------------------------------------------------

def _check_admin(user):
    if user is None or not user.has_role(Role.ADMIN):
        raise ListingError("Зөвхөн оффисын админ зар удирдана.")


def check_coordinates(latitude, longitude):
    """Both empty, or both numbers inside the configured Erdenet/Orkhon area."""
    if latitude is None and longitude is None:
        return None, None
    if latitude is None or longitude is None:
        raise ListingError("Газрын зургийн цэгийн өргөрөг, уртрагийг хоёуланг нь оруулна уу.")
    if type(latitude) not in (int, float) or type(longitude) not in (int, float):
        raise ListingError("Газрын зургийн цэг буруу байна.")
    (south, west), (north, east) = current_app.config["MAP_BOUNDS"]
    if not (south <= latitude <= north and west <= longitude <= east):
        raise ListingError("Газрын зургийн цэг Эрдэнэт/Орхон аймгийн нутгаас гадна байна.")
    return float(latitude), float(longitude)


def _check_values(values, is_new):
    work_date = values["work_date"]
    today = office_today()
    if is_new or "work_date" in values.get("_changed", ()):
        if work_date < today:
            raise ListingError("Өнгөрсөн өдөр сонгох боломжгүй.")
        if work_date > today + timedelta(days=MAX_DAYS_AHEAD):
            raise ListingError(f"Ажлын өдөр {MAX_DAYS_AHEAD} хоногийн дотор байх ёстой.")
    if values["end_time"] <= values["start_time"]:
        raise ListingError("Дуусах цаг эхлэх цагаас хойш байх ёстой.")
    if type(values["workers_needed"]) is not int or not 1 <= values["workers_needed"] <= 50:
        raise ListingError("Ажилтны тоо 1-50 байх ёстой.")
    if type(values["wage_per_worker"]) is not int or not MIN_WAGE <= values["wage_per_worker"] <= MAX_WAGE:
        raise ListingError(WAGE_RANGE_MESSAGE)
    if not (values.get("public_area") or "").strip():
        raise ListingError("Нийтэд харагдах ойролцоо байршлыг бөглөнө үү.")
    values["latitude"], values["longitude"] = check_coordinates(values.get("latitude"), values.get("longitude"))
    for field in TEXT_FIELDS:
        values[field] = (values.get(field) or "").strip()
    values["supervisor_name"] = (values.get("supervisor_name") or "").strip() or None
    phone = normalize_phone((values.get("supervisor_phone") or "").strip()) or None
    if phone is not None and not is_valid_phone(phone):
        raise ListingError("Хянагчийн утасны дугаар 8 оронтой тоо байх ёстой.")
    values["supervisor_phone"] = phone
    if values["supervisor_name"] and len(values["supervisor_name"]) > JobListing.SUPERVISOR_NAME_MAX_LENGTH:
        raise ListingError("Хянагчийн нэр 100 тэмдэгтээс ихгүй байна.")
    if len(values["worker_instructions"]) > JobListing.INSTRUCTIONS_MAX_LENGTH:
        raise ListingError("Заавар 500 тэмдэгтээс ихгүй байна.")
    skill_id = values.get("skill_id")
    if skill_id is not None:
        skill = db.session.get(Skill, skill_id)
        if skill is None or not skill.is_active:
            raise ListingError("Ажлын төрөл буруу байна.")


def _new_public_code():
    return secrets.token_urlsafe(9)[:12]


# --- employers created by the office ---------------------------------------------------------

def find_or_create_employer(admin, phone, full_name, company_name):
    """For employers who phone the office. Reuses the account if the phone is known.

    New accounts get a random password nobody knows: the office manages their jobs;
    the employer can't log in until a password-reset feature exists."""
    _check_admin(admin)
    existing = db.session.scalar(select(User).filter_by(phone=phone))
    if existing is not None:
        if existing.role != Role.EMPLOYER:
            raise ListingError("Энэ утасны дугаар ажил олгогчийн бүртгэл биш.")
        return existing
    employer = User(phone=phone, full_name=full_name, company_name=company_name or None, role=Role.EMPLOYER)
    employer.set_password(secrets.token_urlsafe(32))
    db.session.add(employer)
    return employer


# --- create / edit ----------------------------------------------------------------------------

def create_job(admin, employer, values, publish=False):
    """An office admin enters a job from an employer's phone call. The office has
    accepted it by entering it, so it starts 'accepted' (and optionally published)."""
    try:
        _check_admin(admin)
        if employer is None or employer.role != Role.EMPLOYER:
            raise ListingError("Ажил олгогч сонгоно уу.")
        _check_values(values, is_new=True)
        job = Job(employer=employer, status=JobStatus.ACCEPTED,
                  **{field: values[field] for field in JOB_FIELDS})
        job.status_changes.append(JobStatusChange(
            to_status=JobStatus.ACCEPTED, changed_by=admin, note="Товчоо бүртгэж зөвшөөрсөн",
        ))
        listing = JobListing(job=job, public_code=_new_public_code(), updated_by=admin,
                             **{field: values.get(field) for field in LISTING_FIELDS})
        listing.changes.append(ListingChange(action="created", changed_by=admin))
        db.session.add_all([job, listing])
        db.session.flush()
        notifications.employer_job_accepted(job)
        if publish:
            _publish(job, admin)
        db.session.commit()
        return job
    except Exception:
        db.session.rollback()
        raise


def update_job(job_id, admin, values):
    """Edit a job and its listing (creating the listing for employer-submitted jobs)."""
    try:
        lock_row(Job, job_id)
        admin = db.session.get(User, admin.id)
        _check_admin(admin)
        job = db.session.get(Job, job_id)
        if job is None:
            raise ListingError("Ажил олдсонгүй.")
        if job.status not in EDITABLE_STATUSES:
            raise ListingError("Эхэлсэн, дууссан эсвэл цуцлагдсан ажлыг засах боломжгүй.")

        listing = job.listing
        # Validate (and normalise) first; the date rule only applies if the date changes.
        values["_changed"] = ("work_date",) if job.work_date != values["work_date"] else ()
        _check_values(values, is_new=False)
        changed = [f for f in JOB_FIELDS if getattr(job, f) != values[f]]
        for field in LISTING_FIELDS:
            old = getattr(listing, field) if listing is not None else None
            if field in TEXT_FIELDS:
                old = old or ""
            if old != values.get(field):
                changed.append(field)

        taken = positions_taken(job.id)
        if taken and any(f in changed for f in LOCKED_WHEN_STAFFED):
            raise ListingError("Илгээсэн/томилогдсон ажилтантай үед огноо, цаг, цалин өөрчлөх боломжгүй.")
        if values["workers_needed"] < taken:
            raise ListingError(f"{taken} ажилтан аль хэдийн илгээгдсэн/томилогдсон тул ажилтны тоог үүнээс бага болгох боломжгүй.")

        for field in JOB_FIELDS:
            setattr(job, field, values[field])
        if listing is None:
            listing = JobListing(job=job, public_code=_new_public_code(), updated_by=admin)
            listing.changes.append(ListingChange(action="created", changed_by=admin))
            db.session.add(listing)
        for field in LISTING_FIELDS:
            setattr(listing, field, values.get(field))
        listing.updated_at = utcnow()
        listing.updated_by = admin
        if changed:
            labels = sorted({FIELD_LABELS[f] for f in changed})
            change = ListingChange(action="edited", changed_by=admin, note=", ".join(labels)[:300])
            listing.changes.append(change)
            important = sorted({FIELD_LABELS[f] for f in changed if f in NOTIFY_FIELDS})
            if important:
                db.session.flush()  # the change id makes each notification unique
                recipients = [a.worker for a in job.pending_applications] + [a.worker for a in job.active_assignments]
                notifications.job_details_changed(job, change.id, important, recipients)
        db.session.commit()
        return job
    except Exception:
        db.session.rollback()
        raise


# --- publish / pause --------------------------------------------------------------------------

def _publish(job, admin):
    if job.status != JobStatus.ACCEPTED:
        raise ListingError("Зөвхөн товчоо зөвшөөрсөн хүсэлтийг нийтэлнэ.")
    if job.work_date < office_today():
        raise ListingError("Ажлын өдөр өнгөрсөн тул нийтлэх боломжгүй.")
    listing = job.listing
    if listing is None:
        raise ListingError("Эхлээд зарын мэдээллийг (нийтэд харагдах байршил) бөглөнө үү.")
    if listing.is_published:
        raise ListingError("Зар аль хэдийн нийтлэгдсэн байна.")
    now = utcnow()
    listing.is_published = True
    listing.published_at = listing.published_at or now
    listing.updated_at = now
    listing.updated_by = admin
    listing.changes.append(ListingChange(action="published", changed_by=admin))


def publish(job, admin):
    try:
        _check_admin(admin)
        _publish(job, admin)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def pause(job, admin, note=""):
    try:
        _check_admin(admin)
        listing = job.listing
        if listing is None or not listing.is_published:
            raise ListingError("Зар нийтлэгдээгүй байна.")
        listing.is_published = False
        listing.updated_at = utcnow()
        listing.updated_by = admin
        listing.changes.append(ListingChange(action="paused", changed_by=admin, note=(note or "")[:300]))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


# --- the public board --------------------------------------------------------------------------

def public_jobs():
    """Jobs anyone may see right now, soonest first. Hidden: unpublished, paused,
    not accepted (pending, in progress, completed, cancelled), past dates, full."""
    candidates = db.session.scalars(
        select(Job)
        .join(JobListing, JobListing.job_id == Job.id)
        .where(
            JobListing.is_published.is_(True),
            Job.status == JobStatus.ACCEPTED,
            Job.work_date >= office_today(),
        )
        .options(
            selectinload(Job.listing).selectinload(JobListing.skill),
            selectinload(Job.assignments),
            selectinload(Job.referrals),
        )
        .order_by(Job.work_date, Job.start_time, Job.id)
    ).all()
    return [job for job in candidates if job.is_publicly_listed]


def job_by_code(code):
    """The job behind a public code, whatever its state (callers decide what to show)."""
    return db.session.scalar(
        select(Job).join(JobListing, JobListing.job_id == Job.id).where(JobListing.public_code == code)
    )


def public_job_by_code(code):
    job = job_by_code(code)
    return job if job is not None and job.is_publicly_listed else None


def public_card(job):
    """The complete set of data a public page or map marker may use. Nothing else
    (no employer, exact address, notes, phone, money totals or internal ids)."""
    listing = job.listing
    return {
        "code": listing.public_code,
        "title": job.title,
        "description": job.description,
        "job_type": listing.skill.name if listing.skill else None,
        "wage": job.wage_per_worker,
        "work_date": job.work_date,
        "start_time": job.start_time,
        "end_time": job.end_time,
        "area": listing.public_area,
        "remaining": job.positions_remaining,
        "workers_needed": job.workers_needed,
        "updated_at": listing.updated_at,
        "lat": listing.public_latitude,
        "lng": listing.public_longitude,
    }
