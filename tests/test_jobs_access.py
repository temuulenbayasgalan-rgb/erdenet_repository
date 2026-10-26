"""Who may see or change job requests."""
import pytest

from app.extensions import db
from app.models import Job, JobStatus, Role


@pytest.fixture
def two_employers(make_user):
    mine = make_user(phone="99330001", role=Role.EMPLOYER, full_name="Миний компани")
    other = make_user(phone="99330002", role=Role.EMPLOYER, full_name="Өрсөлдөгч ХХК")
    return mine, other


def reload(job):
    db.session.expire_all()
    return db.session.get(Job, job.id)


def test_employer_cannot_view_another_employers_job(client, login, make_job, two_employers):
    mine, other = two_employers
    secret = make_job(other, title="Нууц төсөл", location="Нууц хаяг")
    login(mine.phone)
    response = client.get(f"/employer/jobs/{secret.id}")
    # 404, not 403: the page doesn't even confirm the request exists.
    assert response.status_code == 404
    html = response.get_data(as_text=True)
    assert "Нууц төсөл" not in html
    assert "Нууц хаяг" not in html


def test_employer_cannot_cancel_another_employers_job(client, login, make_job, two_employers):
    mine, other = two_employers
    job = make_job(other)
    login(mine.phone)
    assert client.post(f"/employer/jobs/{job.id}/cancel").status_code == 404
    assert reload(job).status == JobStatus.PENDING
    assert len(reload(job).status_changes) == 1


def test_nonexistent_job_is_404(client, login, two_employers):
    login(two_employers[0].phone)
    assert client.get("/employer/jobs/99999").status_code == 404
    assert client.post("/employer/jobs/99999/cancel").status_code == 404


@pytest.mark.parametrize("url", ["/admin/jobs", "/admin/jobs/{id}"])
def test_employer_cannot_open_admin_job_pages(client, login, make_job, two_employers, url):
    mine, _ = two_employers
    job = make_job(mine)
    login(mine.phone)
    assert client.get(url.format(id=job.id)).status_code == 403


@pytest.mark.parametrize("method, url", [
    ("get", "/employer/"),
    ("get", "/employer/jobs/new"),
    ("post", "/employer/jobs/new"),
    ("get", "/employer/jobs/{id}"),
    ("post", "/employer/jobs/{id}/cancel"),
    ("get", "/admin/jobs"),
    ("get", "/admin/jobs/{id}"),
    ("post", "/admin/jobs/{id}/status"),
])
def test_worker_cannot_use_job_pages(client, make_user, login, make_job, two_employers, method, url):
    job = make_job(two_employers[0])
    make_user(phone="99110001", role=Role.WORKER)
    login("99110001")
    response = getattr(client, method)(url.format(id=job.id))
    assert response.status_code == 403
    assert reload(job).status == JobStatus.PENDING


@pytest.mark.parametrize("method, url", [
    ("get", "/employer/jobs/new"),
    ("post", "/employer/jobs/new"),
    ("get", "/employer/jobs/{id}"),
    ("post", "/employer/jobs/{id}/cancel"),
    ("get", "/admin/jobs"),
    ("post", "/admin/jobs/{id}/status"),
])
def test_anonymous_user_is_sent_to_login(client, make_job, two_employers, method, url):
    job = make_job(two_employers[0])
    response = getattr(client, method)(url.format(id=job.id))
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/auth/login")
    assert reload(job).status == JobStatus.PENDING


def test_admin_cannot_use_employer_pages(client, make_user, login, make_job, two_employers):
    job = make_job(two_employers[0])
    make_user(phone="99000001", role=Role.ADMIN)
    login("99000001")
    assert client.get(f"/employer/jobs/{job.id}").status_code == 403
    assert client.post("/employer/jobs/new").status_code == 403


def test_employer_cannot_change_status_via_admin_route(client, login, make_job, two_employers):
    mine, _ = two_employers
    job = make_job(mine)
    login(mine.phone)
    for status in JobStatus.ALL:
        assert client.post(f"/admin/jobs/{job.id}/status", data={"new_status": status}).status_code == 403
    assert reload(job).status == JobStatus.PENDING
