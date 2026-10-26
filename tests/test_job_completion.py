"""Job progress and completion rules."""
import pytest

from app.extensions import db
from app.models import Assignment, AssignmentStatus, AttendanceStatus, Job, JobStatus, Role
from app.services import ServiceError
from app.services.attendance import record_attendance
from app.services.jobs import cancel_job_by_employer, change_job_status
from app.services.staffing import StaffingError, cancel_assignment
from app.validation import office_today


@pytest.fixture
def employer(make_user):
    return make_user(phone="99330001", role=Role.EMPLOYER, full_name="Компани")


@pytest.fixture
def job(make_job, employer):
    return make_job(employer, status=JobStatus.ACCEPTED, work_date=office_today(),
                    workers_needed=2, wage_per_worker=70000)


@pytest.fixture
def two_assigned(job, make_worker, assign):
    return [assign(job, make_worker("99110001")), assign(job, make_worker("99110002"))]


def reload(job):
    db.session.expire_all()
    return db.session.get(Job, job.id)


def start(job, admin):
    change_job_status(job, JobStatus.IN_PROGRESS, admin)


def test_admin_moves_accepted_to_in_progress(job, admin_user, two_assigned):
    start(job, admin_user)
    assert reload(job).status == JobStatus.IN_PROGRESS


@pytest.mark.parametrize("role", [Role.WORKER, Role.EMPLOYER])
@pytest.mark.parametrize("target", [JobStatus.IN_PROGRESS, JobStatus.COMPLETED, JobStatus.ACCEPTED, JobStatus.CANCELLED])
def test_only_admin_changes_status(job, make_user, role, target):
    user = make_user(phone="99440001", role=role)
    with pytest.raises(ServiceError, match="Зөвхөн админ"):
        change_job_status(job, target, user)
    assert reload(job).status == JobStatus.ACCEPTED


def test_completion_blocked_without_assignments(job, admin_user):
    start(job, admin_user)
    with pytest.raises(ServiceError, match="Томилогдсон ажилтангүй"):
        change_job_status(reload(job), JobStatus.COMPLETED, admin_user)
    assert reload(job).status == JobStatus.IN_PROGRESS


def test_completion_blocked_when_all_assignments_cancelled(job, admin_user, two_assigned):
    for assignment in two_assigned:
        cancel_assignment(assignment, admin_user)
    start(reload(job), admin_user)
    with pytest.raises(ServiceError, match="Томилогдсон ажилтангүй"):
        change_job_status(reload(job), JobStatus.COMPLETED, admin_user)


def test_completion_blocked_by_unchecked_attendance(job, admin_user, two_assigned):
    start(job, admin_user)
    record_attendance(two_assigned[0], AttendanceStatus.PRESENT, admin_user)
    with pytest.raises(ServiceError, match="1 ажилтны ирц бүртгэгдээгүй"):
        change_job_status(reload(job), JobStatus.COMPLETED, admin_user)
    assert reload(job).status == JobStatus.IN_PROGRESS


def test_completion_blocked_when_nobody_came(job, admin_user, two_assigned):
    start(job, admin_user)
    record_attendance(two_assigned[0], AttendanceStatus.ABSENT, admin_user)
    record_attendance(two_assigned[1], AttendanceStatus.EXCUSED, admin_user)
    with pytest.raises(ServiceError, match="Ирсэн ажилтан алга"):
        change_job_status(reload(job), JobStatus.COMPLETED, admin_user)


def test_cancelled_assignment_does_not_block_completion(job, admin_user, two_assigned):
    cancel_assignment(two_assigned[1], admin_user)
    start(reload(job), admin_user)
    record_attendance(two_assigned[0], AttendanceStatus.PRESENT, admin_user)
    change_job_status(reload(job), JobStatus.COMPLETED, admin_user)
    assert reload(job).status == JobStatus.COMPLETED


def test_successful_completion_is_recorded(job, admin_user, two_assigned):
    start(job, admin_user)
    record_attendance(two_assigned[0], AttendanceStatus.PRESENT, admin_user)
    record_attendance(two_assigned[1], AttendanceStatus.ABSENT, admin_user)
    change_job_status(reload(job), JobStatus.COMPLETED, admin_user, note="Бүгд сайн ажилласан")
    job = reload(job)
    assert job.status == JobStatus.COMPLETED
    done = job.completed_change
    assert (done.from_status, done.changed_by_id, done.note) == (
        JobStatus.IN_PROGRESS, admin_user.id, "Бүгд сайн ажилласан",
    )
    assert done.created_at is not None


def test_completion_does_not_touch_money(job, admin_user, two_assigned):
    """Completed is not paid: no amounts change and nothing payment-like is created.
    Payments arrive in Milestone 6."""
    start(job, admin_user)
    for assignment in two_assigned:
        record_attendance(assignment, AttendanceStatus.PRESENT, admin_user)
    change_job_status(reload(job), JobStatus.COMPLETED, admin_user)
    job = reload(job)
    assert job.wage_per_worker == 70000
    assert [a.agreed_wage for a in job.assignments] == [70000, 70000]
    assert not any("paid" in column.name or "payment" in column.name for column in Job.__table__.columns)
    assert not any("paid" in column.name or "payment" in column.name for column in Assignment.__table__.columns)
    assert not any("payment" in name for name in db.metadata.tables)


def test_accepted_cannot_skip_to_completed(job, admin_user, two_assigned):
    for assignment in two_assigned:
        record_attendance(assignment, AttendanceStatus.PRESENT, admin_user)
    with pytest.raises(ServiceError, match="шилжүүлэх боломжгүй"):
        change_job_status(job, JobStatus.COMPLETED, admin_user)


@pytest.mark.parametrize("target", JobStatus.ALL)
def test_completed_job_cannot_change_again(job, admin_user, two_assigned, target):
    start(job, admin_user)
    for assignment in two_assigned:
        record_attendance(assignment, AttendanceStatus.PRESENT, admin_user)
    change_job_status(reload(job), JobStatus.COMPLETED, admin_user)
    with pytest.raises(ServiceError):
        change_job_status(reload(job), target, admin_user)
    assert reload(job).status == JobStatus.COMPLETED


def test_completed_job_assignment_cannot_be_cancelled(job, admin_user, two_assigned):
    start(job, admin_user)
    for assignment in two_assigned:
        record_attendance(assignment, AttendanceStatus.PRESENT, admin_user)
    change_job_status(reload(job), JobStatus.COMPLETED, admin_user)
    with pytest.raises(StaffingError, match="Дууссан ажлын томилолт"):
        cancel_assignment(db.session.get(Assignment, two_assigned[0].id), admin_user)


def test_cancelling_accepted_job_still_cancels_assignments(job, admin_user, two_assigned):
    change_job_status(job, JobStatus.CANCELLED, admin_user)
    db.session.expire_all()
    assert {a.status for a in reload(job).assignments} == {AssignmentStatus.CANCELLED}


def test_employer_cancel_via_service_rules(make_job, employer, make_user):
    pending = make_job(employer)
    other = make_user(phone="99330002", role=Role.EMPLOYER)
    with pytest.raises(ServiceError):
        cancel_job_by_employer(pending, other)
    cancel_job_by_employer(pending, employer)
    assert reload(pending).status == JobStatus.CANCELLED
    accepted = make_job(employer, status=JobStatus.ACCEPTED)
    with pytest.raises(ServiceError, match="цуцлах боломжгүй"):
        cancel_job_by_employer(accepted, employer)


# --- through the admin page -----------------------------------------------------------------

def test_admin_page_shows_completion_problem(client, login, job, admin_user, two_assigned):
    login(admin_user.phone)
    start(job, admin_user)
    response = client.post(f"/admin/jobs/{job.id}/status", data={"new_status": "completed"})
    assert response.status_code == 400
    assert "2 ажилтны ирц бүртгэгдээгүй байна." in response.get_data(as_text=True)
    assert reload(job).status == JobStatus.IN_PROGRESS


def test_admin_completes_through_page(client, login, job, admin_user, two_assigned):
    login(admin_user.phone)
    start(job, admin_user)
    for assignment in two_assigned:
        client.post(f"/admin/assignments/{assignment.id}/attendance", data={"attendance": "present"})
    response = client.post(f"/admin/jobs/{job.id}/status", data={"new_status": "completed"}, follow_redirects=True)
    html = response.get_data(as_text=True)
    assert "Төлөв «Дууссан» болж өөрчлөгдлөө." in html
    assert "Төлбөр хараахан бүртгэгдээгүй." in html
    assert "Төлөгдсөн" not in html


@pytest.mark.parametrize("role", [Role.WORKER, Role.EMPLOYER])
def test_non_admin_cannot_complete_via_route(client, make_user, login, job, role):
    make_user(phone="99440001", role=role)
    login("99440001")
    assert client.post(f"/admin/jobs/{job.id}/status", data={"new_status": "completed"}).status_code == 403
    assert reload(job).status == JobStatus.ACCEPTED
