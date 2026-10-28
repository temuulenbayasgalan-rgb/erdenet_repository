"""Office referrals: recording callers, positions, duplicates, concurrency, privacy."""
import threading
from datetime import time, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app import create_app
from app.extensions import db
from app.models import (
    Assignment,
    AssignmentStatus,
    AttendanceStatus,
    Job,
    JobListing,
    JobStatus,
    Referral,
    ReferralStatus,
    Role,
    User,
    utcnow,
)
from app.services import referrals as referral_service
from app.services.referrals import (
    DuplicateReferral,
    ReferralError,
    cancel_referral,
    create_referral,
    mark_contact_shared,
    place_referral,
)
from app.validation import office_today


@pytest.fixture
def employer(make_user):
    return make_user(phone="99330001", role=Role.EMPLOYER, full_name="Ганбат Дорж")


@pytest.fixture
def job(make_listed_job, employer):
    return make_listed_job(employer, workers_needed=2, wage_per_worker=70000)


def reload(job):
    db.session.expire_all()
    return db.session.get(Job, job.id)


def refer(job, admin, **kwargs):
    kwargs.setdefault("phone", "88001122")
    return create_referral(job.id, admin.id, **kwargs)


# --- recording ---------------------------------------------------------------------------------

def test_walk_in_referral_without_account(job, admin_user):
    referral = refer(job, admin_user, phone="8800 1122", name="Бат", note="Өглөө 8-д залгасан")
    assert referral.worker_id is None
    assert (referral.caller_phone, referral.caller_name) == ("88001122", "Бат")
    assert referral.status == ReferralStatus.REFERRED
    assert referral.created_by_id == admin_user.id and referral.created_at is not None
    assert referral.contact_shared_at is None
    assert reload(job).positions_remaining == 1


def test_referral_links_existing_worker_account(job, admin_user, make_worker):
    worker = make_worker("99110001")
    referral = refer(job, admin_user, phone="9911 0001", name="Бусад нэр")
    assert referral.worker_id == worker.id
    assert referral.caller_phone is None and referral.caller_name is None  # no copy of personal data


def test_name_only_referral_is_allowed(job, admin_user):
    referral = refer(job, admin_user, phone="", name="Утасгүй хүн")
    assert referral.caller_name == "Утасгүй хүн" and referral.caller_phone is None


@pytest.mark.parametrize("kwargs, message", [
    ({"phone": "", "name": ""}, "утасны дугаар эсвэл нэр"),
    ({"phone": "123"}, "8 оронтой"),
    ({"name": "а" * 101, "phone": ""}, "100 тэмдэгт"),
    ({"note": "а" * 501}, "500 тэмдэгт"),
])
def test_referral_validation(job, admin_user, kwargs, message):
    with pytest.raises(ReferralError, match=message):
        refer(job, admin_user, **kwargs)
    assert reload(job).referrals == []


def test_contact_shared_at_creation_and_later(job, admin_user, make_user):
    shared_now = refer(job, admin_user, phone="88001122", contact_shared=True)
    assert shared_now.contact_shared_by_id == admin_user.id and shared_now.contact_shared_at is not None

    other_admin = make_user(phone="99000002", role=Role.ADMIN, full_name="Хоёр дахь админ")
    later = refer(job, admin_user, phone="88001133")
    mark_contact_shared(job.id, later.id, other_admin)
    db.session.expire_all()
    later = db.session.get(Referral, later.id)
    assert later.contact_shared_by_id == other_admin.id
    with pytest.raises(ReferralError, match="аль хэдийн өгсөн"):
        mark_contact_shared(job.id, later.id, admin_user)


# --- eligibility -------------------------------------------------------------------------------------

@pytest.mark.parametrize("make_kwargs", [
    {"published": False},
    {"status": JobStatus.PENDING},
    {"status": JobStatus.IN_PROGRESS},
    {"status": JobStatus.CANCELLED},
    {"status": JobStatus.COMPLETED},
    {"work_date": office_today() - timedelta(days=1)},
])
def test_only_open_published_jobs_take_referrals(make_listed_job, employer, admin_user, make_kwargs):
    closed = make_listed_job(employer, **make_kwargs)
    with pytest.raises(ReferralError, match="нийтлэгдээгүй, хаагдсан"):
        refer(closed, admin_user)
    assert reload(closed).referrals == []


def test_job_without_listing_takes_no_referrals(make_job, employer, admin_user):
    unlisted = make_job(employer, status=JobStatus.ACCEPTED)
    with pytest.raises(ReferralError, match="нийтлэгдээгүй"):
        refer(unlisted, admin_user)


def test_positions_limit(job, admin_user):
    refer(job, admin_user, phone="88001111")
    refer(job, admin_user, phone="88002222")
    with pytest.raises(ReferralError, match="Орон тоо дүүрсэн"):
        refer(job, admin_user, phone="88003333")
    assert len(reload(job).open_referrals) == 2


def test_referrals_and_assignments_share_positions(job, admin_user, make_worker, assign):
    assign(job, make_worker("99110001"))
    refer(job, admin_user, phone="88001111")
    with pytest.raises(ReferralError, match="Орон тоо дүүрсэн"):
        refer(job, admin_user, phone="88002222")
    from app.services.staffing import StaffingError
    with pytest.raises(StaffingError, match="орон тоо дүүрсэн"):
        assign(job, make_worker("99110002"))


@pytest.mark.parametrize("role", [Role.WORKER, Role.EMPLOYER])
def test_only_admin_refers(job, make_user, role):
    with pytest.raises(ReferralError, match="Зөвхөн оффисын админ"):
        create_referral(job.id, make_user(phone="99440001", role=role).id, phone="88001122")


def test_inactive_or_already_assigned_worker(job, admin_user, make_worker, assign):
    make_worker("99110001", is_active=False)
    with pytest.raises(ReferralError, match="идэвхгүй"):
        refer(job, admin_user, phone="99110001")
    assigned = make_worker("99110002")
    assign(job, assigned)
    with pytest.raises(ReferralError, match="аль хэдийн томилогдсон"):
        refer(job, admin_user, phone="99110002")


# --- duplicates ---------------------------------------------------------------------------------------

def test_same_person_twice_is_refused(job, admin_user, make_worker):
    refer(job, admin_user, phone="88001122")
    with pytest.raises(ReferralError, match="аль хэдийн илгээсэн"):
        refer(job, admin_user, phone="8800-1122")
    make_worker("99110001")
    refer(job, admin_user, phone="99110001")
    assert len(reload(job).open_referrals) == 2  # the duplicate used no position
    with pytest.raises(ReferralError):
        refer(job, admin_user, phone="99110001")


def test_same_form_submitted_twice(job, admin_user):
    refer(job, admin_user, phone="88001122", request_token="form-1")
    with pytest.raises(DuplicateReferral):
        refer(job, admin_user, phone="88001133", request_token="form-1")
    assert len(reload(job).open_referrals) == 1


def test_database_blocks_duplicate_active_referral(job, admin_user):
    for _ in range(2):
        db.session.add(Referral(job_id=job.id, caller_phone="88001122", created_by_id=admin_user.id))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_cancelled_referral_frees_position_and_allows_new(job, admin_user):
    first = refer(job, admin_user, phone="88001122")
    refer(job, admin_user, phone="88002222")
    cancel_referral(job.id, first.id, admin_user, "Ажилтан очихгүй болсон")
    db.session.expire_all()
    first = db.session.get(Referral, first.id)
    assert first.status == ReferralStatus.CANCELLED
    assert (first.closed_by_id, first.close_reason) == (admin_user.id, "Ажилтан очихгүй болсон")
    assert reload(job).positions_remaining == 1
    refer(job, admin_user, phone="88001122")  # the same person can be referred again


@pytest.mark.parametrize("reason", ["", "аб", "а" * 301])
def test_cancel_needs_reason(job, admin_user, reason):
    referral = refer(job, admin_user)
    with pytest.raises(ReferralError, match="шалтгаан"):
        cancel_referral(job.id, referral.id, admin_user, reason)


def test_referral_must_belong_to_job(job, make_listed_job, employer, admin_user):
    other = make_listed_job(employer, title="Өөр ажил")
    referral = refer(other, admin_user)
    with pytest.raises(ReferralError, match="хамаарахгүй"):
        cancel_referral(job.id, referral.id, admin_user, "буруу ажил")
    with pytest.raises(ReferralError, match="хамаарахгүй"):
        mark_contact_shared(job.id, referral.id, admin_user)


# --- placement: the bridge to assignments, attendance and finance -------------------------------------

def test_place_referral_creates_one_assignment(job, admin_user, make_worker):
    worker = make_worker("99110001")
    referral = refer(job, admin_user, phone=worker.phone)
    assignment = place_referral(job.id, referral.id, admin_user, 65000)
    db.session.expire_all()
    referral = db.session.get(Referral, referral.id)
    assert referral.status == ReferralStatus.PLACED and referral.assignment_id == assignment.id
    assert assignment.agreed_wage == 65000
    assert assignment.attendance == AttendanceStatus.NOT_CHECKED  # a referral is not attendance
    job = reload(job)
    assert job.positions_taken == 1  # counted once, through the assignment
    assert db.session.scalar(db.select(db.func.count(Assignment.id))) == 1


def test_direct_assignment_takes_over_open_referral(job, admin_user, make_worker, assign):
    worker = make_worker("99110001")
    refer(job, admin_user, phone=worker.phone)
    refer(job, admin_user, phone="88002222")
    assign(job, worker)  # the job is full (2/2) but this worker already held a position
    job = reload(job)
    assert job.positions_taken == 2
    assert {r.status for r in job.referrals} == {ReferralStatus.PLACED, ReferralStatus.REFERRED}


def test_walk_in_cannot_be_placed(job, admin_user):
    referral = refer(job, admin_user, phone="88001122")
    with pytest.raises(ReferralError, match="Бүртгэлгүй хүнийг томилох боломжгүй"):
        place_referral(job.id, referral.id, admin_user, 70000)


def test_placed_referral_closes_when_assignment_cancelled(job, admin_user, make_worker):
    from app.services.staffing import cancel_assignment
    worker = make_worker("99110001")
    referral = refer(job, admin_user, phone=worker.phone)
    assignment = place_referral(job.id, referral.id, admin_user, 70000)
    cancel_assignment(assignment, admin_user, "Тестийн шалтгаан")
    db.session.expire_all()
    assert db.session.get(Referral, referral.id).status == ReferralStatus.CANCELLED
    assert reload(job).positions_remaining == 2


def test_cancelling_job_closes_referrals(job, admin_user):
    from app.services.jobs import change_job_status
    referral = refer(job, admin_user)
    change_job_status(reload(job), JobStatus.CANCELLED, admin_user, note="Ажил олгогч цуцалсан")
    db.session.expire_all()
    referral = db.session.get(Referral, referral.id)
    assert referral.status == ReferralStatus.CANCELLED and referral.close_reason == "Ажил цуцлагдсан"


def test_open_referral_is_not_paid(make_listed_job, employer, admin_user, make_worker, assign):
    """Finance counts only assignments with attendance 'present', never bare referrals."""
    from app.services import finance as finance_service
    from app.services.attendance import record_attendance
    from app.services.jobs import change_job_status

    finance_service.set_commission(10000, admin_user)
    job = make_listed_job(employer, workers_needed=2, work_date=office_today())
    refer(job, admin_user, phone="88001122")              # walk-in, never placed
    assignment = assign(job, make_worker("99110001"))
    change_job_status(reload(job), JobStatus.IN_PROGRESS, admin_user)
    record_attendance(assignment, "present", admin_user)
    change_job_status(reload(job), JobStatus.COMPLETED, admin_user)
    finance = reload(job).finance
    assert len(finance.lines) == 1 and finance.lines[0].assignment_id == assignment.id


# --- admin pages and privacy ---------------------------------------------------------------------------

@pytest.fixture
def admin(admin_user, login):
    login(admin_user.phone)
    return admin_user


def test_referral_page_shows_private_contact_to_admin(client, admin, job, employer):
    html = client.get(f"/admin/jobs/{job.id}/referrals").get_data(as_text=True)
    assert employer.phone in html and job.location in html
    assert "Зөвхөн оффис" in html


def test_referral_through_page(client, admin, job):
    page = client.get(f"/admin/jobs/{job.id}/referrals").get_data(as_text=True)
    import re
    token = re.search(r'name="request_token"[^>]*value="([^"]+)"|value="([^"]+)"[^>]*name="request_token"', page)
    token = token.group(1) or token.group(2)
    data = {"phone": "88001122", "name": "Бат", "note": "", "contact_shared": "y", "request_token": token}
    response = client.post(f"/admin/jobs/{job.id}/referrals", data=data, follow_redirects=True)
    html = response.get_data(as_text=True)
    assert "Илгээлт бүртгэгдлээ." in html and "Холбоо барих мэдээлэл өгсөн" in html
    again = client.post(f"/admin/jobs/{job.id}/referrals", data=data, follow_redirects=True)
    assert "аль хэдийн бүртгэгдсэн" in again.get_data(as_text=True)
    assert len(reload(job).referrals) == 1


def test_referral_page_validation_error(client, admin, job):
    response = client.post(f"/admin/jobs/{job.id}/referrals", data={"phone": "", "name": "", "request_token": "t"})
    assert response.status_code == 400
    assert "Утасны дугаар эсвэл нэрийг оруулна уу." in response.get_data(as_text=True)


def test_place_and_cancel_through_pages(client, admin, job, make_worker):
    worker = make_worker("99110001")
    placed = refer(job, admin, phone=worker.phone)
    walk_in = refer(job, admin, phone="88002222")
    response = client.post(f"/admin/jobs/{job.id}/referrals/{placed.id}/place", data={"agreed_wage": "70 000"},
                           follow_redirects=True)
    assert "Ажилтан томилогдлоо." in response.get_data(as_text=True)
    response = client.post(f"/admin/jobs/{job.id}/referrals/{walk_in.id}/cancel",
                           data={"reason": "Очихгүй болсон"}, follow_redirects=True)
    assert "орон тоо чөлөөлөгдлөө" in response.get_data(as_text=True)
    assert reload(job).positions_remaining == 1


@pytest.mark.parametrize("role", [Role.WORKER, Role.EMPLOYER])
def test_non_admins_cannot_see_or_record_referrals(client, make_user, login, job, admin_user, employer, role):
    referral = refer(job, admin_user)
    make_user(phone="99440001", role=role)
    login("99440001")
    for method, url in [("get", f"/admin/jobs/{job.id}/referrals"), ("post", f"/admin/jobs/{job.id}/referrals"),
                        ("post", f"/admin/jobs/{job.id}/referrals/{referral.id}/contact-shared"),
                        ("post", f"/admin/jobs/{job.id}/referrals/{referral.id}/cancel"),
                        ("post", f"/admin/jobs/{job.id}/referrals/{referral.id}/place")]:
        response = getattr(client, method)(url, data={"phone": "88009999", "reason": "хуурамч", "agreed_wage": "1"})
        assert response.status_code == 403, url
        assert employer.phone not in response.get_data(as_text=True)
    db.session.expire_all()
    assert len(reload(job).referrals) == 1
    assert db.session.get(Referral, referral.id).contact_shared_at is None


def test_employer_never_sees_referrals(client, login, job, employer, admin_user):
    refer(job, admin_user, phone="88001122", name="Залгасан Хүн")
    login(employer.phone)
    html = client.get(f"/employer/jobs/{job.id}").get_data(as_text=True)
    assert "88001122" not in html and "Залгасан Хүн" not in html


def test_referral_actions_require_post(client, admin, job):
    referral = refer(job, admin)
    for suffix in ("contact-shared", "cancel", "place"):
        assert client.get(f"/admin/jobs/{job.id}/referrals/{referral.id}/{suffix}").status_code == 405


# --- concurrency ------------------------------------------------------------------------------------------

@pytest.fixture
def last_slot_world(tmp_path, monkeypatch):
    """A published job with one position left, in a real SQLite file, so two threads can race."""
    db_file = (tmp_path / "referrals.sqlite3").as_posix()
    app = create_app("testing", {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}"})
    with app.app_context():
        db.create_all()
        admin = User(phone="99000001", full_name="Админ", role=Role.ADMIN, password_hash="x")
        employer = User(phone="99330001", full_name="Компани", role=Role.EMPLOYER, password_hash="x")
        job = Job(employer=employer, title="Ажил", description="Тайлбар тайлбар", location="Хаяг",
                  work_date=office_today() + timedelta(days=1), start_time=time(9), end_time=time(13),
                  workers_needed=1, wage_per_worker=60000, status=JobStatus.ACCEPTED)
        listing = JobListing(job=job, public_code="race0001", public_area="Эрдэнэт", is_published=True,
                             published_at=utcnow(), updated_by=admin)
        db.session.add_all([admin, employer, job, listing])
        db.session.commit()
        ids = dict(job=job.id, admin=admin.id)

    real_positions_taken = referral_service.positions_taken

    def slow_positions_taken(job_id):
        result = real_positions_taken(job_id)
        threading.Event().wait(0.3)  # hold the critical section open
        return result

    monkeypatch.setattr(referral_service, "positions_taken", slow_positions_taken)
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
            except ReferralError as error:
                outcomes.append(type(error).__name__)
            finally:
                db.session.remove()

    threads = [threading.Thread(target=run, args=(c,)) for c in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    return sorted(outcomes)


def test_concurrent_referrals_cannot_overfill(last_slot_world):
    app, ids = last_slot_world
    first = lambda: create_referral(ids["job"], ids["admin"], phone="88001111")  # noqa: E731
    second = lambda: create_referral(ids["job"], ids["admin"], phone="88002222")  # noqa: E731
    assert _race(app, [first, second]) == ["ReferralError", "ok"]
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(Referral.id))) == 1


def test_concurrent_double_submit_records_once(last_slot_world):
    app, ids = last_slot_world
    call = lambda: create_referral(ids["job"], ids["admin"], phone="88001111", request_token="same")  # noqa: E731
    assert _race(app, [call, call]) == ["DuplicateReferral", "ok"]
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(Referral.id))) == 1
