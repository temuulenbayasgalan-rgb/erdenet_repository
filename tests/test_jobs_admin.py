from datetime import timedelta

import pytest

from app.extensions import db
from app.models import Job, JobStatus, Role
from app.validation import office_today
from tests.conftest import tomorrow


@pytest.fixture
def admin(make_user, login):
    user = make_user(phone="99000001", role=Role.ADMIN, full_name="Товчооны админ")
    login("99000001")
    return user


@pytest.fixture
def employer(make_user):
    return make_user(phone="99330001", role=Role.EMPLOYER, full_name="Эрдэнэ Трейд ХХК")


def reload(job):
    db.session.expire_all()
    return db.session.get(Job, job.id)


# --- list and filters --------------------------------------------------------------

def test_admin_lists_all_jobs(client, admin, make_user, make_job, employer):
    other = make_user(phone="99330002", role=Role.EMPLOYER, full_name="Өөр компани")
    make_job(employer, title="Эхний ажил")
    make_job(other, title="Хоёр дахь ажил")
    html = client.get("/admin/jobs").get_data(as_text=True)
    assert "Эхний ажил" in html and "Хоёр дахь ажил" in html
    assert "Эрдэнэ Трейд ХХК" in html and "Өөр компани" in html


def test_filter_by_status(client, admin, make_job, employer):
    make_job(employer, title="Хүлээгдэж буй ажил")
    make_job(employer, title="Дууссан ажил", status=JobStatus.COMPLETED)
    html = client.get("/admin/jobs?status=completed").get_data(as_text=True)
    assert "Дууссан ажил" in html
    assert "Хүлээгдэж буй ажил" not in html


def test_filter_by_work_date(client, admin, make_job, employer):
    later = office_today() + timedelta(days=5)
    make_job(employer, title="Маргаашийн ажил")
    make_job(employer, title="Дараагийн ажил", work_date=later)
    html = client.get(f"/admin/jobs?work_date={later.isoformat()}").get_data(as_text=True)
    assert "Дараагийн ажил" in html
    assert "Маргаашийн ажил" not in html


def test_filters_combine(client, admin, make_job, employer):
    make_job(employer, title="Тохирох", status=JobStatus.ACCEPTED)
    make_job(employer, title="Өөр төлөв", status=JobStatus.PENDING)
    make_job(employer, title="Өөр өдөр", status=JobStatus.ACCEPTED,
             work_date=tomorrow() + timedelta(days=1))
    html = client.get(f"/admin/jobs?status=accepted&work_date={tomorrow().isoformat()}").get_data(as_text=True)
    assert "Тохирох" in html
    assert "Өөр төлөв" not in html and "Өөр өдөр" not in html


def test_empty_filters_show_everything(client, admin, make_job, employer):
    make_job(employer, title="Ажил А")
    make_job(employer, title="Ажил Б", status=JobStatus.CANCELLED)
    html = client.get("/admin/jobs?status=&work_date=").get_data(as_text=True)
    assert "Ажил А" in html and "Ажил Б" in html


@pytest.mark.parametrize("query", ["status=deleted", "work_date=not-a-date", "status=%27%20OR%201=1--"])
def test_invalid_filter_shows_warning_and_all_jobs(client, admin, make_job, employer, query):
    make_job(employer, title="Ажил А")
    response = client.get(f"/admin/jobs?{query}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Шүүлтүүрийн утга буруу байна." in html
    assert "Ажил А" in html


def test_dashboard_shows_pending_count(client, admin, make_job, employer):
    make_job(employer)
    make_job(employer)
    make_job(employer, status=JobStatus.ACCEPTED)
    html = client.get("/admin/").get_data(as_text=True)
    assert "Шинэ хүсэлтүүд" in html
    assert '<span class="badge text-bg-light">2</span>' in html


# --- detail ------------------------------------------------------------------------

def test_admin_detail_shows_job_and_employer_contact(client, admin, make_job, employer):
    job = make_job(employer, title="Цэвэрлэгээ", notes="Түлхүүрийг манаачаас авна")
    html = client.get(f"/admin/jobs/{job.id}").get_data(as_text=True)
    assert "Цэвэрлэгээ" in html
    assert "Эрдэнэ Трейд ХХК" in html
    assert "99330001" in html
    assert "Түлхүүрийг манаачаас авна" in html
    assert "120 000 ₮" in html  # 2 x 60 000
    assert "Төлөв өөрчлөх" in html


def test_admin_detail_is_read_only_for_job_fields(client, admin, make_job, employer):
    job = make_job(employer)
    html = client.get(f"/admin/jobs/{job.id}").get_data(as_text=True)
    assert 'name="title"' not in html
    assert 'name="wage_per_worker"' not in html


def test_admin_detail_404(client, admin):
    assert client.get("/admin/jobs/99999").status_code == 404
    assert client.post("/admin/jobs/99999/status", data={"new_status": "accepted"}).status_code == 404


def test_status_form_offers_only_allowed_moves(client, admin, make_job, employer):
    job = make_job(employer)
    html = client.get(f"/admin/jobs/{job.id}").get_data(as_text=True)
    assert 'value="accepted"' in html and 'value="cancelled"' in html
    assert 'value="completed"' not in html and 'value="in_progress"' not in html


@pytest.mark.parametrize("status", [JobStatus.COMPLETED, JobStatus.CANCELLED])
def test_final_status_has_no_form(client, admin, make_job, employer, status):
    job = make_job(employer, status=status)
    html = client.get(f"/admin/jobs/{job.id}").get_data(as_text=True)
    assert "эцсийн төлөвтэй" in html
    assert "Төлөв өөрчлөх" not in html


# --- status changes --------------------------------------------------------------------

def test_full_workflow_is_recorded(client, admin, make_job, employer, make_worker):
    # Milestone 5: completing needs an assigned worker with attendance, so the
    # job is today and one worker is assigned and marked present on the way.
    from app.services.attendance import record_attendance
    from app.services.staffing import assign_worker

    job = make_job(employer, work_date=office_today())
    worker = make_worker("99110001")
    for status in (JobStatus.ACCEPTED, JobStatus.IN_PROGRESS, JobStatus.COMPLETED):
        if status == JobStatus.IN_PROGRESS:
            assignment = assign_worker(job.id, worker.id, 60000, admin.id)
        if status == JobStatus.COMPLETED:
            record_attendance(assignment, "present", admin)
        response = client.post(f"/admin/jobs/{job.id}/status", data={"new_status": status, "note": f"{status} тэмдэглэл"})
        assert response.status_code == 302
        assert reload(job).status == status

    history = [(c.from_status, c.to_status, c.changed_by_id, c.note) for c in reload(job).status_changes]
    assert history == [
        (None, "pending", employer.id, ""),
        ("pending", "accepted", admin.id, "accepted тэмдэглэл"),
        ("accepted", "in_progress", admin.id, "in_progress тэмдэглэл"),
        ("in_progress", "completed", admin.id, "completed тэмдэглэл"),
    ]


def test_status_change_shows_message_and_history(client, admin, make_job, employer):
    job = make_job(employer)
    response = client.post(f"/admin/jobs/{job.id}/status",
                           data={"new_status": "cancelled", "note": "Ажилтан олдсонгүй"},
                           follow_redirects=True)
    html = response.get_data(as_text=True)
    assert "Төлөв «Цуцлагдсан» болж өөрчлөгдлөө." in html
    assert "Ажилтан олдсонгүй" in html
    assert "Товчооны админ" in html


@pytest.mark.parametrize("current, target", [
    (JobStatus.PENDING, JobStatus.IN_PROGRESS),
    (JobStatus.PENDING, JobStatus.COMPLETED),
    (JobStatus.PENDING, JobStatus.PENDING),
    (JobStatus.ACCEPTED, JobStatus.PENDING),
    (JobStatus.ACCEPTED, JobStatus.COMPLETED),
    (JobStatus.IN_PROGRESS, JobStatus.CANCELLED),
    (JobStatus.IN_PROGRESS, JobStatus.ACCEPTED),
    (JobStatus.COMPLETED, JobStatus.CANCELLED),
    (JobStatus.COMPLETED, JobStatus.PENDING),
    (JobStatus.CANCELLED, JobStatus.PENDING),
    (JobStatus.CANCELLED, JobStatus.ACCEPTED),
    (JobStatus.PENDING, "deleted"),
    (JobStatus.PENDING, ""),
])
def test_disallowed_status_change_is_rejected(client, admin, make_job, employer, current, target):
    job = make_job(employer, status=current)
    response = client.post(f"/admin/jobs/{job.id}/status", data={"new_status": target})
    assert response.status_code == 400
    assert "Төлөв өөрчлөгдсөнгүй." in response.get_data(as_text=True)
    assert reload(job).status == current
    assert len(reload(job).status_changes) == 1


def test_status_note_too_long_is_rejected(client, admin, make_job, employer):
    job = make_job(employer)
    response = client.post(f"/admin/jobs/{job.id}/status", data={"new_status": "accepted", "note": "а" * 501})
    assert response.status_code == 400
    assert reload(job).status == JobStatus.PENDING


def test_status_change_does_not_touch_job_fields(client, admin, make_job, employer):
    job = make_job(employer, wage_per_worker=60000)
    client.post(f"/admin/jobs/{job.id}/status", data={
        "new_status": "accepted", "wage_per_worker": "1", "employer_id": str(admin.id), "title": "Хакердсан",
    })
    job = reload(job)
    assert job.status == JobStatus.ACCEPTED
    assert job.wage_per_worker == 60000
    assert job.employer_id == employer.id
    assert job.title == "Тавилга зөөх"


def test_employer_sees_status_set_by_admin(client, admin, make_job, employer, login):
    job = make_job(employer)
    client.post(f"/admin/jobs/{job.id}/status", data={"new_status": "accepted"})
    client.post("/auth/logout")
    login(employer.phone)
    html = client.get(f"/employer/jobs/{job.id}").get_data(as_text=True)
    assert "Зөвшөөрсөн" in html
    assert "Хүсэлт цуцлах" not in html
    assert "товчоотой утсаар холбогдоно" in html
