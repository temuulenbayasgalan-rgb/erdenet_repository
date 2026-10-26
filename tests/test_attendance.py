"""Office-recorded attendance."""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import Assignment, AttendanceChange, AttendanceStatus, JobStatus, Role
from app.services import ServiceError
from app.services.attendance import record_attendance
from app.services.staffing import cancel_assignment
from app.validation import office_today


@pytest.fixture
def employer(make_user):
    return make_user(phone="99330001", role=Role.EMPLOYER, full_name="Компани")


@pytest.fixture
def todays_job(make_job, employer):
    return make_job(employer, status=JobStatus.ACCEPTED, work_date=office_today(), workers_needed=3)


@pytest.fixture
def assignment(todays_job, make_worker, assign):
    return assign(todays_job, make_worker("99110001", full_name="Бат Дорж"))


def reload(assignment):
    db.session.expire_all()
    return db.session.get(Assignment, assignment.id)


# --- service rules ------------------------------------------------------------------

def test_new_assignment_starts_not_checked(assignment):
    assert assignment.attendance == AttendanceStatus.NOT_CHECKED
    assert assignment.attendance_changes == []


@pytest.mark.parametrize("status", AttendanceStatus.RECORDABLE)
def test_each_recordable_state(assignment, admin_user, status):
    record_attendance(assignment, status, admin_user)
    a = reload(assignment)
    assert a.attendance == status
    change = a.last_attendance_change
    assert (change.from_status, change.to_status, change.changed_by_id) == \
        (AttendanceStatus.NOT_CHECKED, status, admin_user.id)
    assert change.created_at is not None


def test_corrections_keep_full_history(assignment, admin_user, make_user):
    second_admin = make_user(phone="99000002", role=Role.ADMIN, full_name="Хоёр дахь админ")
    record_attendance(assignment, AttendanceStatus.ABSENT, admin_user)
    record_attendance(reload(assignment), AttendanceStatus.PRESENT, second_admin)
    record_attendance(reload(assignment), AttendanceStatus.EXCUSED, admin_user)
    history = [(c.from_status, c.to_status, c.changed_by_id) for c in reload(assignment).attendance_changes]
    assert history == [
        ("not_checked", "absent", admin_user.id),
        ("absent", "present", second_admin.id),
        ("present", "excused", admin_user.id),
    ]


@pytest.mark.parametrize("status", ["not_checked", "late", "", "PRESENT", None])
def test_invalid_states_are_rejected(assignment, admin_user, status):
    with pytest.raises(ServiceError, match="Ирцийн төлөв буруу"):
        record_attendance(assignment, status, admin_user)
    assert reload(assignment).attendance == AttendanceStatus.NOT_CHECKED


def test_cannot_go_back_to_not_checked(assignment, admin_user):
    record_attendance(assignment, AttendanceStatus.PRESENT, admin_user)
    with pytest.raises(ServiceError):
        record_attendance(reload(assignment), AttendanceStatus.NOT_CHECKED, admin_user)
    assert reload(assignment).attendance == AttendanceStatus.PRESENT


def test_same_state_twice_is_not_a_new_change(assignment, admin_user):
    record_attendance(assignment, AttendanceStatus.PRESENT, admin_user)
    with pytest.raises(ServiceError, match="аль хэдийн"):
        record_attendance(reload(assignment), AttendanceStatus.PRESENT, admin_user)
    assert len(reload(assignment).attendance_changes) == 1


@pytest.mark.parametrize("role", [Role.WORKER, Role.EMPLOYER])
def test_non_admin_cannot_record(assignment, make_user, role):
    user = make_user(phone="99440001", role=role)
    with pytest.raises(ServiceError, match="Зөвхөн админ"):
        record_attendance(assignment, AttendanceStatus.PRESENT, user)
    assert reload(assignment).attendance == AttendanceStatus.NOT_CHECKED


def test_cancelled_assignment_is_locked(assignment, admin_user):
    cancel_assignment(assignment, admin_user)
    with pytest.raises(ServiceError, match="Цуцлагдсан томилолт"):
        record_attendance(reload(assignment), AttendanceStatus.PRESENT, admin_user)
    assert db.session.scalar(db.select(db.func.count(AttendanceChange.id))) == 0


def test_future_job_attendance_is_refused(make_job, employer, make_worker, assign, admin_user):
    future = make_job(employer, status=JobStatus.ACCEPTED, work_date=office_today() + timedelta(days=2))
    assignment = assign(future, make_worker("99110002"))
    with pytest.raises(ServiceError, match="Ажлын өдөр болоогүй"):
        record_attendance(assignment, AttendanceStatus.PRESENT, admin_user)


def test_past_in_progress_job_can_still_be_recorded(make_job, employer, make_worker, assign, admin_user):
    """The office may fill in attendance the day after."""
    job = make_job(employer, status=JobStatus.ACCEPTED, work_date=office_today())
    assignment = assign(job, make_worker("99110002"))
    job.work_date = office_today() - timedelta(days=1)
    job.status = JobStatus.IN_PROGRESS
    db.session.commit()
    record_attendance(assignment, AttendanceStatus.PRESENT, admin_user)
    assert reload(assignment).attendance == AttendanceStatus.PRESENT


@pytest.mark.parametrize("status", [JobStatus.COMPLETED, JobStatus.CANCELLED])
def test_final_job_attendance_is_locked(assignment, todays_job, admin_user, status):
    if status == JobStatus.COMPLETED:
        record_attendance(assignment, AttendanceStatus.PRESENT, admin_user)
    todays_job = db.session.get(type(todays_job), todays_job.id)
    todays_job.status = status
    db.session.commit()
    with pytest.raises(ServiceError, match="Дууссан эсвэл цуцлагдсан"):
        record_attendance(reload(assignment), AttendanceStatus.ABSENT, admin_user)


def test_database_rejects_invalid_history_row(assignment, admin_user):
    db.session.add(AttendanceChange(assignment_id=assignment.id, from_status="present",
                                    to_status="not_checked", changed_by_id=admin_user.id))
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


# --- admin page -------------------------------------------------------------------------

@pytest.fixture
def admin(admin_user, login):
    login(admin_user.phone)
    return admin_user


def test_attendance_page_lists_assigned_workers(client, admin, todays_job, assignment, make_worker, assign):
    cancelled = assign(todays_job, make_worker("99110003", full_name="Цуцлагдсан Хүн"))
    cancel_assignment(cancelled, admin)
    html = client.get(f"/admin/jobs/{todays_job.id}/attendance").get_data(as_text=True)
    assert "Бат Дорж" in html
    assert "Цуцлагдсан Хүн" not in html
    for label in ("Ирсэн", "Ирээгүй", "Чөлөөтэй", "Шалгаагүй"):
        assert label in html


def test_admin_records_attendance_through_page(client, admin, todays_job, assignment):
    response = client.post(f"/admin/assignments/{assignment.id}/attendance",
                           data={"attendance": "present"}, follow_redirects=True)
    html = response.get_data(as_text=True)
    assert "Бат Дорж: Ирсэн" in html
    assert "Сүүлд: Товчооны админ" in html
    assert reload(assignment).attendance == AttendanceStatus.PRESENT


@pytest.mark.parametrize("value", ["not_checked", "late", ""])
def test_page_rejects_invalid_value(client, admin, assignment, value):
    response = client.post(f"/admin/assignments/{assignment.id}/attendance",
                           data={"attendance": value}, follow_redirects=True)
    assert "Ирцийн төлөв буруу байна." in response.get_data(as_text=True)
    assert reload(assignment).attendance == AttendanceStatus.NOT_CHECKED


def test_future_job_page_shows_no_buttons(client, admin, make_job, employer, make_worker, assign):
    future = make_job(employer, status=JobStatus.ACCEPTED, work_date=office_today() + timedelta(days=2))
    assignment = assign(future, make_worker("99110002"))
    html = client.get(f"/admin/jobs/{future.id}/attendance").get_data(as_text=True)
    assert "Ирцийг ажлын өдрөөс" in html
    assert f"/admin/assignments/{assignment.id}/attendance" not in html


def test_missing_assignment_is_404(client, admin):
    assert client.post("/admin/assignments/99999/attendance", data={"attendance": "present"}).status_code == 404
    assert client.get("/admin/jobs/99999/attendance").status_code == 404


@pytest.mark.parametrize("role", [Role.WORKER, Role.EMPLOYER])
def test_workers_and_employers_cannot_modify_attendance(client, make_user, login, assignment, role):
    make_user(phone="99440001", role=role)
    login("99440001")
    response = client.post(f"/admin/assignments/{assignment.id}/attendance", data={"attendance": "present"})
    assert response.status_code == 403
    assert client.get(f"/admin/jobs/{assignment.job_id}/attendance").status_code == 403
    assert reload(assignment).attendance == AttendanceStatus.NOT_CHECKED


def test_assigned_worker_cannot_mark_self_present(client, login, assignment):
    login("99110001")
    response = client.post(f"/admin/assignments/{assignment.id}/attendance", data={"attendance": "present"})
    assert response.status_code == 403
    assert reload(assignment).attendance == AttendanceStatus.NOT_CHECKED


def test_anonymous_attendance_post_redirects(client, assignment):
    response = client.post(f"/admin/assignments/{assignment.id}/attendance", data={"attendance": "present"})
    assert response.status_code == 302
    assert reload(assignment).attendance == AttendanceStatus.NOT_CHECKED


def test_worker_sees_own_attendance(client, login, assignment, admin_user):
    record_attendance(assignment, AttendanceStatus.PRESENT, admin_user)
    login("99110001")
    html = client.get("/worker/assignments").get_data(as_text=True)
    assert "Ирц:" in html and "Ирсэн" in html
