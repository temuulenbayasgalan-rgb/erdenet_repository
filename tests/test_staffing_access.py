"""Cross-role access to every Milestone 4 route."""
import pytest

from app.extensions import db
from app.models import ApplicationStatus, Assignment, AssignmentStatus, JobApplication, JobStatus, Role


@pytest.fixture
def world(make_user, make_worker, make_listed_job, assign, apply):
    employer = make_user(phone="99330001", role=Role.EMPLOYER, full_name="Компани")
    worker = make_worker("99110001")
    other_worker = make_worker("99110002")
    job = make_listed_job(employer, workers_needed=3)
    application = apply(job, other_worker)
    assignment = assign(job, worker)
    return dict(employer=employer, worker=worker, job=job, application=application, assignment=assignment,
                other_worker=other_worker)


def routes(w):
    return {
        "worker": [
            ("get", "/worker/jobs"),
            ("get", f"/worker/jobs/{w['job'].id}"),
            ("post", f"/worker/jobs/{w['job'].id}/apply"),
            ("get", "/worker/applications"),
            ("post", f"/worker/applications/{w['application'].id}/withdraw"),
            ("get", "/worker/assignments"),
        ],
        "employer": [
            ("get", "/employer/workers"),
            ("get", f"/employer/workers/{w['worker'].id}"),
        ],
        "admin": [
            ("get", f"/admin/jobs/{w['job'].id}/applications"),
            ("get", "/admin/applications"),
            ("post", f"/admin/applications/{w['application'].id}/accept"),
            ("post", f"/admin/applications/{w['application'].id}/reject"),
            ("post", f"/admin/jobs/{w['job'].id}/assignments"),
            ("post", f"/admin/assignments/{w['assignment'].id}/cancel"),
        ],
    }


def unchanged(w):
    db.session.expire_all()
    assert db.session.get(JobApplication, w["application"].id).status == ApplicationStatus.PENDING
    assert db.session.get(Assignment, w["assignment"].id).status == AssignmentStatus.ACTIVE
    assert db.session.scalar(db.select(db.func.count(Assignment.id))) == 1


@pytest.mark.parametrize("area", ["worker", "employer", "admin"])
def test_anonymous_is_sent_to_login(client, world, area):
    for method, url in routes(world)[area]:
        response = getattr(client, method)(url, data={"reason": "Шалтгаан тест"})
        assert response.status_code == 302, url
        assert response.headers["Location"].startswith("/auth/login"), url
    unchanged(world)


@pytest.mark.parametrize("role, forbidden_areas", [
    (Role.WORKER, ["employer", "admin"]),
    (Role.EMPLOYER, ["worker", "admin"]),
    (Role.ADMIN, ["worker", "employer"]),
])
def test_roles_cannot_use_other_areas(client, world, make_user, login, role, forbidden_areas):
    make_user(phone="99440001", role=role, full_name="Шалгагч")
    login("99440001")
    for area in forbidden_areas:
        for method, url in routes(world)[area]:
            data = {"reason": "Шалтгаан тест", "worker_id": str(world["other_worker"].id),
                    "agreed_wage": "60000"}
            response = getattr(client, method)(url, data=data)
            assert response.status_code == 403, url
    unchanged(world)


def test_employer_cannot_see_worker_applications(client, world, login):
    login(world["employer"].phone)
    html = client.get(f"/employer/jobs/{world['job'].id}").get_data(as_text=True)
    assert "Хянагдаж байна" not in html  # the other worker's pending application
    assert world["other_worker"].phone not in html


def test_employer_cannot_open_admin_worker_page(client, world, login):
    login(world["employer"].phone)
    assert client.get(f"/admin/workers/{world['worker'].id}").status_code == 403
    assert client.get("/worker/profile/edit").status_code == 403


def test_worker_id_manipulation(client, world, login):
    """A worker trying other ids only ever reaches their own records."""
    login(world["worker"].phone)
    # The other worker's application id:
    assert client.post(f"/worker/applications/{world['application'].id}/withdraw").status_code == 404
    unchanged(world)
