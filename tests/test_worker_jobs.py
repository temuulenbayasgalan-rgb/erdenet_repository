"""Worker pages: browsing jobs, applying, withdrawing, and seeing assignments."""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import ApplicationStatus, Availability, JobApplication, JobStatus, Role
from app.validation import office_today


@pytest.fixture
def employer(make_user):
    return make_user(phone="99330001", role=Role.EMPLOYER, full_name="Нууц Компани ХХК")


@pytest.fixture
def worker(make_worker, login):
    user = make_worker("99110001", full_name="Бат Дорж")
    login("99110001")
    return user


@pytest.fixture
def open_job(make_job, employer):
    return make_job(employer, status=JobStatus.ACCEPTED, title="Нээлттэй ажил",
                    notes="Бээлийтэй ирэх", workers_needed=2)


def application_of(worker, job):
    db.session.expire_all()
    return db.session.scalar(db.select(JobApplication).filter_by(worker_id=worker.id, job_id=job.id))


# --- browsing --------------------------------------------------------------------------

def test_worker_sees_only_open_jobs(client, worker, make_job, employer, open_job, make_worker, assign):
    make_job(employer, status=JobStatus.PENDING, title="Хүлээгдэж буй ажил")
    make_job(employer, status=JobStatus.CANCELLED, title="Цуцлагдсан ажил")
    make_job(employer, status=JobStatus.IN_PROGRESS, title="Явагдаж буй ажил")
    make_job(employer, status=JobStatus.ACCEPTED, title="Өнгөрсөн ажил",
             work_date=office_today() - timedelta(days=1))
    full = make_job(employer, status=JobStatus.ACCEPTED, title="Дүүрсэн ажил", workers_needed=1)
    assign(full, make_worker("99110009"))

    html = client.get("/worker/jobs").get_data(as_text=True)
    assert "Нээлттэй ажил" in html
    for hidden in ("Хүлээгдэж буй ажил", "Цуцлагдсан ажил", "Явагдаж буй ажил", "Өнгөрсөн ажил", "Дүүрсэн ажил"):
        assert hidden not in html


def test_job_detail_shows_work_info_but_not_employer(client, worker, open_job):
    html = client.get(f"/worker/jobs/{open_job.id}").get_data(as_text=True)
    for text in ("Нээлттэй ажил", open_job.location, open_job.description, "60 000 ₮", "Бээлийтэй ирэх"):
        assert text in html
    assert "Нууц Компани ХХК" not in html
    assert "99330001" not in html
    assert "Нийт цалин" not in html
    assert "Өргөдөл гаргах" in html


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.CANCELLED, JobStatus.COMPLETED])
def test_unrelated_closed_job_is_404(client, worker, make_job, employer, status):
    job = make_job(employer, status=status)
    assert client.get(f"/worker/jobs/{job.id}").status_code == 404
    assert client.post(f"/worker/jobs/{job.id}/apply").status_code == 404
    assert application_of(worker, job) is None


def test_missing_job_is_404(client, worker):
    assert client.get("/worker/jobs/99999").status_code == 404
    assert client.post("/worker/jobs/99999/apply").status_code == 404


def test_incomplete_profile_gets_prompt(client, make_worker, login, open_job):
    make_worker("99110002", skills=())
    login("99110002")
    html = client.get("/worker/jobs").get_data(as_text=True)
    assert "ур чадвараа сонгоно уу" in html


# --- applying ----------------------------------------------------------------------------

def test_worker_applies(client, worker, open_job):
    response = client.post(f"/worker/jobs/{open_job.id}/apply", follow_redirects=True)
    assert "Өргөдөл илгээгдлээ." in response.get_data(as_text=True)
    assert application_of(worker, open_job).status == ApplicationStatus.PENDING
    html = client.get(f"/worker/jobs/{open_job.id}").get_data(as_text=True)
    assert "Хянагдаж байна" in html
    assert "Өргөдөл гаргах</button>" not in html


def test_duplicate_application_is_rejected(client, worker, open_job):
    client.post(f"/worker/jobs/{open_job.id}/apply")
    response = client.post(f"/worker/jobs/{open_job.id}/apply", follow_redirects=True)
    assert "Та энэ ажилд өргөдөл гаргасан байна." in response.get_data(as_text=True)
    assert db.session.scalar(db.select(db.func.count(JobApplication.id))) == 1


def test_unavailable_worker_cannot_apply(client, make_worker, login, open_job):
    user = make_worker("99110002", availability=Availability.UNAVAILABLE)
    login("99110002")
    response = client.post(f"/worker/jobs/{open_job.id}/apply", follow_redirects=True)
    assert "Ажил хийх боломжтой" in response.get_data(as_text=True)
    assert application_of(user, open_job) is None


def test_crafted_fields_cannot_set_application_status(client, worker, open_job):
    client.post(f"/worker/jobs/{open_job.id}/apply", data={"status": "approved", "worker_id": "1"})
    assert application_of(worker, open_job).status == ApplicationStatus.PENDING


# --- my applications ------------------------------------------------------------------------

def test_applications_page_lists_own_only(client, worker, open_job, make_worker, make_job, employer):
    from app.services.staffing import apply_to_job
    other = make_worker("99110002")
    other_job = make_job(employer, status=JobStatus.ACCEPTED, title="Бусдын өргөдөл")
    apply_to_job(other_job, other)
    client.post(f"/worker/jobs/{open_job.id}/apply")

    html = client.get("/worker/applications").get_data(as_text=True)
    assert "Нээлттэй ажил" in html
    assert "Бусдын өргөдөл" not in html


def test_worker_withdraws_pending_application(client, worker, open_job):
    client.post(f"/worker/jobs/{open_job.id}/apply")
    application = application_of(worker, open_job)
    response = client.post(f"/worker/applications/{application.id}/withdraw", follow_redirects=True)
    assert "Өргөдлөө татан авлаа." in response.get_data(as_text=True)
    assert application_of(worker, open_job).status == ApplicationStatus.WITHDRAWN

    # ... and can apply again.
    client.post(f"/worker/jobs/{open_job.id}/apply")
    assert application_of(worker, open_job).status == ApplicationStatus.PENDING


@pytest.mark.parametrize("status", [ApplicationStatus.APPROVED, ApplicationStatus.REJECTED])
def test_reviewed_application_cannot_be_withdrawn(client, worker, open_job, admin_user, status):
    from app.services.staffing import apply_to_job, review_application
    application = apply_to_job(open_job, worker)
    review_application(application, status, admin_user)
    response = client.post(f"/worker/applications/{application.id}/withdraw", follow_redirects=True)
    assert "Зөвхөн хянагдаж буй" in response.get_data(as_text=True)
    assert application_of(worker, open_job).status == status


def test_worker_cannot_withdraw_someone_elses_application(client, worker, open_job, make_worker):
    from app.services.staffing import apply_to_job
    other = make_worker("99110002")
    application = apply_to_job(open_job, other)
    assert client.post(f"/worker/applications/{application.id}/withdraw").status_code == 404
    assert application_of(other, open_job).status == ApplicationStatus.PENDING


def test_worker_cannot_approve_own_application(client, worker, open_job):
    client.post(f"/worker/jobs/{open_job.id}/apply")
    application = application_of(worker, open_job)
    for decision in (ApplicationStatus.APPROVED, ApplicationStatus.REJECTED):
        response = client.post(f"/admin/applications/{application.id}/review", data={"decision": decision})
        assert response.status_code == 403
    assert client.post(f"/admin/jobs/{open_job.id}/assignments",
                       data={"worker_id": worker.id, "agreed_wage": "60000"}).status_code == 403
    assert application_of(worker, open_job).status == ApplicationStatus.PENDING


def test_withdraw_requires_post(client, worker, open_job):
    client.post(f"/worker/jobs/{open_job.id}/apply")
    application = application_of(worker, open_job)
    assert client.get(f"/worker/applications/{application.id}/withdraw").status_code == 405


# --- my assignments ---------------------------------------------------------------------------

def test_assignments_page_shows_own_assignments(client, worker, open_job, make_worker, make_job,
                                                employer, assign, admin_user):
    from app.services.staffing import cancel_assignment
    assign(open_job, worker, agreed_wage=70000)
    other_job = make_job(employer, status=JobStatus.ACCEPTED, title="Бусдын томилолт",
                         work_date=office_today() + timedelta(days=3))
    assign(other_job, make_worker("99110002"))
    cancelled_job = make_job(employer, status=JobStatus.ACCEPTED, title="Цуцалсан томилолт",
                             work_date=office_today() + timedelta(days=4))
    cancel_assignment(assign(cancelled_job, worker), admin_user)

    html = client.get("/worker/assignments").get_data(as_text=True)
    assert "Нээлттэй ажил" in html
    assert "70 000 ₮" in html
    assert "Бусдын томилолт" not in html
    assert "Цуцалсан томилолт" in html  # listed under cancelled
    assert "Нууц Компани ХХК" not in html


def test_assigned_worker_can_view_job_after_it_fills(client, worker, make_job, employer, assign):
    job = make_job(employer, status=JobStatus.ACCEPTED, workers_needed=1)
    assign(job, worker)
    response = client.get(f"/worker/jobs/{job.id}")
    assert response.status_code == 200
    assert "Та энэ ажилд томилогдсон" in response.get_data(as_text=True)


def test_application_is_not_shown_as_assignment(client, worker, open_job):
    client.post(f"/worker/jobs/{open_job.id}/apply")
    html = client.get("/worker/assignments").get_data(as_text=True)
    assert "Нээлттэй ажил" not in html
    assert "томилогдсон ажил алга" in html
