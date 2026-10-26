"""In-app notifications: creation, de-duplication, privacy and ownership."""
import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import AttendanceStatus, JobStatus, Notification, Role
from app.services import ServiceError
from app.services.attendance import record_attendance
from app.services.jobs import cancel_job_by_employer, change_job_status
from app.services.notifications import notify
from app.services.staffing import StaffingError, cancel_assignment
from app.validation import office_today


@pytest.fixture
def employer(make_user):
    return make_user(phone="99330001", role=Role.EMPLOYER, full_name="Дорж Ганбат")


@pytest.fixture
def job(make_job, employer):
    return make_job(employer, status=JobStatus.ACCEPTED, work_date=office_today(),
                    title="Тавилга зөөх", workers_needed=3)


def notes_for(user):
    db.session.expire_all()
    return db.session.scalars(
        db.select(Notification).filter_by(recipient_id=user.id).order_by(Notification.id)
    ).all()


def all_notes():
    return db.session.scalars(db.select(Notification).order_by(Notification.id)).all()


# --- each event --------------------------------------------------------------------------

def test_worker_notified_of_assignment(job, make_worker, assign):
    worker = make_worker("99110001")
    assign(job, worker, agreed_wage=65000)
    [note] = notes_for(worker)
    assert note.title == "Шинэ ажилд томилогдлоо"
    assert "Тавилга зөөх" in note.message and "65 000 ₮" in note.message
    assert note.link == "/worker/assignments"
    assert not note.is_read


def test_worker_notified_of_cancelled_assignment(job, make_worker, assign, admin_user):
    worker = make_worker("99110001")
    cancel_assignment(assign(job, worker), admin_user)
    titles = [n.title for n in notes_for(worker)]
    assert titles == ["Шинэ ажилд томилогдлоо", "Томилолт цуцлагдлаа"]


def test_employer_notified_of_acceptance(make_job, employer, admin_user):
    pending = make_job(employer, title="Цэвэрлэгээ")
    change_job_status(pending, JobStatus.ACCEPTED, admin_user)
    [note] = notes_for(employer)
    assert note.title == "Хүсэлт зөвшөөрөгдлөө"
    assert "Цэвэрлэгээ" in note.message
    assert note.link == f"/employer/jobs/{pending.id}"


def test_employer_and_workers_notified_when_office_cancels(job, employer, make_worker, assign, admin_user):
    first, second = make_worker("99110001"), make_worker("99110002")
    assign(job, first)
    assign(job, second)
    change_job_status(db.session.get(type(job), job.id), JobStatus.CANCELLED, admin_user)
    assert [n.title for n in notes_for(employer)] == ["Хүсэлт цуцлагдлаа"]
    for worker in (first, second):
        assert [n.title for n in notes_for(worker)] == ["Шинэ ажилд томилогдлоо", "Томилолт цуцлагдлаа"]


def test_employer_not_notified_of_own_cancellation(make_job, employer):
    pending = make_job(employer)
    cancel_job_by_employer(pending, employer)
    assert notes_for(employer) == []


def test_employer_notified_of_completion(job, employer, make_worker, assign, admin_user):
    assignment = assign(job, make_worker("99110001"))
    change_job_status(job, JobStatus.IN_PROGRESS, admin_user)
    record_attendance(assignment, AttendanceStatus.PRESENT, admin_user)
    change_job_status(db.session.get(type(job), job.id), JobStatus.COMPLETED, admin_user)
    [note] = notes_for(employer)
    assert note.title == "Ажил дууслаа"


def test_in_progress_and_attendance_send_nothing(job, employer, make_worker, assign, admin_user):
    worker = make_worker("99110001")
    assignment = assign(job, worker)
    change_job_status(job, JobStatus.IN_PROGRESS, admin_user)
    record_attendance(assignment, AttendanceStatus.PRESENT, admin_user)
    assert notes_for(employer) == []
    assert len(notes_for(worker)) == 1  # only the assignment


# --- only after success, never duplicated ---------------------------------------------------

def test_failed_operations_create_no_notification(job, employer, make_worker, assign, admin_user):
    worker = make_worker("99110001")
    assign(job, worker)
    with pytest.raises(StaffingError):
        assign(job, worker)  # duplicate assignment
    with pytest.raises(ServiceError):
        change_job_status(db.session.get(type(job), job.id), JobStatus.COMPLETED, admin_user)  # not allowed
    with pytest.raises(ServiceError):
        change_job_status(db.session.get(type(job), job.id), JobStatus.ACCEPTED, admin_user)  # already accepted
    assert len(notes_for(worker)) == 1
    assert notes_for(employer) == []


def test_retried_requests_do_not_duplicate(client, login, make_job, employer, admin_user, make_worker):
    login(admin_user.phone)
    pending = make_job(employer, work_date=office_today())
    worker = make_worker("99110001")
    for _ in range(3):
        client.post(f"/admin/jobs/{pending.id}/status", data={"new_status": "accepted"})
        client.post(f"/admin/jobs/{pending.id}/assignments", data={"worker_id": worker.id, "agreed_wage": "60000"})
    assert [n.title for n in notes_for(employer)] == ["Хүсэлт зөвшөөрөгдлөө"]
    assert [n.title for n in notes_for(worker)] == ["Шинэ ажилд томилогдлоо"]


def test_notify_same_event_twice_is_ignored(employer):
    assert notify(employer, "test:1", "Гарчиг", "Мессеж") is not None
    assert notify(employer, "test:1", "Гарчиг", "Мессеж") is None  # still in the session
    db.session.commit()
    assert notify(employer, "test:1", "Гарчиг", "Мессеж") is None  # already stored
    db.session.commit()
    assert len(notes_for(employer)) == 1


def test_database_blocks_duplicate_event_key(employer):
    db.session.add_all([
        Notification(recipient=employer, event_key="x", title="a", message="b"),
        Notification(recipient=employer, event_key="x", title="a", message="b"),
    ])
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_notification_failure_rolls_back_the_assignment(job, make_worker, assign, monkeypatch):
    """Recovery strategy: the business change and its notification share one transaction."""
    from app.models import Assignment
    from app.services import notifications

    def broken(*args, **kwargs):
        raise RuntimeError("notification store unavailable")

    monkeypatch.setattr(notifications, "worker_assigned", broken)
    with pytest.raises(RuntimeError):
        assign(job, make_worker("99110001"))
    db.session.rollback()
    assert db.session.scalar(db.select(db.func.count(Assignment.id))) == 0
    assert all_notes() == []


def test_external_links_are_refused(employer):
    for bad in ("https://evil.example/", "//evil.example/", "javascript:alert(1)"):
        with pytest.raises(ValueError):
            notify(employer, f"bad:{bad}", "a", "b", link=bad)


# --- privacy ------------------------------------------------------------------------------------

def test_worker_notifications_contain_no_other_workers_or_phones(job, employer, make_worker, assign, admin_user):
    first = make_worker("99110001", full_name="Анхны Ажилтан")
    second = make_worker("99110002", full_name="Хоёр Дахь")
    assign(job, first)
    assign(job, second)
    change_job_status(db.session.get(type(job), job.id), JobStatus.CANCELLED, admin_user)
    for note in notes_for(first):
        text = note.title + note.message
        assert "Хоёр Дахь" not in text and "99110002" not in text
        assert employer.phone not in text and employer.full_name not in text


def test_employer_notifications_contain_no_worker_details(job, employer, make_worker, assign, admin_user):
    worker = make_worker("99110001", full_name="Нууц Ажилтан")
    assignment = assign(job, worker)
    change_job_status(job, JobStatus.IN_PROGRESS, admin_user)
    record_attendance(assignment, AttendanceStatus.PRESENT, admin_user)
    change_job_status(db.session.get(type(job), job.id), JobStatus.COMPLETED, admin_user)
    for note in notes_for(employer):
        assert "Нууц Ажилтан" not in note.message and "99110001" not in note.message


def test_employer_only_gets_own_job_notifications(make_job, employer, make_user, admin_user):
    other = make_user(phone="99330002", role=Role.EMPLOYER)
    theirs = make_job(other, title="Тэдний ажил")
    change_job_status(theirs, JobStatus.ACCEPTED, admin_user)
    assert notes_for(employer) == []
    assert len(notes_for(other)) == 1


# --- pages ----------------------------------------------------------------------------------------

@pytest.fixture
def worker_with_notes(job, make_worker, assign):
    worker = make_worker("99110001")
    assign(job, worker)
    return worker


def test_unread_count_in_navigation(client, login, worker_with_notes):
    login(worker_with_notes.phone)
    html = client.get("/worker/").get_data(as_text=True)
    assert 'aria-label="уншаагүй">1</span>' in html


def test_notification_list_shows_own(client, login, worker_with_notes, make_worker, job, assign):
    other = make_worker("99110002", full_name="Өөр Ажилтан")
    notify(other, "secret:1", "Бусдын мэдэгдэл", "Нууц агуулга")
    db.session.commit()
    login(worker_with_notes.phone)
    html = client.get("/notifications/").get_data(as_text=True)
    assert "Шинэ ажилд томилогдлоо" in html
    assert "Бусдын мэдэгдэл" not in html


def test_open_marks_read_and_follows_link(client, login, worker_with_notes):
    login(worker_with_notes.phone)
    [note] = notes_for(worker_with_notes)
    response = client.post(f"/notifications/{note.id}/open")
    assert response.headers["Location"] == "/worker/assignments"
    [note] = notes_for(worker_with_notes)
    assert note.is_read and note.read_at is not None
    assert 'aria-label="уншаагүй"' not in client.get("/worker/").get_data(as_text=True)


def test_mark_all_read(client, login, worker_with_notes):
    notify(worker_with_notes, "extra:1", "Нэмэлт", "Мессеж")
    db.session.commit()
    login(worker_with_notes.phone)
    client.post("/notifications/read-all")
    assert all(n.is_read for n in notes_for(worker_with_notes))


def test_cannot_open_someone_elses_notification(client, login, worker_with_notes, make_worker):
    intruder = make_worker("99110002")
    login(intruder.phone)
    [note] = notes_for(worker_with_notes)
    assert client.post(f"/notifications/{note.id}/open").status_code == 404
    client.post("/notifications/read-all")  # only marks the intruder's own
    [note] = notes_for(worker_with_notes)
    assert not note.is_read


def test_open_requires_post(client, login, worker_with_notes):
    login(worker_with_notes.phone)
    [note] = notes_for(worker_with_notes)
    assert client.get(f"/notifications/{note.id}/open").status_code == 405
    assert not notes_for(worker_with_notes)[0].is_read


def test_notifications_require_login(client, worker_with_notes):
    [note] = notes_for(worker_with_notes)
    for method, url in [("get", "/notifications/"), ("post", f"/notifications/{note.id}/open"),
                        ("post", "/notifications/read-all")]:
        response = getattr(client, method)(url)
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/auth/login")
    assert not notes_for(worker_with_notes)[0].is_read


def test_missing_notification_is_404(client, login, worker_with_notes):
    login(worker_with_notes.phone)
    assert client.post("/notifications/99999/open").status_code == 404
