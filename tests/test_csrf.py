import pytest

from app import create_app
from app.extensions import db
from app.models import DEFAULT_SKILLS, Job, JobStatus, Role, Skill, User
from app.validation import office_today
from tests.conftest import job_form_data, tomorrow


@pytest.fixture
def csrf_app():
    app = create_app("testing", {"WTF_CSRF_ENABLED": True})
    with app.app_context():
        db.create_all()
        for code, name in DEFAULT_SKILLS:
            db.session.add(Skill(code=code, name=name))
        db.session.commit()
        yield app
        db.drop_all()


@pytest.fixture
def csrf_client(csrf_app):
    return csrf_app.test_client()


def make_user_and_log_in(client, role, phone="99110001"):
    """Log in by writing the session directly (the login form itself needs a token)."""
    user = User(phone=phone, full_name="Тест", role=role)
    user.set_password("nuuts-ug-123")
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True
    return user


def make_pending_job(employer):
    from datetime import time
    job = Job(employer=employer, title="Ажил", description="Тайлбар тайлбар", location="Хаяг",
              work_date=tomorrow(), start_time=time(9), end_time=time(17),
              workers_needed=1, wage_per_worker=50000)
    db.session.add(job)
    db.session.commit()
    return job


def test_login_without_csrf_token_is_rejected(csrf_client):
    response = csrf_client.post("/auth/login", data={"phone": "99110001", "password": "x" * 8})
    assert response.status_code == 400
    assert "Хүсэлт буруу байна" in response.get_data(as_text=True)


def test_profile_edit_without_csrf_token_is_rejected(csrf_client):
    make_user_and_log_in(csrf_client, Role.WORKER)
    response = csrf_client.post("/worker/profile/edit", data={"availability": "unavailable"})
    assert response.status_code == 400


def test_registration_without_csrf_token_is_rejected(csrf_client):
    response = csrf_client.post("/auth/register/worker", data={
        "full_name": "Бат", "phone": "99110001",
        "password": "nuuts-ug-123", "confirm_password": "nuuts-ug-123",
    })
    assert response.status_code == 400


def test_job_request_without_csrf_token_is_rejected(csrf_client):
    make_user_and_log_in(csrf_client, Role.EMPLOYER)
    response = csrf_client.post("/employer/jobs/new", data=job_form_data())
    assert response.status_code == 400
    assert db.session.scalar(db.select(db.func.count(Job.id))) == 0


def test_job_cancel_without_csrf_token_is_rejected(csrf_client):
    employer = make_user_and_log_in(csrf_client, Role.EMPLOYER)
    job = make_pending_job(employer)
    assert csrf_client.post(f"/employer/jobs/{job.id}/cancel").status_code == 400
    db.session.expire_all()
    assert db.session.get(Job, job.id).status == JobStatus.PENDING


def test_admin_status_change_without_csrf_token_is_rejected(csrf_client):
    employer = User(phone="99330001", full_name="Компани", role=Role.EMPLOYER, password_hash="x")
    db.session.add(employer)
    job = make_pending_job(employer)
    make_user_and_log_in(csrf_client, Role.ADMIN, phone="99000001")
    response = csrf_client.post(f"/admin/jobs/{job.id}/status", data={"new_status": "accepted"})
    assert response.status_code == 400
    db.session.expire_all()
    assert db.session.get(Job, job.id).status == JobStatus.PENDING


@pytest.fixture
def staffing_world(csrf_app):
    """An accepted job, a ready worker with a pending application, and one active assignment."""
    from datetime import time

    from app.models import Assignment, JobApplication, WorkerProfile

    cleaning = db.session.scalar(db.select(Skill).filter_by(code="cleaning"))
    employer = User(phone="99330001", full_name="Компани", role=Role.EMPLOYER, password_hash="x")
    admin = User(phone="99000009", full_name="Админ", role=Role.ADMIN, password_hash="x")
    applicant = User(phone="99110005", full_name="Өргөдөлтэй", role=Role.WORKER, password_hash="x")
    applicant.worker_profile = WorkerProfile(skills=[cleaning])
    assigned = User(phone="99110006", full_name="Томилогдсон", role=Role.WORKER, password_hash="x")
    assigned.worker_profile = WorkerProfile(skills=[cleaning])
    job = Job(employer=employer, title="Ажил", description="Тайлбар тайлбар", location="Хаяг",
              work_date=tomorrow(), start_time=time(9), end_time=time(17),
              workers_needed=3, wage_per_worker=50000, status=JobStatus.ACCEPTED)
    application = JobApplication(job=job, worker=applicant)
    assignment = Assignment(job=job, worker=assigned, agreed_wage=50000, created_by=admin)
    db.session.add_all([employer, admin, applicant, assigned, job, application, assignment])
    db.session.commit()
    return dict(job=job, application=application, assignment=assignment, applicant=applicant)


def _counts():
    from app.models import Assignment, JobApplication
    db.session.expire_all()
    return (
        db.session.scalar(db.select(db.func.count(Assignment.id)).filter_by(status="active")),
        db.session.scalar(db.select(JobApplication.status)),
    )


def test_worker_apply_and_withdraw_need_csrf_token(csrf_client, staffing_world):
    make_user_and_log_in(csrf_client, Role.WORKER, phone="99110007")
    # The applicant themself, for withdraw:
    with csrf_client.session_transaction() as session:
        session["_user_id"] = str(staffing_world["applicant"].id)
    assert csrf_client.post(f"/worker/applications/{staffing_world['application'].id}/withdraw").status_code == 400
    assert csrf_client.post(f"/worker/jobs/{staffing_world['job'].id}/apply").status_code == 400
    assert _counts() == (1, "pending")


def test_admin_staffing_actions_need_csrf_token(csrf_client, staffing_world):
    make_user_and_log_in(csrf_client, Role.ADMIN, phone="99000001")
    w = staffing_world
    assert csrf_client.post(f"/admin/applications/{w['application'].id}/review",
                            data={"decision": "approved"}).status_code == 400
    assert csrf_client.post(f"/admin/jobs/{w['job'].id}/assignments",
                            data={"worker_id": str(w["applicant"].id), "agreed_wage": "50000"}).status_code == 400
    assert csrf_client.post(f"/admin/assignments/{w['assignment'].id}/cancel").status_code == 400
    assert _counts() == (1, "pending")


def test_attendance_needs_csrf_token(csrf_client, staffing_world):
    from app.models import Assignment
    make_user_and_log_in(csrf_client, Role.ADMIN, phone="99000001")
    w = staffing_world
    w["job"].work_date = office_today()
    db.session.commit()
    response = csrf_client.post(f"/admin/assignments/{w['assignment'].id}/attendance", data={"attendance": "present"})
    assert response.status_code == 400
    db.session.expire_all()
    assert db.session.get(Assignment, w["assignment"].id).attendance == "not_checked"


def test_notification_actions_need_csrf_token(csrf_client):
    from app.models import Notification
    user = make_user_and_log_in(csrf_client, Role.WORKER)
    db.session.add(Notification(recipient=user, event_key="t:1", title="Гарчиг", message="Мессеж"))
    db.session.commit()
    note_id = db.session.scalar(db.select(Notification.id))
    assert csrf_client.post(f"/notifications/{note_id}/open").status_code == 400
    assert csrf_client.post("/notifications/read-all").status_code == 400
    db.session.expire_all()
    assert db.session.get(Notification, note_id).read_at is None


def test_company_name_edit_needs_csrf_token(csrf_client):
    user = make_user_and_log_in(csrf_client, Role.EMPLOYER)
    assert csrf_client.post("/employer/profile", data={"company_name": "X ХХК"}).status_code == 400
    db.session.expire_all()
    assert db.session.get(User, user.id).company_name is None


def test_forms_include_csrf_token(csrf_client):
    make_user_and_log_in(csrf_client, Role.EMPLOYER)
    html = csrf_client.get("/employer/jobs/new").get_data(as_text=True)
    assert 'name="csrf_token"' in html


def test_admin_filter_works_without_token(csrf_client):
    make_user_and_log_in(csrf_client, Role.ADMIN, phone="99000001")
    assert csrf_client.get("/admin/jobs?status=pending").status_code == 200
