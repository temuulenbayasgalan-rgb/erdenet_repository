"""Employer company name: what workers see, and what stays private."""
import pytest

from app.extensions import db
from app.models import EMPLOYER_FALLBACK_NAME, JobStatus, Role, User
from app.validation import office_today


def register_employer(client, **extra):
    return client.post("/auth/register/employer", data={
        "full_name": "Ганбат Дорж", "phone": "99330005",
        "password": "employer-123", "confirm_password": "employer-123", **extra,
    })


def employer_by_phone(phone="99330005"):
    db.session.expire_all()
    return db.session.scalar(db.select(User).filter_by(phone=phone))


def test_registration_form_offers_company_name(client):
    html = client.get("/auth/register/employer").get_data(as_text=True)
    assert "Байгууллагын нэр (заавал биш)" in html
    assert "Байгууллагын нэр" not in client.get("/auth/register/worker").get_data(as_text=True)


def test_registration_saves_company_name(client):
    register_employer(client, company_name="  Эрдэнэ Трейд ХХК  ")
    assert employer_by_phone().company_name == "Эрдэнэ Трейд ХХК"


def test_company_name_is_optional(client):
    register_employer(client)
    user = employer_by_phone()
    assert user.company_name is None
    assert user.employer_display_name == EMPLOYER_FALLBACK_NAME


def test_company_name_too_long_is_rejected(client):
    response = register_employer(client, company_name="Б" * 101)
    assert response.status_code == 200
    assert "2-100 тэмдэгт байх ёстой." in response.get_data(as_text=True)
    assert employer_by_phone() is None


def test_worker_registration_ignores_company_name(client):
    client.post("/auth/register/worker", data={
        "full_name": "Бат Дорж", "phone": "99110005", "company_name": "Хуурамч ХХК",
        "password": "worker-1234", "confirm_password": "worker-1234",
    })
    assert employer_by_phone("99110005").company_name is None


def test_employer_edits_company_name(client, make_user, login):
    make_user(phone="99330001", role=Role.EMPLOYER)
    login("99330001")
    assert "Хувь ажил олгогч" in client.get("/employer/").get_data(as_text=True)
    response = client.post("/employer/profile", data={"company_name": "Шинэ Нэр ХХК"}, follow_redirects=True)
    html = response.get_data(as_text=True)
    assert "Байгууллагын нэр хадгалагдлаа." in html
    assert "Шинэ Нэр ХХК" in html
    client.post("/employer/profile", data={"company_name": ""})
    assert employer_by_phone("99330001").company_name is None


@pytest.mark.parametrize("role", [Role.WORKER, Role.ADMIN])
def test_only_employers_edit_company_name(client, make_user, login, role):
    make_user(phone="99440001", role=role)
    login("99440001")
    assert client.post("/employer/profile", data={"company_name": "X ХХК"}).status_code == 403
    assert employer_by_phone("99440001").company_name is None


# --- what workers see ---------------------------------------------------------------------------

@pytest.fixture
def setup(make_user, make_job, make_worker, assign, login):
    named = make_user(phone="99330001", role=Role.EMPLOYER, full_name="Ганбат Дорж")
    named.company_name = "Эрдэнэ Трейд ХХК"
    private = make_user(phone="99330002", role=Role.EMPLOYER, full_name="Хувь Хүн Нэртэй")
    db.session.commit()
    open_named = make_job(named, status=JobStatus.ACCEPTED, title="Нэртэй ажил")
    open_private = make_job(private, status=JobStatus.ACCEPTED, title="Хувь хүний ажил",
                            work_date=office_today())
    worker = make_worker("99110001")
    assign(open_private, worker)
    login(worker.phone)
    return dict(named=named, private=private, open_named=open_named, open_private=open_private)


def test_worker_sees_company_name_or_fallback(client, setup):
    jobs_html = client.get("/worker/jobs").get_data(as_text=True)
    assert "Эрдэнэ Трейд ХХК" in jobs_html
    detail = client.get(f"/worker/jobs/{setup['open_named'].id}").get_data(as_text=True)
    assert "Эрдэнэ Трейд ХХК" in detail
    assignments = client.get("/worker/assignments").get_data(as_text=True)
    assert EMPLOYER_FALLBACK_NAME in assignments


def test_worker_never_sees_employer_personal_data(client, setup):
    pages = [
        client.get("/worker/jobs").get_data(as_text=True),
        client.get(f"/worker/jobs/{setup['open_named'].id}").get_data(as_text=True),
        client.get(f"/worker/jobs/{setup['open_private'].id}").get_data(as_text=True),
        client.get("/worker/assignments").get_data(as_text=True),
        client.get("/notifications/").get_data(as_text=True),
    ]
    for html in pages:
        for employer in (setup["named"], setup["private"]):
            assert employer.phone not in html
            assert employer.full_name not in html


def test_employer_pages_never_show_worker_phone(client, make_user, make_job, make_worker, assign, login, admin_user):
    from app.services.attendance import record_attendance
    employer = make_user(phone="99330001", role=Role.EMPLOYER)
    job = make_job(employer, status=JobStatus.ACCEPTED, work_date=office_today())
    worker = make_worker("99110001", full_name="Нууц Нэртэй")
    assignment = assign(job, worker)
    record_attendance(assignment, "present", admin_user)
    login(employer.phone)
    for url in ("/employer/", f"/employer/jobs/{job.id}", "/employer/workers", "/notifications/"):
        html = client.get(url).get_data(as_text=True)
        assert worker.phone not in html, url
        assert "Нууц Нэртэй" not in html, url
