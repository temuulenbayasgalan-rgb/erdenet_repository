from datetime import time, timedelta

import pytest

from app.extensions import db
from app.models import Job, JobStatus, Role
from app.validation import office_today
from tests.conftest import job_form_data, tomorrow

NEW_URL = "/employer/jobs/new"


@pytest.fixture
def employer(make_user, login):
    user = make_user(phone="99330001", role=Role.EMPLOYER, full_name="Эрдэнэ Трейд ХХК")
    login("99330001")
    return user


def job_count():
    return db.session.scalar(db.select(db.func.count(Job.id)))


def only_job():
    db.session.expire_all()
    return db.session.scalars(db.select(Job)).one()


# --- creating a request ---------------------------------------------------------

def test_new_job_form_is_mongolian(client, employer):
    html = client.get(NEW_URL).get_data(as_text=True)
    assert "Ажилтан захиалах" in html
    for label in ("Ажлын төрөл", "Ажлын байршил", "Ажлын өдөр", "Эхлэх цаг",
                  "Хэдэн ажилтан хэрэгтэй", "Нэг ажилтанд олгох цалин (₮)"):
        assert label in html
    assert 'type="date"' in html
    assert 'type="time"' in html


def test_employer_creates_job(client, employer):
    response = client.post(NEW_URL, data=job_form_data())
    job = only_job()
    assert response.status_code == 302
    assert response.headers["Location"] == f"/employer/jobs/{job.id}"

    assert job.employer_id == employer.id
    assert job.title == "Ачаа буулгах"
    assert job.work_date == tomorrow()
    assert job.start_time == time(9, 0)
    assert job.end_time == time(17, 0)
    assert job.workers_needed == 3
    assert job.wage_per_worker == 80000
    assert isinstance(job.wage_per_worker, int)
    assert job.status == JobStatus.PENDING
    assert job.created_at is not None and job.updated_at is not None
    # The creation is the first entry in the status history.
    assert [(c.from_status, c.to_status, c.changed_by_id) for c in job.status_changes] == \
        [(None, JobStatus.PENDING, employer.id)]


def test_success_message_and_detail_page(client, employer):
    response = client.post(NEW_URL, data=job_form_data(), follow_redirects=True)
    html = response.get_data(as_text=True)
    assert "Хүсэлт амжилттай илгээгдлээ." in html
    assert "Ачаа буулгах" in html
    assert "80 000 ₮" in html
    assert "240 000 ₮" in html  # total: 3 x 80 000
    assert "Хүлээгдэж буй" in html
    assert "09:00 – 17:00" in html


@pytest.mark.parametrize("typed, stored", [
    ("80000", 80000),
    ("80 000", 80000),
    ("80,000", 80000),
    ("80 000₮", 80000),
    (" 1000 ", 1000),
    ("5000000", 5_000_000),
])
def test_wage_input_formats(client, employer, typed, stored):
    client.post(NEW_URL, data=job_form_data(wage_per_worker=typed))
    assert only_job().wage_per_worker == stored


def test_optional_notes_and_trimming(client, employer):
    client.post(NEW_URL, data=job_form_data(title="  Цэвэрлэгээ  ", notes="   "))
    job = only_job()
    assert job.title == "Цэвэрлэгээ"
    assert job.notes == ""


def test_today_is_accepted(client, employer):
    client.post(NEW_URL, data=job_form_data(work_date=office_today().isoformat()))
    assert only_job().work_date == office_today()


def test_time_with_seconds_is_accepted(client, employer):
    client.post(NEW_URL, data=job_form_data(start_time="08:30:00", end_time="12:00:00"))
    assert only_job().start_time == time(8, 30)


def test_crafted_fields_are_ignored(client, employer, make_user):
    other = make_user(phone="99330002", role=Role.EMPLOYER, full_name="Өөр компани")
    client.post(NEW_URL, data=job_form_data(
        status=JobStatus.COMPLETED, employer_id=str(other.id), id="999",
        created_at="2000-01-01", total_wage="1",
    ))
    job = only_job()
    assert job.status == JobStatus.PENDING
    assert job.employer_id == employer.id
    assert job.id != 999


# --- validation --------------------------------------------------------------------

def _days(n):
    return (office_today() + timedelta(days=n)).isoformat()


@pytest.mark.parametrize("field, value, message", [
    ("title", "", "Энэ талбарыг бөглөнө үү."),
    ("title", "   ", "Энэ талбарыг бөглөнө үү."),
    ("title", "аа", "3-100 тэмдэгт байх ёстой."),
    ("title", "а" * 101, "3-100 тэмдэгт байх ёстой."),
    ("description", "", "Энэ талбарыг бөглөнө үү."),
    ("description", "богино", "10-1000 тэмдэгт байх ёстой."),
    ("description", "а" * 1001, "10-1000 тэмдэгт байх ёстой."),
    ("location", "", "Энэ талбарыг бөглөнө үү."),
    ("location", "а" * 201, "3-200 тэмдэгт байх ёстой."),
    ("work_date", "", "Энэ талбарыг бөглөнө үү."),
    ("work_date", "маргааш", "Огноог зөв оруулна уу."),
    ("work_date", "2026-02-30", "Огноог зөв оруулна уу."),
    ("work_date", _days(-1), "Өнгөрсөн өдөр сонгох боломжгүй."),
    ("work_date", _days(91), "Ажлын өдөр 90 хоногийн дотор байх ёстой."),
    ("start_time", "", "Энэ талбарыг бөглөнө үү."),
    ("start_time", "25:00", "Цагийг зөв оруулна уу."),
    ("end_time", "өглөө", "Цагийг зөв оруулна уу."),
    ("end_time", "09:00", "Дуусах цаг эхлэх цагаас хойш байх ёстой."),
    ("end_time", "08:00", "Дуусах цаг эхлэх цагаас хойш байх ёстой."),
    ("workers_needed", "", "Энэ талбарыг бөглөнө үү."),
    ("workers_needed", "0", "Ажилтны тоо 1-50 байх ёстой."),
    ("workers_needed", "-2", "Ажилтны тоо 1-50 байх ёстой."),
    ("workers_needed", "51", "Ажилтны тоо 1-50 байх ёстой."),
    ("workers_needed", "2.5", "Бүхэл тоо оруулна уу."),
    ("workers_needed", "гурав", "Бүхэл тоо оруулна уу."),
    ("wage_per_worker", "", "Энэ талбарыг бөглөнө үү."),
    ("wage_per_worker", "80000.50", "Мөнгөн дүнг бүхэл төгрөгөөр оруулна уу."),
    ("wage_per_worker", "-80000", "Мөнгөн дүнг бүхэл төгрөгөөр оруулна уу."),
    ("wage_per_worker", "наян мянга", "Мөнгөн дүнг бүхэл төгрөгөөр оруулна уу."),
    ("wage_per_worker", "1e5", "Мөнгөн дүнг бүхэл төгрөгөөр оруулна уу."),
    ("wage_per_worker", "²", "Мөнгөн дүнг бүхэл төгрөгөөр оруулна уу."),
    ("wage_per_worker", "999", "Цалин 1 000-5 000 000 ₮ байх ёстой."),
    ("wage_per_worker", "5000001", "Цалин 1 000-5 000 000 ₮ байх ёстой."),
    ("wage_per_worker", "9" * 30, "Цалин 1 000-5 000 000 ₮ байх ёстой."),
    ("notes", "а" * 501, "500 тэмдэгтээс ихгүй байна."),
])
def test_invalid_job_is_rejected(client, employer, field, value, message):
    response = client.post(NEW_URL, data=job_form_data(**{field: value}))
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert message in html
    assert "Хүсэлт илгээгдсэнгүй." in html
    assert job_count() == 0


def test_unparseable_value_shows_only_one_message(client, employer):
    html = client.post(NEW_URL, data=job_form_data(wage_per_worker="abc")).get_data(as_text=True)
    assert "Мөнгөн дүнг бүхэл төгрөгөөр оруулна уу." in html
    assert "Цалин 1 000-5 000 000" not in html


def test_missing_fields_are_rejected(client, employer):
    response = client.post(NEW_URL, data={"title": "Ачаа буулгах"})
    assert response.status_code == 200
    assert job_count() == 0


def test_invalid_form_keeps_typed_values(client, employer):
    html = client.post(NEW_URL, data=job_form_data(workers_needed="0")).get_data(as_text=True)
    assert 'value="Ачаа буулгах"' in html
    assert 'value="80000"' in html


# --- dashboard and detail ------------------------------------------------------------

def test_dashboard_lists_only_own_jobs(client, employer, make_user, make_job):
    other = make_user(phone="99330002", role=Role.EMPLOYER, full_name="Өөр компани")
    make_job(employer, title="Миний ажил")
    make_job(other, title="Бусдын ажил")
    html = client.get("/employer/").get_data(as_text=True)
    assert "Миний ажил" in html
    assert "Бусдын ажил" not in html


def test_dashboard_empty_state(client, employer):
    html = client.get("/employer/").get_data(as_text=True)
    assert "хүсэлт илгээгээгүй" in html
    assert NEW_URL in html


def test_detail_shows_history_and_cancel_button(client, employer, make_job):
    job = make_job(employer, notes="Хоол өгнө")
    html = client.get(f"/employer/jobs/{job.id}").get_data(as_text=True)
    assert "Хоол өгнө" in html
    assert "Төлөвийн түүх" in html
    assert "Хүсэлт цуцлах" in html


def test_detail_escapes_html(client, employer, make_job):
    job = make_job(employer, description="<script>alert(1)</script> тайлбар")
    html = client.get(f"/employer/jobs/{job.id}").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# --- cancelling -----------------------------------------------------------------------

def test_employer_cancels_pending_job(client, employer, make_job):
    job = make_job(employer)
    response = client.post(f"/employer/jobs/{job.id}/cancel", follow_redirects=True)
    assert "Хүсэлт цуцлагдлаа." in response.get_data(as_text=True)
    job = only_job()
    assert job.status == JobStatus.CANCELLED
    last = job.status_changes[-1]
    assert (last.from_status, last.to_status, last.changed_by_id) == \
        (JobStatus.PENDING, JobStatus.CANCELLED, employer.id)


@pytest.mark.parametrize("status", [
    JobStatus.ACCEPTED, JobStatus.IN_PROGRESS, JobStatus.COMPLETED, JobStatus.CANCELLED,
])
def test_employer_cannot_cancel_after_pending(client, employer, make_job, status):
    job = make_job(employer, status=status)
    html = client.get(f"/employer/jobs/{job.id}").get_data(as_text=True)
    assert "Хүсэлт цуцлах" not in html

    response = client.post(f"/employer/jobs/{job.id}/cancel", follow_redirects=True)
    assert "цуцлах боломжгүй" in response.get_data(as_text=True)
    assert only_job().status == status
    assert len(only_job().status_changes) == 1


def test_cancel_requires_post(client, employer, make_job):
    job = make_job(employer)
    assert client.get(f"/employer/jobs/{job.id}/cancel").status_code == 405
    assert only_job().status == JobStatus.PENDING


def test_employer_has_no_status_change_route(client, employer, make_job):
    job = make_job(employer)
    response = client.post(f"/admin/jobs/{job.id}/status", data={"new_status": JobStatus.COMPLETED})
    assert response.status_code == 403
    assert client.post(f"/employer/jobs/{job.id}", data={"status": "completed"}).status_code == 405
    assert only_job().status == JobStatus.PENDING
