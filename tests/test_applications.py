"""Milestone 8: workers apply to published jobs in the app; the office accepts or rejects.

Accepting creates the assignment in the same locked transaction (services/applications.py).
"""
import threading
from datetime import time, timedelta

import pytest

from app import create_app
from app.extensions import db
from app.models import (
    ApplicationChange,
    ApplicationStatus,
    Assignment,
    AssignmentStatus,
    Availability,
    Job,
    JobApplication,
    JobListing,
    JobStatus,
    Notification,
    Referral,
    ReferralStatus,
    Role,
    Skill,
    User,
    WorkerProfile,
    utcnow,
)
from app.services import applications, listings, staffing
from app.services.applications import ApplicationError, DuplicateApplication
from app.services.jobs import change_job_status
from app.services.referrals import create_referral
from app.services.staffing import StaffingError, cancel_assignment
from app.validation import office_today
from tests.conftest import DEFAULT_PASSWORD, seed_default_skills

EXACT_ADDRESS = "Нууц гудамж 7, 3-р давхар"
SUPERVISOR_PHONE = "88776655"
INSTRUCTIONS = "08:45-д хаалган дээр хянагчид нэрээ хэлнэ"


@pytest.fixture
def employer(make_user):
    user = make_user(phone="99330001", role=Role.EMPLOYER, full_name="Ганбат Дорж")
    user.company_name = "Нууц Трейд ХХК"
    db.session.commit()
    return user


@pytest.fixture
def job(make_listed_job, employer):
    """A published job with two places, private details filled in."""
    job = make_listed_job(employer, title="Ачаа буулгах", workers_needed=2, wage_per_worker=70000,
                          location=EXACT_ADDRESS, notes="Бээлийтэй ирнэ", start_time=time(9), end_time=time(13))
    job.listing.supervisor_name = "Хянагч Болд"
    job.listing.supervisor_phone = SUPERVISOR_PHONE
    job.listing.worker_instructions = INSTRUCTIONS
    db.session.commit()
    return job


@pytest.fixture
def worker(make_worker):
    return make_worker("99110001", full_name="Бат Дорж")


def code(job):
    return job.listing.public_code


def reload(model, row_id):
    db.session.expire_all()
    return db.session.get(model, row_id)


def active_assignments(job):
    db.session.expire_all()
    return db.session.scalars(db.select(Assignment).filter_by(job_id=job.id, status=AssignmentStatus.ACTIVE)).all()


def titles(user):
    db.session.expire_all()
    return [n.title for n in db.session.scalars(
        db.select(Notification).filter_by(recipient_id=user.id).order_by(Notification.id))]


# --- public board and logged-out visitors -------------------------------------------------------------

def test_board_card_sends_logged_out_visitor_to_login_then_back(client, job):
    html = client.get("/").get_data(as_text=True)
    assert "Ажилд хүсэлт илгээх" in html
    assert f'href="/auth/login?next=/jobs/{code(job)}"' in html


def test_public_job_page_offers_login_and_registration_with_return(client, job):
    html = client.get(f"/jobs/{code(job)}").get_data(as_text=True)
    assert f"/auth/login?next=/jobs/{code(job)}" in html
    assert f"/auth/register/worker?next=/jobs/{code(job)}" in html
    for private in (EXACT_ADDRESS, SUPERVISOR_PHONE, INSTRUCTIONS, "Хянагч Болд", "Бээлийтэй ирнэ",
                    "Нууц Трейд ХХК", "99330001", "Ганбат"):
        assert private not in html


def test_logged_out_apply_goes_to_login_and_creates_nothing(client, job):
    response = client.post(f"/jobs/{code(job)}/apply")
    assert response.status_code == 302 and response.headers["Location"].startswith("/auth/login")
    assert db.session.scalar(db.select(db.func.count(JobApplication.id))) == 0


def test_login_returns_to_the_job(client, job, worker, login):
    response = login(worker.phone, next_url=f"/jobs/{code(job)}")
    assert response.headers["Location"] == f"/jobs/{code(job)}"
    html = client.get(f"/jobs/{code(job)}").get_data(as_text=True)
    assert f'action="/jobs/{code(job)}/apply"' in html


def test_login_ignores_external_next(client, worker, login):
    response = login(worker.phone, next_url="https://evil.example/")
    assert response.headers["Location"] == "/worker/"


def test_get_on_apply_url_changes_nothing(client, job, worker, login):
    """Login may land on the apply URL with GET; that only shows the job."""
    login(worker.phone)
    response = client.get(f"/jobs/{code(job)}/apply")
    assert response.headers["Location"] == f"/jobs/{code(job)}"
    assert db.session.scalar(db.select(db.func.count(JobApplication.id))) == 0


def test_registration_goes_through_profile_and_back_to_job(client, job):
    target = f"/jobs/{code(job)}"
    response = client.post(f"/auth/register/worker?next={target}", data={
        "full_name": "Шинэ Ажилтан", "phone": "99118877", "password": DEFAULT_PASSWORD,
        "confirm_password": DEFAULT_PASSWORD,
    })
    assert response.headers["Location"] == f"/worker/profile/edit?next={target}"
    skill_id = db.session.scalar(db.select(Skill.id).filter_by(code="loading"))
    response = client.post(response.headers["Location"], data={
        "skills": [str(skill_id)], "availability": "available", "description": "",
    })
    assert response.headers["Location"] == target
    html = client.get(target).get_data(as_text=True)
    assert f'action="/jobs/{code(job)}/apply"' in html


def test_incomplete_profile_is_prompted_on_public_page(client, make_worker, login, job):
    make_worker("99110002", skills=())
    login("99110002")
    html = client.get(f"/jobs/{code(job)}").get_data(as_text=True)
    assert "ур чадвараа сонгоно уу" in html and "/apply" not in html
    response = client.post(f"/jobs/{code(job)}/apply", follow_redirects=True)
    assert "ур чадвараа сонгоно уу" in response.get_data(as_text=True)
    assert db.session.scalar(db.select(db.func.count(JobApplication.id))) == 0


@pytest.mark.parametrize("role", [Role.ADMIN, Role.EMPLOYER])
def test_admins_and_employers_cannot_apply(client, make_user, login, job, role):
    make_user(phone="99440001", role=role, full_name="Шалгагч")
    login("99440001")
    assert "зөвхөн ажилтны бүртгэлээр" in client.get(f"/jobs/{code(job)}").get_data(as_text=True)
    assert client.post(f"/jobs/{code(job)}/apply").status_code == 403
    with pytest.raises(ApplicationError):
        applications.apply(job.id, db.session.scalar(db.select(User.id).filter_by(phone="99440001")))
    assert db.session.scalar(db.select(db.func.count(JobApplication.id))) == 0


# --- applying, duplicates, withdrawing ---------------------------------------------------------------------

def test_worker_applies_from_public_page(client, job, worker, login):
    login(worker.phone)
    response = client.post(f"/jobs/{code(job)}/apply", follow_redirects=True)
    html = response.get_data(as_text=True)
    assert "Хүсэлт илгээгдлээ." in html and "Хянагдаж байна" in html
    application = db.session.scalar(db.select(JobApplication))
    assert (application.worker_id, application.status) == (worker.id, ApplicationStatus.PENDING)
    assert [c.action for c in application.changes] == [ApplicationChange.APPLIED]
    assert application.changes[0].changed_by_id == worker.id
    # The public page now shows the status instead of the button.
    html = client.get(f"/jobs/{code(job)}").get_data(as_text=True)
    assert "Хянагдаж байна" in html and f'action="/jobs/{code(job)}/apply"' not in html
    # A pending application holds no position.
    assert reload(Job, job.id).positions_remaining == 2


def test_duplicate_application_is_refused(client, job, worker, login):
    login(worker.phone)
    client.post(f"/jobs/{code(job)}/apply")
    response = client.post(f"/jobs/{code(job)}/apply", follow_redirects=True)
    assert "Та энэ ажилд хүсэлт илгээсэн байна." in response.get_data(as_text=True)
    assert db.session.scalar(db.select(db.func.count(JobApplication.id))) == 1
    with pytest.raises(DuplicateApplication):
        applications.apply(job.id, worker.id)


def test_withdraw_and_reapply_keeps_history(job, worker, apply):
    application = apply(job, worker)
    applications.withdraw(application.id, worker.id)
    assert reload(JobApplication, application.id).status == ApplicationStatus.WITHDRAWN
    again = apply(job, worker)
    assert again.id == application.id and again.status == ApplicationStatus.PENDING
    assert [c.action for c in again.changes] == ["applied", "withdrawn", "applied"]
    assert again.changes[-1].note == "Дахин илгээсэн"


def test_only_own_pending_application_can_be_withdrawn(job, worker, make_worker, apply, accept):
    other = make_worker("99110002")
    application = apply(job, worker)
    with pytest.raises(ApplicationError, match="олдсонгүй"):
        applications.withdraw(application.id, other.id)
    accept(application)
    with pytest.raises(ApplicationError, match="Зөвхөн хянагдаж буй"):
        applications.withdraw(application.id, worker.id)
    assert reload(JobApplication, application.id).status == ApplicationStatus.APPROVED


@pytest.mark.parametrize("state", ["paused", "filled", "expired", "cancelled", "unpublished"])
def test_closed_jobs_refuse_new_applications(make_listed_job, make_job, employer, make_worker, assign,
                                             admin_user, worker, state):
    if state == "unpublished":
        target = make_job(employer, status=JobStatus.ACCEPTED)
    elif state == "paused":
        target = make_listed_job(employer)
        listings.pause(target, admin_user)
    elif state == "expired":
        target = make_listed_job(employer, work_date=office_today() - timedelta(days=1))
    else:
        target = make_listed_job(employer, workers_needed=1)
        if state == "filled":
            assign(target, make_worker("99110009"))
        else:
            change_job_status(target, JobStatus.CANCELLED, admin_user)
    with pytest.raises(ApplicationError):
        applications.apply(target.id, worker.id)
    assert db.session.scalar(db.select(db.func.count(JobApplication.id))) == 0


def test_worker_with_overlapping_assignment_cannot_apply(make_listed_job, employer, job, worker, assign):
    other = make_listed_job(employer, start_time=time(11), end_time=time(15))
    assign(other, worker)
    with pytest.raises(ApplicationError, match="өөр ажилд томилогдсон"):
        applications.apply(job.id, worker.id)


# --- office decisions ------------------------------------------------------------------------------------------

def test_accept_creates_confirmed_assignment_at_job_wage(job, worker, apply, accept, admin_user):
    application = apply(job, worker)
    assignment = accept(application)
    application = reload(JobApplication, application.id)
    assert application.status == ApplicationStatus.APPROVED and application.reviewed_by_id == admin_user.id
    assert (assignment.worker_id, assignment.agreed_wage, assignment.status) == (worker.id, 70000, "active")
    assert application.assignment.id == assignment.id
    assert [(c.action, c.changed_by_id) for c in application.changes] == [
        ("applied", worker.id), ("accepted", admin_user.id)]
    assert titles(worker) == ["Ажил баталгаажлаа"]
    assert reload(Job, job.id).positions_remaining == 1


def test_accept_through_inbox(client, job, worker, apply, admin_user, login):
    application = apply(job, worker)
    login(admin_user.phone)
    html = client.get("/admin/applications").get_data(as_text=True)
    assert "Бат Дорж" in html and "99110001" in html
    assert "үлдсэн <strong>2</strong>" in html and "хянагдаж буй хүсэлт <strong>1</strong>" in html
    response = client.post(f"/admin/applications/{application.id}/accept", data={"back": "/admin/applications"})
    assert response.headers["Location"] == "/admin/applications"
    assert len(active_assignments(job)) == 1


def test_accept_ignores_external_back_url(client, job, worker, apply, admin_user, login):
    application = apply(job, worker)
    login(admin_user.phone)
    response = client.post(f"/admin/applications/{application.id}/accept", data={"back": "https://evil.example"})
    assert response.headers["Location"].startswith("/admin/applications")


def test_crafted_wage_or_worker_fields_are_ignored(client, job, worker, make_worker, apply, admin_user, login):
    application = apply(job, worker)
    other = make_worker("99110002")
    login(admin_user.phone)
    client.post(f"/admin/applications/{application.id}/accept",
                data={"agreed_wage": "1", "worker_id": str(other.id), "workers_needed": "99"})
    [assignment] = active_assignments(job)
    assert (assignment.worker_id, assignment.agreed_wage) == (worker.id, 70000)
    assert reload(Job, job.id).workers_needed == 2


def test_reject_with_reason_notifies_worker(client, job, worker, apply, admin_user, login):
    application = apply(job, worker)
    login(admin_user.phone)
    client.post(f"/admin/applications/{application.id}/reject", data={"reason": "Туршлага шаардлагатай"})
    application = reload(JobApplication, application.id)
    assert application.status == ApplicationStatus.REJECTED
    assert application.changes[-1].action == "rejected" and application.changes[-1].note == "Туршлага шаардлагатай"
    note = db.session.scalar(db.select(Notification).filter_by(recipient_id=worker.id))
    assert note.title == "Хүсэлт татгалзагдлаа" and "Туршлага шаардлагатай" in note.message
    assert active_assignments(job) == []
    # A rejected worker can't simply re-apply.
    with pytest.raises(ApplicationError, match="шийдвэрлэгдсэн"):
        applications.apply(job.id, worker.id)


def test_reject_reason_too_long(job, worker, apply, admin_user):
    application = apply(job, worker)
    with pytest.raises(ApplicationError):
        applications.reject(application.id, admin_user.id, "x" * 301)
    assert reload(JobApplication, application.id).status == ApplicationStatus.PENDING


@pytest.mark.parametrize("first, second", [("accept", "accept"), ("accept", "reject"),
                                           ("reject", "accept"), ("withdraw", "accept")])
def test_decided_application_cannot_be_decided_again(job, worker, apply, admin_user, first, second):
    application = apply(job, worker)
    actions = {
        "accept": lambda: applications.accept(application.id, admin_user.id),
        "reject": lambda: applications.reject(application.id, admin_user.id),
        "withdraw": lambda: applications.withdraw(application.id, worker.id),
    }
    actions[first]()
    status = reload(JobApplication, application.id).status
    with pytest.raises(ApplicationError, match="шийдвэрлэгдсэн"):
        actions[second]()
    assert reload(JobApplication, application.id).status == status
    assert len(active_assignments(job)) == (1 if first == "accept" else 0)


def test_only_admin_can_accept_or_reject(job, worker, make_worker, apply, employer):
    application = apply(job, worker)
    for actor in (worker, employer):
        with pytest.raises(ApplicationError, match="оффисын ажилтан"):
            applications.accept(application.id, actor.id)
        with pytest.raises(ApplicationError, match="оффисын ажилтан"):
            applications.reject(application.id, actor.id)
    assert reload(JobApplication, application.id).status == ApplicationStatus.PENDING


def test_accept_rechecks_worker_availability(job, worker, apply, accept):
    application = apply(job, worker)
    worker.worker_profile.availability = Availability.TEMPORARILY_UNAVAILABLE
    db.session.commit()
    with pytest.raises(StaffingError, match="боломжгүй"):
        accept(application)
    assert reload(JobApplication, application.id).status == ApplicationStatus.PENDING


def test_accept_rechecks_overlapping_jobs(make_listed_job, employer, job, worker, apply, accept):
    """Applied to two jobs at the same time: accepting one blocks the other."""
    other = make_listed_job(employer, title="Давхцах ажил", start_time=time(12), end_time=time(16))
    first, second = apply(job, worker), apply(other, worker)
    accept(first)
    with pytest.raises(StaffingError, match="өөр ажилд томилогдсон"):
        accept(second)
    assert reload(JobApplication, second.id).status == ApplicationStatus.PENDING


@pytest.mark.parametrize("change", ["pause", "cancel", "past", "in_progress"])
def test_accept_rechecks_job_state(job, worker, apply, accept, admin_user, change):
    application = apply(job, worker)
    if change == "pause":
        listings.pause(job, admin_user)
    elif change == "cancel":
        job.status = JobStatus.CANCELLED
        db.session.commit()
    elif change == "past":
        job.work_date = office_today() - timedelta(days=1)
        db.session.commit()
    else:
        job.status = JobStatus.IN_PROGRESS
        db.session.commit()
    with pytest.raises(StaffingError):
        accept(application)
    assert active_assignments(job) == []


def test_accept_refused_when_last_place_was_taken(job, worker, make_worker, apply, accept):
    applicants = [apply(job, make_worker(f"9911000{n}")) for n in (2, 3)]
    mine = apply(job, worker)
    for application in applicants:
        accept(application)
    with pytest.raises(StaffingError, match="орон тоо дүүрсэн"):
        accept(mine)
    assert len(active_assignments(job)) == 2
    assert reload(JobApplication, mine.id).status == ApplicationStatus.PENDING


def test_inbox_filters_by_job_and_status(client, job, make_listed_job, employer, worker, make_worker, apply,
                                         admin_user, login):
    other = make_listed_job(employer, title="Өөр ажил", work_date=office_today() + timedelta(days=2))
    apply(job, worker)
    rejected = apply(other, make_worker("99110002", full_name="Татгалзсан Хүн"))
    applications.reject(rejected.id, admin_user.id)
    login(admin_user.phone)
    default = client.get("/admin/applications").get_data(as_text=True)
    assert "Бат Дорж" in default and "Татгалзсан Хүн" not in default  # pending only by default
    everything = client.get("/admin/applications?status=").get_data(as_text=True)
    assert "Бат Дорж" in everything and "Татгалзсан Хүн" in everything
    one_job = client.get(f"/admin/applications?job={other.id}&status=").get_data(as_text=True)
    assert "Татгалзсан Хүн" in one_job and "Бат Дорж" not in one_job
    bad = client.get("/admin/applications?job=abc").get_data(as_text=True)
    assert "Шүүлтүүрийн утга буруу" in bad


# --- privacy before and after acceptance ----------------------------------------------------------------------

def test_exact_details_only_after_acceptance(client, job, worker, make_worker, apply, accept, login):
    application = apply(job, worker)
    pending_other = make_worker("99110002")
    apply(job, pending_other)
    login(worker.phone)
    before = client.get(f"/worker/jobs/{job.id}").get_data(as_text=True)
    for private in (EXACT_ADDRESS, SUPERVISOR_PHONE, INSTRUCTIONS, "Бээлийтэй ирнэ"):
        assert private not in before

    accept(application)
    after = client.get(f"/worker/jobs/{job.id}").get_data(as_text=True)
    for shown in ("Ажил баталгаажсан", EXACT_ADDRESS, SUPERVISOR_PHONE, "Хянагч Болд", INSTRUCTIONS,
                  "Бээлийтэй ирнэ", "70 000 ₮", "09:00"):
        assert shown in after
    # Another applicant who is still pending sees none of it.
    client.post("/auth/logout")
    login(pending_other.phone)
    other_view = client.get(f"/worker/jobs/{job.id}").get_data(as_text=True)
    for private in (EXACT_ADDRESS, SUPERVISOR_PHONE, INSTRUCTIONS):
        assert private not in other_view


def test_supervisor_details_never_public(client, job):
    for path in ("/", f"/jobs/{code(job)}", "/?q=Ачаа"):
        html = client.get(path).get_data(as_text=True)
        for private in (EXACT_ADDRESS, SUPERVISOR_PHONE, INSTRUCTIONS, "Хянагч Болд"):
            assert private not in html, path


def test_confirmed_worker_redirected_from_public_page_after_job_fills(client, make_listed_job, employer, worker,
                                                                      apply, accept, login):
    full = make_listed_job(employer, workers_needed=1)
    accept(apply(full, worker))
    login(worker.phone)
    response = client.get(f"/jobs/{code(full)}")
    assert response.headers["Location"] == f"/worker/jobs/{full.id}"
    # Strangers just get 404 for a filled job.
    client.post("/auth/logout")
    assert client.get(f"/jobs/{code(full)}").status_code == 404


def test_worker_cannot_see_others_applications_or_assignments(client, job, worker, make_worker, apply, accept,
                                                              login):
    other = make_worker("99110002", full_name="Бусад Хүн")
    accept(apply(job, other))
    login(worker.phone)
    assert "Бусад Хүн" not in client.get("/worker/applications").get_data(as_text=True)
    assert "Бусад Хүн" not in client.get(f"/worker/jobs/{job.id}").get_data(as_text=True)
    [assignment] = active_assignments(job)
    assert client.post(f"/worker/assignments/{assignment.id}/dispute", data={"message": "Миний биш"}).status_code == 404


@pytest.mark.parametrize("path", ["/admin/applications", "/admin/applications/1/accept",
                                  "/admin/applications/1/reject", "/admin/disputes/1/resolve",
                                  "/admin/assignments/1/disputes"])
def test_inbox_and_decisions_are_admin_only(client, job, worker, apply, login, path):
    apply(job, worker)
    login(worker.phone)
    response = client.get(path) if path == "/admin/applications" else client.post(path, data={"reason": "x"})
    assert response.status_code == 403
    assert reload(JobApplication, 1).status == ApplicationStatus.PENDING


# --- notifications -------------------------------------------------------------------------------------------------

def _listing_values(job, **changes):
    values = {field: getattr(job, field) for field in listings.JOB_FIELDS}
    values.update({field: getattr(job.listing, field) for field in listings.LISTING_FIELDS})
    values.update(changes)
    return values


def test_important_change_notifies_applicants_and_placed_workers(job, worker, make_worker, apply, accept,
                                                                 admin_user):
    placed = make_worker("99110002")
    accept(apply(job, placed))
    apply(job, worker)
    bystander = make_worker("99110003")
    listings.update_job(job.id, admin_user, _listing_values(job, location="Шинэ нууц хаяг 9", notes="Малгайтай"))
    for person in (worker, placed):
        assert "Ажлын мэдээлэл өөрчлөгдлөө" in titles(person)
    assert titles(bystander) == []
    message = db.session.scalar(db.select(Notification.message).filter_by(
        recipient_id=worker.id, title="Ажлын мэдээлэл өөрчлөгдлөө"))
    # Names what changed, never the new private address.
    assert "яг хаяг" in message and "Шинэ нууц хаяг 9" not in message


def test_office_only_change_sends_no_notification(job, worker, apply, admin_user):
    apply(job, worker)
    listings.update_job(job.id, admin_user, _listing_values(job, internal_notes="Зөвхөн оффис"))
    assert "Ажлын мэдээлэл өөрчлөгдлөө" not in titles(worker)


def test_cancelled_job_rejects_pending_applications(job, worker, make_worker, apply, accept, admin_user):
    placed = make_worker("99110002")
    placement = accept(apply(job, placed))
    pending = apply(job, worker)
    change_job_status(reload(Job, job.id), JobStatus.CANCELLED, admin_user)
    pending = reload(JobApplication, pending.id)
    assert pending.status == ApplicationStatus.REJECTED and pending.changes[-1].note == "Ажил цуцлагдсан"
    assert "Хүсэлт татгалзагдлаа" in titles(worker)
    placed_application = db.session.scalar(db.select(JobApplication).filter_by(worker_id=placed.id))
    assert placed_application.status_label == "Томилолт цуцлагдсан"
    assert placed_application.changes[-1].action == "placement_cancelled"
    assert reload(Assignment, placement.id).cancel_reason == "Ажил цуцлагдсан"


# --- capacity -------------------------------------------------------------------------------------------------------

def test_cancelled_placement_frees_the_place(client, make_listed_job, employer, worker, make_worker, apply, accept,
                                            admin_user):
    full = make_listed_job(employer, workers_needed=1, title="Нэг хүний ажил")
    application = apply(full, worker)
    assignment = accept(application)
    assert "Нэг хүний ажил" not in client.get("/").get_data(as_text=True)  # filled: off the board
    with pytest.raises(ApplicationError, match="дүүрсэн"):
        applications.apply(full.id, make_worker("99110002").id)

    cancel_assignment(assignment, admin_user, "Ажилтан өвчтэй")
    assert "Нэг хүний ажил" in client.get("/").get_data(as_text=True)
    application = reload(JobApplication, application.id)
    assert application.status_label == "Томилолт цуцлагдсан"
    assert [c.action for c in application.changes][-1] == "placement_cancelled"
    assert application.changes[-1].note == "Ажилтан өвчтэй"
    assert reload(Job, full.id).positions_remaining == 1
    # The freed place can be taken by someone else.
    accept(apply(full, make_worker("99110003")))
    assert len(active_assignments(full)) == 1


def test_withdrawal_and_rejection_do_not_change_capacity(job, worker, make_worker, apply, admin_user):
    a = apply(job, worker)
    b = apply(job, make_worker("99110002"))
    assert reload(Job, job.id).positions_remaining == 2
    applications.withdraw(a.id, worker.id)
    applications.reject(b.id, admin_user.id)
    assert reload(Job, job.id).positions_remaining == 2


def test_cancel_placement_on_completed_job_is_refused(make_completed_job, admin_user):
    job = make_completed_job()
    [assignment] = job.active_assignments
    with pytest.raises(StaffingError, match="Дууссан"):
        cancel_assignment(assignment, admin_user, "Хожимдсон")


# --- legacy referrals ------------------------------------------------------------------------------------------------

def test_open_referral_still_holds_a_place(make_listed_job, employer, worker, apply, accept, admin_user):
    one = make_listed_job(employer, workers_needed=1)
    application = apply(one, worker)
    create_referral(one.id, admin_user.id, phone="88001122", name="Утсаар Ирсэн")
    with pytest.raises(StaffingError, match="орон тоо дүүрсэн"):
        accept(application)


def test_accepting_a_referred_applicant_uses_one_place(make_listed_job, employer, worker, apply, accept, admin_user):
    one = make_listed_job(employer, workers_needed=1)
    application = apply(one, worker)
    referral = create_referral(one.id, admin_user.id, phone=worker.phone)
    assignment = accept(application)
    referral = reload(Referral, referral.id)
    assert referral.status == ReferralStatus.PLACED and referral.assignment_id == assignment.id
    assert staffing.positions_taken(one.id) == 1


# --- attendance, disputes and completion -------------------------------------------------------------------------------

@pytest.fixture
def today_job(make_listed_job, employer):
    return make_listed_job(employer, work_date=office_today(), workers_needed=1, wage_per_worker=65000)


def test_full_flow_to_completion_with_dispute(client, today_job, worker, apply, accept, admin_user, login):
    from app.services import finance
    from app.services.attendance import record_attendance
    from app.services.disputes import raise_dispute, resolve_dispute

    finance.set_commission(10000, admin_user)
    assignment = accept(apply(today_job, worker))
    change_job_status(reload(Job, today_job.id), JobStatus.IN_PROGRESS, admin_user)
    record_attendance(reload(Assignment, assignment.id), "absent", admin_user)

    login(worker.phone)
    response = client.post(f"/worker/assignments/{assignment.id}/dispute",
                           data={"message": "Би 09:00-д ирсэн, хянагч мэднэ."}, follow_redirects=True)
    assert "Гомдол илгээгдлээ" in response.get_data(as_text=True)
    with pytest.raises(Exception, match="бүртгэгдсэн"):
        raise_dispute(assignment.id, worker.id, "Дахин нэг удаа")  # one open dispute per person

    # The office corrects attendance (its own audited history), but while the dispute is
    # open the job still can't be completed: nobody's claim is final until reviewed.
    record_attendance(reload(Assignment, assignment.id), "present", admin_user)
    with pytest.raises(Exception, match="шийдвэрлээгүй гомдол"):
        change_job_status(reload(Job, today_job.id), JobStatus.COMPLETED, admin_user)
    assert reload(Job, today_job.id).status == JobStatus.IN_PROGRESS and reload(Job, today_job.id).finance is None

    [dispute] = reload(Assignment, assignment.id).disputes
    resolve_dispute(dispute.id, admin_user.id, "Хянагч утсаар баталсан, ирсэн гэж засав.")
    assert "Гомдол шийдвэрлэгдлээ" in titles(worker)
    change_job_status(reload(Job, today_job.id), JobStatus.COMPLETED, admin_user)
    job = reload(Job, today_job.id)
    assert job.status == JobStatus.COMPLETED
    # Wage, commission and total stay separate in the frozen snapshot.
    assert (job.finance.total_wages, job.finance.total_commission, job.finance.total_due) == (65000, 10000, 75000)


def test_dispute_rules(make_listed_job, employer, worker, make_worker, apply, accept, admin_user):
    from app.services.disputes import DisputeError, raise_dispute, resolve_dispute
    future = make_listed_job(employer)  # tomorrow
    assignment = accept(apply(future, worker))
    with pytest.raises(DisputeError, match="ажлын өдрөөс"):
        raise_dispute(assignment.id, worker.id, "Урьдчилж гомдол")
    future.work_date = office_today()
    db.session.commit()
    stranger = make_worker("99110002")
    with pytest.raises(DisputeError, match="олдсонгүй"):
        raise_dispute(assignment.id, stranger.id, "Би энэ ажилд байгаагүй")
    with pytest.raises(DisputeError, match="тэмдэгт"):
        raise_dispute(assignment.id, worker.id, "abc")
    dispute = raise_dispute(assignment.id, employer.id, "Ажилтан 2 цаг хоцорсон")
    assert dispute.raised_role == "employer"
    assert any("гомдол" in t for t in titles(admin_user))
    with pytest.raises(DisputeError, match="оффисын ажилтан"):
        resolve_dispute(dispute.id, worker.id, "Би өөрөө шийдлээ")
    resolve_dispute(dispute.id, admin_user.id, "Ажил олгогчтой ярьж тохирсон.")
    with pytest.raises(DisputeError, match="аль хэдийн"):
        resolve_dispute(dispute.id, admin_user.id, "Дахин шийдэх гэсэн")


def test_employer_reports_on_own_job_only(client, today_job, worker, make_user, apply, accept, login):
    assignment = accept(apply(today_job, worker))
    other_employer = make_user(phone="99330009", role=Role.EMPLOYER, full_name="Өөр Компани")
    login(other_employer.phone)
    assert client.post(f"/employer/assignments/{assignment.id}/dispute",
                       data={"message": "Миний ажил биш"}).status_code == 404
    client.post("/auth/logout")
    login(today_job.employer.phone)
    response = client.post(f"/employer/assignments/{assignment.id}/dispute",
                           data={"message": "Ажилтан ирээгүй"}, follow_redirects=True)
    assert "Гомдол илгээгдлээ" in response.get_data(as_text=True)


def test_admin_records_dispute_for_supervisor_and_resolves_in_page(client, today_job, worker, apply, accept,
                                                                   admin_user, login):
    assignment = accept(apply(today_job, worker))
    login(admin_user.phone)
    client.post(f"/admin/assignments/{assignment.id}/disputes", data={"message": "Хянагч утсаар: 1 цаг хоцорсон"})
    html = client.get(f"/admin/jobs/{today_job.id}/attendance").get_data(as_text=True)
    assert "Хянагч утсаар: 1 цаг хоцорсон" in html and "Шийдвэрлэх" in html
    [dispute] = reload(Assignment, assignment.id).disputes
    client.post(f"/admin/disputes/{dispute.id}/resolve", data={"resolution": "Ажилтантай ярьж тохирсон"})
    html = client.get(f"/admin/jobs/{today_job.id}/attendance").get_data(as_text=True)
    assert "Шийдвэрлэсэн" in html and "Ажилтантай ярьж тохирсон" in html


# --- concurrency (a real SQLite file and two threads) ---------------------------------------------------------------

@pytest.fixture
def race_world(tmp_path, monkeypatch):
    db_file = (tmp_path / "applications.sqlite3").as_posix()
    app = create_app("testing", {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}"})
    with app.app_context():
        db.create_all()
        seed_default_skills()
        skill = db.session.scalar(db.select(Skill))
        admin = User(phone="99000001", full_name="Админ", role=Role.ADMIN, password_hash="x")
        admin2 = User(phone="99000002", full_name="Админ Хоёр", role=Role.ADMIN, password_hash="x")
        employer = User(phone="99330001", full_name="Компани", role=Role.EMPLOYER, password_hash="x")
        workers = []
        for n in (1, 2):
            w = User(phone=f"9911000{n}", full_name=f"Ажилтан {n}", role=Role.WORKER, password_hash="x")
            w.worker_profile = WorkerProfile(availability=Availability.AVAILABLE)
            w.worker_profile.skills = [skill]
            workers.append(w)
        job = Job(employer=employer, title="Ажил", description="Тайлбар тайлбар", location="Хаяг",
                  work_date=office_today() + timedelta(days=1), start_time=time(9), end_time=time(13),
                  workers_needed=1, wage_per_worker=60000, status=JobStatus.ACCEPTED)
        listing = JobListing(job=job, public_code="race0001", public_area="Эрдэнэт", is_published=True,
                             published_at=utcnow(), updated_by=admin)
        db.session.add_all([admin, admin2, employer, job, listing, *workers])
        db.session.commit()
        apps = [applications.apply(job.id, w.id).id for w in workers]
        ids = dict(job=job.id, admins=[admin.id, admin2.id], apps=apps, workers=[w.id for w in workers])

    real = staffing.positions_taken

    def slow_positions_taken(job_id):
        result = real(job_id)
        threading.Event().wait(0.3)  # hold the critical section open so the threads overlap
        return result

    monkeypatch.setattr(staffing, "positions_taken", slow_positions_taken)
    yield app, ids
    with app.app_context():
        db.engine.dispose()


def _race(app, calls):
    barrier = threading.Barrier(len(calls))
    outcomes = []

    def run(call):
        with app.app_context():
            barrier.wait()
            try:
                call()
                outcomes.append("ok")
            except StaffingError:
                outcomes.append("refused")
            finally:
                db.session.remove()

    threads = [threading.Thread(target=run, args=(c,)) for c in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    return sorted(outcomes)


def _active(app):
    with app.app_context():
        return db.session.scalar(db.select(db.func.count(Assignment.id)).filter_by(status=AssignmentStatus.ACTIVE))


def test_two_staff_accepting_for_the_last_place_never_overbook(race_world):
    app, ids = race_world
    calls = [lambda: applications.accept(ids["apps"][0], ids["admins"][0]),
             lambda: applications.accept(ids["apps"][1], ids["admins"][1])]
    assert _race(app, calls) == ["ok", "refused"]
    assert _active(app) == 1


def test_same_application_accepted_twice_at_once_creates_one_assignment(race_world):
    app, ids = race_world
    calls = [lambda: applications.accept(ids["apps"][0], ids["admins"][0]),
             lambda: applications.accept(ids["apps"][0], ids["admins"][1])]
    assert _race(app, calls) == ["ok", "refused"]
    assert _active(app) == 1
    with app.app_context():
        changes = db.session.scalars(db.select(ApplicationChange.action).filter_by(application_id=ids["apps"][0]))
        assert list(changes) == ["applied", "accepted"]


def test_withdraw_racing_accept_ends_consistent(race_world):
    app, ids = race_world
    calls = [lambda: applications.accept(ids["apps"][0], ids["admins"][0]),
             lambda: applications.withdraw(ids["apps"][0], ids["workers"][0])]
    assert _race(app, calls) == ["ok", "refused"]
    with app.app_context():
        application = db.session.get(JobApplication, ids["apps"][0])
        active = _active(app)
        # Either accepted with exactly one assignment, or withdrawn with none.
        assert (application.status, active) in ((ApplicationStatus.APPROVED, 1), (ApplicationStatus.WITHDRAWN, 0))
