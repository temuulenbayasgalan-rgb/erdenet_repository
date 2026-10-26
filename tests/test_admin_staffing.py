"""Admin pages for reviewing applications and making assignments."""
from datetime import time

import pytest

from app.extensions import db
from app.models import ApplicationStatus, Assignment, AssignmentStatus, Availability, JobApplication, JobStatus, Role
from app.services.staffing import apply_to_job


@pytest.fixture
def admin(admin_user, login):
    login(admin_user.phone)
    return admin_user


@pytest.fixture
def employer(make_user):
    return make_user(phone="99330001", role=Role.EMPLOYER, full_name="Эрдэнэ Трейд ХХК")


@pytest.fixture
def job(make_job, employer):
    return make_job(employer, status=JobStatus.ACCEPTED, workers_needed=2, wage_per_worker=60000,
                    start_time=time(9), end_time=time(13))


def assignments_for(job):
    db.session.expire_all()
    return db.session.scalars(db.select(Assignment).filter_by(job_id=job.id)).all()


def post_assign(client, job, worker_id, wage="60000", **extra):
    return client.post(f"/admin/jobs/{job.id}/assignments",
                       data={"worker_id": str(worker_id), "agreed_wage": wage, **extra})


# --- application review ----------------------------------------------------------------------

def test_applications_page_shows_worker_details(client, admin, job, make_worker):
    worker = make_worker("99110001", full_name="Бат Дорж", skills=("cleaning", "loading"),
                         description="Туршлагатай")
    apply_to_job(job, worker)
    html = client.get(f"/admin/jobs/{job.id}/applications").get_data(as_text=True)
    for text in ("Бат Дорж", "99110001", "Цэвэрлэгээ", "Ачаа ачих, буулгах", "Ажил хийх боломжтой",
                 "Туршлагатай", "Хянагдаж байна", "Тэнцүүлэх", "Татгалзах", "Томилох"):
        assert text in html


def test_filter_applications_by_status(client, admin, job, make_worker):
    pending = make_worker("99110001", full_name="Хүлээж Буй")
    withdrawn = make_worker("99110002", full_name="Татсан Хүн")
    apply_to_job(job, pending)
    from app.services.staffing import withdraw_application
    withdraw_application(apply_to_job(job, withdrawn))

    html = client.get(f"/admin/jobs/{job.id}/applications?status=withdrawn").get_data(as_text=True)
    assert "Татсан Хүн" in html and "Хүлээж Буй" not in html
    html = client.get(f"/admin/jobs/{job.id}/applications?status=bogus").get_data(as_text=True)
    assert "Шүүлтүүрийн утга буруу" in html and "Хүлээж Буй" in html


@pytest.mark.parametrize("decision, label", [
    (ApplicationStatus.APPROVED, "Тэнцсэн"), (ApplicationStatus.REJECTED, "Тэнцээгүй"),
])
def test_admin_reviews_application(client, admin, job, make_worker, decision, label):
    application = apply_to_job(job, make_worker("99110001"))
    response = client.post(f"/admin/applications/{application.id}/review",
                           data={"decision": decision}, follow_redirects=True)
    assert f"Өргөдөл «{label}» боллоо." in response.get_data(as_text=True)
    db.session.expire_all()
    assert application.status == decision
    assert application.reviewed_by_id == admin.id
    # Approval alone does not assign.
    assert assignments_for(job) == []


@pytest.mark.parametrize("decision", ["withdrawn", "pending", "", "assigned"])
def test_invalid_review_decision(client, admin, job, make_worker, decision):
    application = apply_to_job(job, make_worker("99110001"))
    response = client.post(f"/admin/applications/{application.id}/review",
                           data={"decision": decision}, follow_redirects=True)
    assert "Шийдвэр буруу байна." in response.get_data(as_text=True)
    db.session.expire_all()
    assert application.status == ApplicationStatus.PENDING


def test_review_missing_application_is_404(client, admin):
    assert client.post("/admin/applications/99999/review", data={"decision": "approved"}).status_code == 404


# --- assignment through the admin pages ------------------------------------------------------------

def test_job_detail_lists_candidates_and_remaining(client, admin, job, make_worker):
    make_worker("99110001", full_name="Бэлэн Ажилтан")
    make_worker("99110002", full_name="Завгүй Ажилтан", availability=Availability.UNAVAILABLE)
    html = client.get(f"/admin/jobs/{job.id}").get_data(as_text=True)
    assert "Ажилтан томилох" in html
    assert "Бэлэн Ажилтан" in html
    assert "Завгүй Ажилтан" not in html
    assert "үлдсэн 2" in html


def test_admin_assigns_worker(client, admin, job, make_worker):
    worker = make_worker("99110001", full_name="Бат Дорж")
    response = post_assign(client, job, worker.id, wage="65 000")
    assert response.status_code == 302
    rows = assignments_for(job)
    assert [(r.worker_id, r.agreed_wage, r.status, r.created_by_id) for r in rows] == \
        [(worker.id, 65000, AssignmentStatus.ACTIVE, admin.id)]
    html = client.get(f"/admin/jobs/{job.id}").get_data(as_text=True)
    assert "Бат Дорж томилогдлоо." in html
    assert "(1 / 2," in html


def test_assign_from_applications_page_returns_there(client, admin, job, make_worker):
    worker = make_worker("99110001")
    apply_to_job(job, worker)
    response = post_assign(client, job, worker.id, back_to="applications")
    assert response.headers["Location"] == f"/admin/jobs/{job.id}/applications"
    db.session.expire_all()
    assert db.session.scalar(db.select(JobApplication.status)) == ApplicationStatus.APPROVED


def test_open_redirect_via_back_to_is_ignored(client, admin, job, make_worker):
    worker = make_worker("99110001")
    response = post_assign(client, job, worker.id, back_to="https://evil.example/")
    assert response.headers["Location"] == f"/admin/jobs/{job.id}"


@pytest.mark.parametrize("worker_id, wage, message", [
    ("", "60000", "Ажилтан сонгоно уу."),
    ("abc", "60000", "Бүхэл тоо оруулна уу."),
    ("WORKER", "", "Энэ талбарыг бөглөнө үү."),
    ("WORKER", "60000.5", "Мөнгөн дүнг бүхэл төгрөгөөр оруулна уу."),
    ("WORKER", "500", "Цалин 1 000-5 000 000 ₮ байх ёстой."),
    ("99999", "60000", "Ажилтан олдсонгүй."),
])
def test_assignment_form_errors(client, admin, job, make_worker, worker_id, wage, message):
    worker = make_worker("99110001")
    response = post_assign(client, job, worker.id if worker_id == "WORKER" else worker_id, wage=wage)
    assert response.status_code == 400
    assert message in response.get_data(as_text=True)
    assert assignments_for(job) == []


def test_rule_violations_are_shown(client, admin, job, make_worker, assign):
    inactive = make_worker("99110001", is_active=False)
    unavailable = make_worker("99110002", availability=Availability.TEMPORARILY_UNAVAILABLE)
    cases = [(inactive, "идэвхгүй"), (unavailable, "боломжгүй гэж тэмдэглэсэн")]
    for worker, message in cases:
        response = post_assign(client, job, worker.id)
        assert response.status_code == 400
        assert message in response.get_data(as_text=True)
    assert assignments_for(job) == []


def test_duplicate_and_limit_via_route(client, admin, job, make_worker):
    first, second, third = (make_worker(p) for p in ("99110001", "99110002", "99110003"))
    post_assign(client, job, first.id)
    assert "аль хэдийн томилогдсон" in post_assign(client, job, first.id).get_data(as_text=True)
    post_assign(client, job, second.id)
    response = post_assign(client, job, third.id)
    assert "орон тоо дүүрсэн" in response.get_data(as_text=True)
    assert len(assignments_for(job)) == 2
    html = client.get(f"/admin/jobs/{job.id}").get_data(as_text=True)
    assert "Бүх орон тоо дүүрсэн." in html
    assert "Ажилтан томилох" not in html


def test_overlap_via_route(client, admin, job, make_job, employer, make_worker):
    worker = make_worker("99110001")
    other = make_job(employer, status=JobStatus.ACCEPTED, start_time=time(11), end_time=time(15))
    post_assign(client, job, worker.id)
    response = post_assign(client, other, worker.id)
    assert "өөр ажилд томилогдсон" in response.get_data(as_text=True)
    back_to_back = make_job(employer, status=JobStatus.ACCEPTED, start_time=time(13), end_time=time(17))
    assert post_assign(client, back_to_back, worker.id).status_code == 302


def test_pending_job_cannot_be_staffed(client, admin, make_job, employer, make_worker):
    pending = make_job(employer, status=JobStatus.PENDING)
    response = post_assign(client, pending, make_worker("99110001").id)
    assert response.status_code == 400
    assert "Зөвхөн зөвшөөрсөн" in response.get_data(as_text=True)


def test_admin_cancels_assignment(client, admin, job, make_worker, assign):
    assignment = assign(job, make_worker("99110001", full_name="Бат Дорж"))
    response = client.post(f"/admin/assignments/{assignment.id}/cancel", follow_redirects=True)
    html = response.get_data(as_text=True)
    assert "Томилолт цуцлагдлаа." in html
    assert "Цуцлагдсан томилолт (1)" in html
    db.session.expire_all()
    assert assignment.status == AssignmentStatus.CANCELLED
    assert assignment.cancelled_by_id == admin.id
    # Cancelling again is refused, not silently repeated.
    again = client.post(f"/admin/assignments/{assignment.id}/cancel", follow_redirects=True)
    assert "аль хэдийн цуцлагдсан" in again.get_data(as_text=True)


def test_cancel_missing_assignment_is_404(client, admin):
    assert client.post("/admin/assignments/99999/cancel").status_code == 404


def test_cancelling_job_via_admin_status_cancels_assignments(client, admin, job, make_worker, assign):
    assignment = assign(job, make_worker("99110001"))
    client.post(f"/admin/jobs/{job.id}/status", data={"new_status": "cancelled"})
    db.session.expire_all()
    assert assignment.status == AssignmentStatus.CANCELLED
