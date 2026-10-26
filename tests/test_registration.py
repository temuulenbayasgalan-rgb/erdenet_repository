import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Role, User


def form_data(**overrides):
    data = {
        "full_name": "Сараа Болд",
        "phone": "99220002",
        "password": "hamgaalalt-123",
        "confirm_password": "hamgaalalt-123",
    }
    data.update(overrides)
    return data


def user_count():
    return db.session.scalar(select(func.count(User.id)))


@pytest.mark.parametrize("url, role, dashboard", [
    ("/auth/register/worker", Role.WORKER, "/worker/"),
    ("/auth/register/employer", Role.EMPLOYER, "/employer/"),
])
def test_registration_creates_user_and_logs_in(client, url, role, dashboard):
    assert client.get(url).status_code == 200

    response = client.post(url, data=form_data())
    assert response.status_code == 302
    assert response.headers["Location"] == dashboard

    user = db.session.scalar(select(User).filter_by(phone="99220002"))
    assert user.role == role
    assert user.full_name == "Сараа Болд"
    assert user.is_active is True
    assert user.check_password("hamgaalalt-123")
    assert client.get(dashboard).status_code == 200


def test_registration_normalizes_phone(client):
    client.post("/auth/register/worker", data=form_data(phone="+976 9922 0002"))
    assert db.session.scalar(select(User.phone)) == "99220002"


def test_role_field_in_form_is_ignored(client):
    client.post("/auth/register/worker", data=form_data(role=Role.ADMIN))
    assert db.session.scalar(select(User.role)) == Role.WORKER


def test_cannot_register_as_admin(client):
    assert client.post("/auth/register/admin", data=form_data()).status_code == 404
    assert user_count() == 0


def test_duplicate_phone_is_rejected(client, make_user):
    make_user(phone="99220002")
    response = client.post("/auth/register/employer", data=form_data(phone="9922-0002"))
    assert response.status_code == 200
    assert "Энэ утасны дугаар бүртгэлтэй байна." in response.get_data(as_text=True)
    assert user_count() == 1


def test_database_enforces_unique_phone(app, make_user):
    make_user(phone="99220002")
    db.session.add(User(phone="99220002", full_name="Өөр хүн", role=Role.WORKER, password_hash="x"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_database_rejects_unknown_role(app):
    db.session.add(User(phone="99220002", full_name="Хүн", role="superuser", password_hash="x"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


@pytest.mark.parametrize("overrides, message", [
    ({"full_name": ""}, "Энэ талбарыг бөглөнө үү."),
    ({"full_name": "   "}, "Энэ талбарыг бөглөнө үү."),
    ({"full_name": "Б"}, "Нэр 2-100 тэмдэгт байх ёстой."),
    ({"full_name": "Б" * 101}, "Нэр 2-100 тэмдэгт байх ёстой."),
    ({"phone": ""}, "Энэ талбарыг бөглөнө үү."),
    ({"phone": "9922000"}, "Утасны дугаар 8 оронтой тоо байх ёстой."),
    ({"phone": "992200021"}, "Утасны дугаар 8 оронтой тоо байх ёстой."),
    ({"phone": "9922000a"}, "Утасны дугаар 8 оронтой тоо байх ёстой."),
    ({"password": "short", "confirm_password": "short"}, "Нууц үг хамгийн багадаа 8 тэмдэгт байх ёстой."),
    ({"password": "a" * 129, "confirm_password": "a" * 129}, "Нууц үг хамгийн багадаа 8 тэмдэгт байх ёстой."),
    ({"confirm_password": "oor-nuuts-ug"}, "Нууц үг таарахгүй байна."),
])
def test_registration_validation(client, overrides, message):
    response = client.post("/auth/register/worker", data=form_data(**overrides))
    assert response.status_code == 200
    assert message in response.get_data(as_text=True)
    assert user_count() == 0


def test_logged_in_user_cannot_open_registration(client, make_user, login):
    make_user(phone="99110001")
    login("99110001")
    response = client.get("/auth/register/employer")
    assert response.headers["Location"] == "/worker/"
