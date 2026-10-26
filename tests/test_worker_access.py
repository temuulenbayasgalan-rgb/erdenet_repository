"""Who may see or change worker profiles."""
import pytest

from app.extensions import db
from app.models import Role, WorkerProfile


@pytest.fixture
def worker_with_profile(make_user, skills):
    user = make_user(phone="99110001", role=Role.WORKER, full_name="Бат Дорж")
    user.worker_profile = WorkerProfile(description="Нууц биш ч хувийн тайлбар")
    user.worker_profile.skills = [skills["cleaning"]]
    db.session.commit()
    return user


WORKER_PAGES = ["/worker/", "/worker/profile/edit"]


def admin_pages(worker):
    return ["/admin/workers", f"/admin/workers/{worker.id}"]


@pytest.mark.parametrize("url", WORKER_PAGES + ["/admin/workers", "/admin/workers/1"])
def test_anonymous_users_are_sent_to_login(client, url):
    response = client.get(url)
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/auth/login")


def test_anonymous_post_to_edit_is_refused(client, worker_with_profile, skills):
    response = client.post("/worker/profile/edit", data={"skills": [skills["loading"].id], "availability": "unavailable"})
    assert response.status_code == 302
    db.session.expire_all()
    assert worker_with_profile.worker_profile.availability == "available"


def test_employer_cannot_reach_any_worker_information(client, make_user, login, worker_with_profile):
    make_user(phone="99330001", role=Role.EMPLOYER)
    login("99330001")
    for url in WORKER_PAGES + admin_pages(worker_with_profile):
        response = client.get(url)
        assert response.status_code == 403, url
        html = response.get_data(as_text=True)
        assert worker_with_profile.phone not in html
        assert "хувийн тайлбар" not in html


def test_employer_cannot_post_profile_changes(client, make_user, login, skills):
    make_user(phone="99330001", role=Role.EMPLOYER)
    login("99330001")
    response = client.post("/worker/profile/edit", data={"skills": [skills["cleaning"].id], "availability": "available"})
    assert response.status_code == 403
    assert db.session.scalar(db.select(db.func.count(WorkerProfile.id))) == 0


def test_employer_dashboard_does_not_list_workers(client, make_user, login, worker_with_profile):
    make_user(phone="99330001", role=Role.EMPLOYER, full_name="Ажил олгогч ХХК")
    login("99330001")
    html = client.get("/employer/").get_data(as_text=True)
    assert worker_with_profile.phone not in html
    assert worker_with_profile.full_name not in html


def test_worker_cannot_view_admin_worker_pages(client, make_user, login, worker_with_profile):
    make_user(phone="99110002", role=Role.WORKER)
    login("99110002")
    for url in admin_pages(worker_with_profile):
        assert client.get(url).status_code == 403, url


def test_admin_can_list_workers(client, make_user, login, worker_with_profile):
    make_user(phone="99330001", role=Role.EMPLOYER, full_name="Ажил олгогч ХХК")
    make_user(phone="99000001", role=Role.ADMIN, full_name="Админ")
    login("99000001")
    html = client.get("/admin/workers").get_data(as_text=True)
    assert "Бат Дорж" in html
    assert "99110001" in html
    assert "Цэвэрлэгээ" in html
    assert "Ажил хийх боломжтой" in html
    assert "Ажил олгогч ХХК" not in html  # only workers are listed


def test_admin_can_view_worker_profile(client, make_user, login, worker_with_profile):
    make_user(phone="99000001", role=Role.ADMIN)
    login("99000001")
    response = client.get(f"/admin/workers/{worker_with_profile.id}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Бат Дорж" in html
    assert "Цэвэрлэгээ" in html
    assert "Нууц биш ч хувийн тайлбар" in html


def test_admin_sees_worker_without_profile(client, make_user, login):
    worker = make_user(phone="99110005", role=Role.WORKER, full_name="Шинэ ажилтан")
    make_user(phone="99000001", role=Role.ADMIN)
    login("99000001")
    assert "Профайлгүй" in client.get("/admin/workers").get_data(as_text=True)
    assert client.get(f"/admin/workers/{worker.id}").status_code == 200


@pytest.mark.parametrize("role", [Role.ADMIN, Role.EMPLOYER])
def test_admin_worker_detail_404_for_non_workers(client, make_user, login, role):
    target = make_user(phone="99440001", role=role)
    make_user(phone="99000001", role=Role.ADMIN)
    login("99000001")
    assert client.get(f"/admin/workers/{target.id}").status_code == 404
    assert client.get("/admin/workers/99999").status_code == 404


def test_admin_cannot_use_worker_edit_page(client, make_user, login, skills):
    make_user(phone="99000001", role=Role.ADMIN)
    login("99000001")
    assert client.get("/worker/profile/edit").status_code == 403
    response = client.post("/worker/profile/edit", data={"skills": [skills["cleaning"].id], "availability": "available"})
    assert response.status_code == 403


def test_admin_has_no_profile_edit_route(client, make_user, login, worker_with_profile):
    make_user(phone="99000001", role=Role.ADMIN)
    login("99000001")
    assert client.post(f"/admin/workers/{worker_with_profile.id}", data={"availability": "unavailable"}).status_code == 405
