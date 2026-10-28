from datetime import time, timedelta

import pytest

from app import create_app
from app.extensions import db
from app.models import (
    DEFAULT_SKILLS,
    Availability,
    Job,
    JobStatus,
    JobStatusChange,
    Role,
    Skill,
    User,
    WorkerProfile,
)
from app.validation import office_today

DEFAULT_PASSWORD = "nuuts-ug-123"


def tomorrow():
    return office_today() + timedelta(days=1)


def job_form_data(**overrides):
    """A valid employer job request form; override single fields to test validation."""
    data = {
        "title": "Ачаа буулгах",
        "description": "Ачааны машинаас 200 шуудай цемент буулгана.",
        "location": "Баян-Өндөр сум, 5-р баг",
        "work_date": tomorrow().isoformat(),
        "start_time": "09:00",
        "end_time": "17:00",
        "workers_needed": "3",
        "wage_per_worker": "80000",
        "notes": "Ажлын бээлийтэй ирнэ үү.",
    }
    data.update(overrides)
    return data


def seed_default_skills():
    """create_all() builds tables but not the data a migration inserts, so add the skills here."""
    for position, (code, name) in enumerate(DEFAULT_SKILLS, start=1):
        db.session.add(Skill(code=code, name=name, sort_order=position * 10))
    db.session.commit()


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        seed_default_skills()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def make_user(app):
    """Create a user directly in the database."""
    def _make_user(phone="99110001", role=Role.WORKER, password=DEFAULT_PASSWORD,
                   full_name="Бат Дорж", is_active=True):
        user = User(phone=phone, full_name=full_name, role=role, is_active=is_active)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return user
    return _make_user


@pytest.fixture
def make_job(app):
    """Create a job directly in the database (bypassing the form)."""
    def _make_job(employer, status=JobStatus.PENDING, **overrides):
        values = dict(
            title="Тавилга зөөх", description="Гурван өрөө байрны тавилга зөөнө.",
            location="Шинэ төв, 12-р байр", work_date=tomorrow(),
            start_time=time(9, 0), end_time=time(13, 0),
            workers_needed=2, wage_per_worker=60000,
        )
        values.update(overrides)
        job = Job(employer=employer, status=status, **values)
        job.status_changes.append(JobStatusChange(to_status=status, changed_by=employer))
        db.session.add(job)
        db.session.commit()
        return job
    return _make_job


@pytest.fixture
def make_worker(make_user):
    """A worker with a profile. Defaults to a complete, available profile."""
    def _make_worker(phone, full_name="Бат Дорж", skills=("cleaning",),
                     availability=Availability.AVAILABLE, description="", is_active=True):
        user = make_user(phone=phone, role=Role.WORKER, full_name=full_name, is_active=is_active)
        user.worker_profile = WorkerProfile(availability=availability, description=description)
        user.worker_profile.skills = list(db.session.scalars(db.select(Skill).where(Skill.code.in_(skills))))
        db.session.commit()
        return user
    return _make_worker


@pytest.fixture
def admin_user(make_user):
    return make_user(phone="99000001", role=Role.ADMIN, full_name="Товчооны админ")


@pytest.fixture
def assign(admin_user):
    """Assign through the real service, as the admin would."""
    from app.services.staffing import assign_worker

    def _assign(job, worker, agreed_wage=None):
        return assign_worker(job.id, worker.id, agreed_wage or job.wage_per_worker, admin_user.id)
    return _assign


@pytest.fixture
def apply(app):
    """A worker applies through the real service (job must be a published listing)."""
    from app.services import applications

    def _apply(job, worker):
        return applications.apply(job.id, worker.id)
    return _apply


@pytest.fixture
def accept(admin_user):
    """The office accepts an application through the real service; returns the assignment."""
    from app.services import applications

    def _accept(application, admin=None):
        return applications.accept(application.id, (admin or admin_user).id)
    return _accept


@pytest.fixture
def make_listed_job(make_job, admin_user):
    """An accepted job with a public listing (published by default)."""
    from app.models import JobListing, ListingChange, utcnow

    counter = {"n": 0}

    def _make(employer, published=True, area="5-р баг, Шинэ төв орчим", lat=None, lng=None,
              skill_code=None, internal_notes="", **job_overrides):
        job_overrides.setdefault("status", JobStatus.ACCEPTED)
        job = make_job(employer, **job_overrides)
        counter["n"] += 1
        skill = db.session.scalar(db.select(Skill).filter_by(code=skill_code)) if skill_code else None
        now = utcnow()
        listing = JobListing(job=job, public_code=f"code{counter['n']:04d}", public_area=area,
                             latitude=lat, longitude=lng, skill=skill, internal_notes=internal_notes,
                             is_published=published, published_at=now if published else None,
                             updated_at=now, updated_by=admin_user)
        listing.changes.append(ListingChange(action="created", changed_by=admin_user))
        db.session.add(listing)
        db.session.commit()
        return job
    return _make


@pytest.fixture
def finance_employer(make_user):
    return make_user(phone="99330001", role=Role.EMPLOYER, full_name="Ганбат Дорж")


@pytest.fixture
def make_completed_job(make_job, make_worker, assign, admin_user, finance_employer):
    """Build a completed job through the real services.

    `workers` is a list of (attendance, agreed_wage); "cancelled" means the assignment is
    cancelled before completion. `commission=None` leaves the commission unconfigured.
    Returns the job; its snapshot (if any) is job.finance."""
    from app.services import finance as finance_service
    from app.services.attendance import record_attendance
    from app.services.jobs import change_job_status
    from app.services.staffing import cancel_assignment

    counter = {"n": 0}

    def _make(workers=(("present", 60000),), commission=10000, title="Ачаа буулгах"):
        if commission is not None:
            finance_service.set_commission(commission, admin_user)
        job = make_job(finance_employer, status=JobStatus.ACCEPTED, work_date=office_today(),
                       workers_needed=max(len(workers), 1), title=title)
        assignments = []
        for attendance, wage in workers:
            counter["n"] += 1
            worker = make_worker(f"9911{counter['n']:04d}", full_name=f"Ажилтан Дугаар{counter['n']}")
            assignments.append((assign(job, worker, agreed_wage=wage), attendance))
        change_job_status(db.session.get(Job, job.id), JobStatus.IN_PROGRESS, admin_user)
        for assignment, attendance in assignments:
            if attendance == "cancelled":
                cancel_assignment(assignment, admin_user, "Ажилтан ирэхгүй болсон")
            else:
                record_attendance(assignment, attendance, admin_user)
        change_job_status(db.session.get(Job, job.id), JobStatus.COMPLETED, admin_user)
        db.session.expire_all()
        return db.session.get(Job, job.id)
    return _make


@pytest.fixture
def skills(app):
    """The seeded skills, keyed by code: skills["cleaning"].id"""
    return {skill.code: skill for skill in db.session.scalars(db.select(Skill))}


@pytest.fixture
def login(client):
    """Log in through the real login form."""
    def _login(phone, password=DEFAULT_PASSWORD, next_url=None):
        url = "/auth/login" + (f"?next={next_url}" if next_url else "")
        return client.post(url, data={"phone": phone, "password": password})
    return _login
