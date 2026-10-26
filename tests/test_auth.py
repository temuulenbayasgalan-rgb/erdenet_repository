import pytest

from app.extensions import db
from app.models import Role
from tests.conftest import DEFAULT_PASSWORD


def test_login_page_is_mongolian(client):
    response = client.get("/auth/login")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Нэвтрэх" in html
    assert "Утасны дугаар" in html


@pytest.mark.parametrize("role, dashboard", [
    (Role.ADMIN, "/admin/"),
    (Role.WORKER, "/worker/"),
    (Role.EMPLOYER, "/employer/"),
])
def test_successful_login_goes_to_role_dashboard(client, make_user, login, role, dashboard):
    make_user(phone="99110001", role=role)
    response = login("99110001")
    assert response.status_code == 302
    assert response.headers["Location"] == dashboard
    assert client.get(dashboard).status_code == 200


def test_login_accepts_formatted_phone(client, make_user, login):
    make_user(phone="99110001")
    response = login("+976 9911-0001")
    assert response.headers["Location"] == "/worker/"


def test_wrong_password_is_rejected(client, make_user, login):
    make_user(phone="99110001")
    response = login("99110001", password="buruu-nuuts-ug")
    assert response.status_code == 200
    assert "Утасны дугаар эсвэл нууц үг буруу байна." in response.get_data(as_text=True)
    assert client.get("/worker/").status_code == 302  # still logged out


def test_unknown_phone_gets_same_message(client, login):
    response = login("88889999")
    assert response.status_code == 200
    assert "Утасны дугаар эсвэл нууц үг буруу байна." in response.get_data(as_text=True)


def test_empty_login_form_shows_errors(client):
    response = client.post("/auth/login", data={"phone": "", "password": ""})
    assert response.status_code == 200
    assert "Энэ талбарыг бөглөнө үү." in response.get_data(as_text=True)


def test_inactive_user_cannot_log_in(client, make_user, login):
    make_user(phone="99110001", is_active=False)
    response = login("99110001")
    assert response.status_code == 200
    assert "Утасны дугаар эсвэл нууц үг буруу байна." in response.get_data(as_text=True)


def test_deactivating_user_ends_existing_session(client, make_user, login):
    user = make_user(phone="99110001")
    login("99110001")
    assert client.get("/worker/").status_code == 200

    user.is_active = False
    db.session.commit()
    response = client.get("/worker/")
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/auth/login")


def test_logout_ends_session(client, make_user, login):
    make_user(phone="99110001")
    login("99110001")
    response = client.post("/auth/logout")
    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    assert client.get("/worker/").status_code == 302


def test_logout_requires_post(client, make_user, login):
    make_user(phone="99110001")
    login("99110001")
    assert client.get("/auth/logout").status_code == 405
    assert client.get("/worker/").status_code == 200


def test_login_follows_safe_next_url(client, make_user, login):
    make_user(phone="99110001")
    response = login("99110001", next_url="/worker/")
    assert response.headers["Location"] == "/worker/"


@pytest.mark.parametrize("evil", [
    "https://evil.example/",
    "//evil.example/",
    "/\\evil.example/",
    "javascript:alert(1)",
])
def test_login_ignores_external_next_url(client, make_user, login, evil):
    make_user(phone="99110001")
    response = login("99110001", next_url=evil)
    assert response.headers["Location"] == "/worker/"


def test_logged_in_user_visiting_login_is_redirected(client, make_user, login):
    make_user(phone="99110001", role=Role.EMPLOYER)
    login("99110001")
    response = client.get("/auth/login")
    assert response.headers["Location"] == "/employer/"


def test_password_is_stored_hashed(make_user):
    user = make_user()
    assert DEFAULT_PASSWORD not in user.password_hash
    assert user.password_hash.startswith(("scrypt:", "pbkdf2:"))
    assert user.check_password(DEFAULT_PASSWORD)
    assert not user.check_password("buruu")
