from sqlalchemy import select

from app.extensions import db
from app.models import Role, User

PASSWORD = "admin-nuuts-123"


def run_create_admin(app, phone="99001122", name="Админ Ганаа", input_text=None):
    runner = app.test_cli_runner()
    if input_text is None:
        input_text = f"{PASSWORD}\n{PASSWORD}\n"
    return runner.invoke(
        args=["create-admin", "--phone", phone, "--name", name],
        input=input_text,
    )


def get_user(phone):
    return db.session.scalar(select(User).filter_by(phone=phone))


def test_create_admin(app):
    result = run_create_admin(app, phone="+976 9900-1122")
    assert result.exit_code == 0, result.output
    user = get_user("99001122")
    assert user.role == Role.ADMIN
    assert user.check_password(PASSWORD)
    assert PASSWORD not in result.output


def test_created_admin_can_log_in(app, client, login):
    run_create_admin(app)
    response = login("99001122", password=PASSWORD)
    assert response.headers["Location"] == "/admin/"


def test_create_admin_rejects_mismatched_confirmation(app):
    result = run_create_admin(app, input_text=f"{PASSWORD}\noor-nuuts-ug\n")
    assert result.exit_code != 0
    assert get_user("99001122") is None


def test_create_admin_rejects_short_password(app):
    result = run_create_admin(app, input_text="short\nshort\n")
    assert result.exit_code != 0
    assert "Нууц үг 8-128 тэмдэгт" in result.output
    assert get_user("99001122") is None


def test_create_admin_rejects_invalid_phone(app):
    result = run_create_admin(app, phone="123")
    assert result.exit_code != 0
    assert "8 оронтой" in result.output


def test_create_admin_rejects_duplicate_phone(app, make_user):
    make_user(phone="99001122")
    result = run_create_admin(app)
    assert result.exit_code != 0
    assert "бүртгэлтэй" in result.output
    assert get_user("99001122").role == Role.WORKER
