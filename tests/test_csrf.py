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
    assert csrf_client.post(f"/admin/applications/{w['application'].id}/accept").status_code == 400
    assert csrf_client.post(f"/admin/applications/{w['application'].id}/reject",
                            data={"reason": "x"}).status_code == 400
    assert csrf_client.post(f"/admin/jobs/{w['job'].id}/assignments",
                            data={"worker_id": str(w["applicant"].id), "agreed_wage": "50000"}).status_code == 400
    assert csrf_client.post(f"/admin/assignments/{w['assignment'].id}/cancel",
                            data={"reason": "Шалтгаан"}).status_code == 400
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


def test_finance_actions_need_csrf_token(csrf_client, staffing_world):
    """Every money-changing POST is refused without a CSRF token, and nothing is written."""
    from app.models import CommissionRate, JobFinance, JobFinanceLine, LedgerEntry

    admin = make_user_and_log_in(csrf_client, Role.ADMIN, phone="99000001")
    w = staffing_world
    job = w["job"]
    job.status = JobStatus.COMPLETED
    w["assignment"].attendance = "present"
    rate = CommissionRate(amount_per_worker=10000, created_by=admin)
    line = JobFinanceLine(assignment=w["assignment"], worker_id=w["assignment"].worker_id, wage=50000, commission=10000)
    db.session.add(JobFinance(job=job, commission_rate=rate, commission_per_worker=10000, total_wages=50000,
                              total_commission=10000, total_due=60000, calculation_version=1,
                              created_by=admin, lines=[line]))
    db.session.commit()

    posts = [
        ("/admin/finance/commission", {"amount": "5000"}),
        (f"/admin/jobs/{job.id}/finance/snapshot", {}),
        (f"/admin/jobs/{job.id}/finance/receipts", {"amount": "1000", "method": "cash", "request_token": "a"}),
        (f"/admin/jobs/{job.id}/finance/lines/{line.id}/payouts",
         {f"line{line.id}-amount": "1000", f"line{line.id}-method": "cash"}),
        (f"/admin/jobs/{job.id}/finance/entries/1/reverse", {"rev1-reason": "шалтгаан байна"}),
    ]
    for url, data in posts:
        assert csrf_client.post(url, data=data).status_code == 400, url
    db.session.expire_all()
    assert db.session.scalar(db.select(db.func.count(LedgerEntry.id))) == 0
    assert db.session.scalar(db.select(db.func.count(CommissionRate.id))) == 1


def test_listing_and_referral_actions_need_csrf_token(csrf_client, staffing_world):
    from app.models import JobListing, Referral, utcnow

    admin = make_user_and_log_in(csrf_client, Role.ADMIN, phone="99000001")
    job = staffing_world["job"]
    db.session.add(JobListing(job=job, public_code="csrf0001", public_area="Эрдэнэт", is_published=True,
                              published_at=utcnow(), updated_by=admin))
    referral = Referral(job=job, caller_phone="88001122", created_by=admin)
    db.session.add(referral)
    db.session.commit()

    posts = [
        ("/admin/listings/new", {"title": "Шинэ"}),
        (f"/admin/jobs/{job.id}/listing", {"title": "Хакердсан"}),
        (f"/admin/jobs/{job.id}/listing/publish", {}),
        (f"/admin/jobs/{job.id}/listing/pause", {}),
        (f"/admin/jobs/{job.id}/listing/cancel", {"reason": "шалтгаан"}),
        (f"/admin/jobs/{job.id}/referrals", {"phone": "88009999", "request_token": "t"}),
        (f"/admin/jobs/{job.id}/referrals/{referral.id}/contact-shared", {}),
        (f"/admin/jobs/{job.id}/referrals/{referral.id}/cancel", {"reason": "шалтгаан"}),
        (f"/admin/jobs/{job.id}/referrals/{referral.id}/place", {"agreed_wage": "50000"}),
    ]
    for url, data in posts:
        assert csrf_client.post(url, data=data).status_code == 400, url
    db.session.expire_all()
    job = db.session.get(Job, job.id)
    assert job.title == "Ажил" and job.status == JobStatus.ACCEPTED and job.listing.is_published
    assert [(r.status, r.contact_shared_at) for r in job.referrals] == [("referred", None)]


def test_public_board_filters_work_without_token(csrf_client):
    assert csrf_client.get("/?q=ачаа&when=today").status_code == 200


def test_forms_include_csrf_token(csrf_client):
    make_user_and_log_in(csrf_client, Role.EMPLOYER)
    html = csrf_client.get("/employer/jobs/new").get_data(as_text=True)
    assert 'name="csrf_token"' in html


def test_admin_filter_works_without_token(csrf_client):
    make_user_and_log_in(csrf_client, Role.ADMIN, phone="99000001")
    assert csrf_client.get("/admin/jobs?status=pending").status_code == 200


# --- Milestone 8: applying from the board, office decisions, disputes -------------------------------

@pytest.fixture
def marketplace_world(staffing_world):
    """staffing_world's job, published, dated today (so disputes are allowed)."""
    from app.models import JobListing, utcnow

    job = staffing_world["job"]
    job.work_date = office_today()
    admin = db.session.scalar(db.select(User).filter_by(role=Role.ADMIN))
    db.session.add(JobListing(job=job, public_code="csrf0001", public_area="Эрдэнэт", is_published=True,
                              published_at=utcnow(), updated_by=admin))
    db.session.commit()
    return staffing_world


def test_public_apply_needs_csrf_token(csrf_client, marketplace_world):
    from app.models import JobApplication, WorkerProfile
    worker = make_user_and_log_in(csrf_client, Role.WORKER, phone="99110007")
    worker.worker_profile = WorkerProfile(skills=[db.session.scalar(db.select(Skill))])
    db.session.commit()
    assert csrf_client.post("/jobs/csrf0001/apply").status_code == 400
    assert db.session.scalar(db.select(JobApplication).filter_by(worker_id=worker.id)) is None
    # The page itself carries the token in its form.
    assert 'name="csrf_token"' in csrf_client.get("/jobs/csrf0001").get_data(as_text=True)


def test_decisions_and_disputes_need_csrf_token(csrf_client, marketplace_world):
    from app.models import AttendanceDispute
    make_user_and_log_in(csrf_client, Role.ADMIN, phone="99000001")
    w = marketplace_world
    posts = [
        (f"/admin/applications/{w['application'].id}/accept", {}),
        (f"/admin/applications/{w['application'].id}/reject", {"reason": "x"}),
        (f"/admin/assignments/{w['assignment'].id}/disputes", {"message": "Хянагч утсаар"}),
    ]
    for url, data in posts:
        assert csrf_client.post(url, data=data).status_code == 400, url
    assert _counts() == (1, "pending")
    assert db.session.scalar(db.select(db.func.count(AttendanceDispute.id))) == 0


def test_worker_dispute_needs_csrf_token(csrf_client, marketplace_world):
    from app.models import AttendanceDispute
    w = marketplace_world
    with csrf_client.session_transaction() as session:
        session["_user_id"] = str(w["assignment"].worker_id)
    assert csrf_client.post(f"/worker/assignments/{w['assignment'].id}/dispute",
                            data={"message": "Ирсэн байсан"}).status_code == 400
    assert db.session.scalar(db.select(db.func.count(AttendanceDispute.id))) == 0
