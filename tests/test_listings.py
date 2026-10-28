"""Office-managed listings: create, edit, publish, pause, cancel."""
from datetime import time, timedelta

import pytest

from app.extensions import db
from app.models import Job, JobListing, JobStatus, ListingChange, ListingState, Notification, Role, User
from app.services import listings
from app.services.listings import ListingError
from app.validation import office_today
from tests.conftest import tomorrow


@pytest.fixture
def employer(make_user):
    return make_user(phone="99330001", role=Role.EMPLOYER, full_name="Ганбат Дорж")


def values(**overrides):
    data = {
        "title": "Ачаа буулгах", "description": "Машинаас 200 шуудай цемент буулгана.",
        "location": "7-р байрны ар тал, агуулах", "work_date": tomorrow(),
        "start_time": time(9), "end_time": time(17), "workers_needed": 3, "wage_per_worker": 80000,
        "notes": "", "public_area": "7-р баг орчим", "latitude": 49.03, "longitude": 104.05,
        "skill_id": None, "internal_notes": "Үүдэнд манаач байна",
    }
    data.update(overrides)
    return data


def form_data(**overrides):
    data = {
        "title": "Ачаа буулгах", "description": "Машинаас 200 шуудай цемент буулгана.",
        "location": "7-р байрны ар тал, агуулах", "work_date": tomorrow().isoformat(),
        "start_time": "09:00", "end_time": "17:00", "workers_needed": "3", "wage_per_worker": "80000",
        "notes": "", "public_area": "7-р баг орчим", "latitude": "49.03", "longitude": "104.05",
        "skill": "", "internal_notes": "",
    }
    data.update(overrides)
    return data


def reload(job):
    db.session.expire_all()
    return db.session.get(Job, job.id)


# --- service ----------------------------------------------------------------------------------

def test_create_job_is_accepted_but_unpublished_by_default(admin_user, employer):
    job = listings.create_job(admin_user, employer, values())
    assert job.status == JobStatus.ACCEPTED
    assert job.listing.is_published is False
    assert job.listing_state == ListingState.UNPUBLISHED
    assert job.status_changes[0].changed_by_id == admin_user.id
    assert [c.action for c in job.listing.changes] == ["created"]
    assert len(job.listing.public_code) >= 8


def test_create_and_publish(admin_user, employer):
    job = listings.create_job(admin_user, employer, values(), publish=True)
    assert job.listing.is_published and job.listing.published_at is not None
    assert job.is_publicly_listed
    assert [c.action for c in job.listing.changes] == ["created", "published"]
    note = db.session.scalar(db.select(Notification).filter_by(recipient_id=employer.id))
    assert note.title == "Хүсэлт зөвшөөрөгдлөө"


@pytest.mark.parametrize("overrides, message", [
    ({"work_date": office_today() - timedelta(days=1)}, "Өнгөрсөн өдөр"),
    ({"work_date": office_today() + timedelta(days=91)}, "90 хоног"),
    ({"end_time": time(9)}, "Дуусах цаг"),
    ({"workers_needed": 0}, "Ажилтны тоо"),
    ({"workers_needed": 51}, "Ажилтны тоо"),
    ({"wage_per_worker": 500}, "Цалин"),
    ({"wage_per_worker": 80000.5}, "Цалин"),
    ({"public_area": "  "}, "ойролцоо байршл"),
    ({"latitude": 49.03, "longitude": None}, "хоёуланг"),
    ({"latitude": 47.92, "longitude": 106.92}, "гадна"),   # Ulaanbaatar: outside Erdenet/Orkhon
    ({"latitude": "49.03", "longitude": "104.05"}, "буруу"),
    ({"skill_id": 9999}, "Ажлын төрөл"),
])
def test_create_validation(admin_user, employer, overrides, message):
    with pytest.raises(ListingError, match=message):
        listings.create_job(admin_user, employer, values(**overrides))
    assert db.session.scalar(db.select(db.func.count(Job.id))) == 0


@pytest.mark.parametrize("role", [Role.WORKER, Role.EMPLOYER])
def test_only_admin_creates(make_user, employer, role):
    with pytest.raises(ListingError, match="Зөвхөн оффисын админ"):
        listings.create_job(make_user(phone="99440001", role=role), employer, values())


def test_employer_must_be_employer(admin_user, make_user):
    worker = make_user(phone="99110001", role=Role.WORKER)
    with pytest.raises(ListingError, match="Ажил олгогч"):
        listings.create_job(admin_user, worker, values())


def test_find_or_create_employer(admin_user, employer, make_user):
    assert listings.find_or_create_employer(admin_user, employer.phone, "Өөр нэр", None).id == employer.id
    new = listings.find_or_create_employer(admin_user, "88990011", "Утсаар Ирсэн", "Шинэ ХХК")
    db.session.commit()
    assert new.role == Role.EMPLOYER and new.company_name == "Шинэ ХХК"
    assert not new.check_password("") and new.password_hash  # random, unknown password
    make_user(phone="99110001", role=Role.WORKER)
    with pytest.raises(ListingError, match="ажил олгогчийн бүртгэл биш"):
        listings.find_or_create_employer(admin_user, "99110001", "x", None)


def test_publish_rules(admin_user, employer, make_job):
    pending = make_job(employer)
    pending_listing = JobListing(job=pending, public_code="pend0001", public_area="Эрдэнэт", updated_by=admin_user)
    db.session.add(pending_listing)
    db.session.commit()
    with pytest.raises(ListingError, match="зөвшөөрсөн"):
        listings.publish(pending, admin_user)

    no_listing = make_job(employer, status=JobStatus.ACCEPTED)
    with pytest.raises(ListingError, match="зарын мэдээллийг"):
        listings.publish(no_listing, admin_user)

    job = listings.create_job(admin_user, employer, values(), publish=True)
    with pytest.raises(ListingError, match="аль хэдийн нийтлэгдсэн"):
        listings.publish(job, admin_user)


def test_cannot_publish_past_job(admin_user, employer, make_listed_job):
    job = make_listed_job(employer, published=False, work_date=office_today() - timedelta(days=1))
    with pytest.raises(ListingError, match="өнгөрсөн"):
        listings.publish(job, admin_user)


def test_pause_and_republish(admin_user, employer):
    job = listings.create_job(admin_user, employer, values(), publish=True)
    first_published = job.listing.published_at
    listings.pause(job, admin_user, "Ажил олгогч түр хойшлуулсан")
    job = reload(job)
    assert job.listing_state == ListingState.PAUSED and not job.is_publicly_listed
    listings.publish(job, admin_user)
    job = reload(job)
    assert job.is_publicly_listed
    assert job.listing.published_at == first_published  # first publication time kept
    assert [c.action for c in job.listing.changes] == ["created", "published", "paused", "published"]
    assert job.listing.changes[2].note == "Ажил олгогч түр хойшлуулсан"


def test_update_job_records_changes(admin_user, employer):
    job = listings.create_job(admin_user, employer, values())
    listings.update_job(job.id, admin_user, values(title="Шинэ гарчиг", public_area="8-р баг"))
    job = reload(job)
    assert job.title == "Шинэ гарчиг" and job.listing.public_area == "8-р баг"
    edit = job.listing.changes[-1]
    assert edit.action == "edited" and "ажлын нэр" in edit.note and "нийтийн байршил" in edit.note


def test_update_creates_listing_for_employer_request(admin_user, employer, make_job):
    request = make_job(employer, status=JobStatus.ACCEPTED, title="Ажил олгогчийн хүсэлт")
    assert request.listing is None and request.listing_state == ListingState.UNPUBLISHED
    listings.update_job(request.id, admin_user, values(
        title=request.title, description=request.description, location=request.location,
        work_date=request.work_date, start_time=request.start_time, end_time=request.end_time,
        workers_needed=request.workers_needed, wage_per_worker=request.wage_per_worker, notes=request.notes,
    ))
    request = reload(request)
    assert request.listing is not None and request.listing.public_area == "7-р баг орчим"
    listings.publish(request, admin_user)
    assert reload(request).is_publicly_listed


def test_schedule_and_wage_locked_once_staffed(admin_user, employer, make_worker, assign):
    from app.services.referrals import create_referral
    job = listings.create_job(admin_user, employer, values(), publish=True)
    create_referral(job.id, admin_user.id, phone="88001122")
    for change in ({"work_date": tomorrow() + timedelta(days=1)}, {"start_time": time(8)}, {"wage_per_worker": 90000}):
        with pytest.raises(ListingError, match="огноо, цаг, цалин"):
            listings.update_job(job.id, admin_user, values(**change))
    listings.update_job(job.id, admin_user, values(description="Шинэчилсэн тайлбар энд байна"))  # still fine
    assert reload(job).description == "Шинэчилсэн тайлбар энд байна"


def test_worker_count_cannot_drop_below_taken(admin_user, employer, make_worker, assign):
    from app.services.referrals import create_referral
    job = listings.create_job(admin_user, employer, values(workers_needed=3), publish=True)
    create_referral(job.id, admin_user.id, phone="88001122")
    assign(job, make_worker("99110001"))
    with pytest.raises(ListingError, match="2 ажилтан"):
        listings.update_job(job.id, admin_user, values(workers_needed=1))
    listings.update_job(job.id, admin_user, values(workers_needed=2))
    assert reload(job).listing_state == ListingState.FILLED


@pytest.mark.parametrize("status", [JobStatus.IN_PROGRESS, JobStatus.COMPLETED, JobStatus.CANCELLED])
def test_closed_jobs_cannot_be_edited(admin_user, employer, make_listed_job, status):
    job = make_listed_job(employer, status=status)
    with pytest.raises(ListingError, match="засах боломжгүй"):
        listings.update_job(job.id, admin_user, values())


def test_listing_states(admin_user, employer, make_listed_job, make_worker, assign):
    assert make_listed_job(employer).listing_state == ListingState.PUBLISHED
    assert make_listed_job(employer, published=False).listing_state == ListingState.UNPUBLISHED
    assert make_listed_job(employer, status=JobStatus.CANCELLED).listing_state == ListingState.CANCELLED
    assert make_listed_job(employer, status=JobStatus.COMPLETED).listing_state == ListingState.CLOSED
    expired = make_listed_job(employer, work_date=office_today() - timedelta(days=1))
    assert expired.listing_state == ListingState.EXPIRED
    full = make_listed_job(employer, workers_needed=1)
    assign(full, make_worker("99110001"))
    assert reload(full).listing_state == ListingState.FILLED
    # Expired/filled jobs are not deleted, just hidden.
    assert db.session.get(Job, expired.id) is not None


# --- admin pages -----------------------------------------------------------------------------------

@pytest.fixture
def admin(admin_user, login):
    login(admin_user.phone)
    return admin_user


def test_new_listing_page_with_new_employer(client, admin):
    html = client.get("/admin/listings/new").get_data(as_text=True)
    assert "Шинэ ажлын зар" in html and 'id="pin-map"' in html
    response = client.post("/admin/listings/new", data=form_data(
        employer="new", employer_name="Утсаар Ирсэн", employer_phone="8899 0011",
        employer_company="Шинэ ХХК", publish_now="y",
    ), follow_redirects=True)
    assert "Зар үүслээ. Нийтлэгдсэн." in response.get_data(as_text=True)
    employer = db.session.scalar(db.select(User).filter_by(phone="88990011"))
    job = db.session.scalar(db.select(Job))
    assert job.employer_id == employer.id and job.is_publicly_listed
    assert job.listing.latitude == 49.03


def test_new_listing_with_existing_employer(client, admin, employer):
    client.post("/admin/listings/new", data=form_data(employer=str(employer.id)))
    job = db.session.scalar(db.select(Job))
    assert job.employer_id == employer.id and not job.listing.is_published


@pytest.mark.parametrize("overrides, message", [
    ({"employer": "new", "employer_phone": "123"}, "8 оронтой"),
    ({"employer": "new", "employer_phone": "88990011", "employer_name": ""}, "нэр, утсыг"),
    ({"employer": "99999"}, "Ажил олгогч сонгоно уу."),
    ({"latitude": "abc"}, "аравтын бутархай"),
    ({"latitude": "49.03", "longitude": ""}, "хоёуланг нь"),
    ({"latitude": "40.0", "longitude": "100.0"}, "гадна"),
    ({"public_area": ""}, "Энэ талбарыг бөглөнө үү."),
    ({"wage_per_worker": "-5"}, "бүхэл төгрөг"),
    ({"workers_needed": "0"}, "1-50"),
])
def test_new_listing_page_validation(client, admin, employer, overrides, message):
    data = form_data(employer=str(employer.id))
    data.update(overrides)
    response = client.post("/admin/listings/new", data=data)
    assert response.status_code == 400
    assert message in response.get_data(as_text=True)
    assert db.session.scalar(db.select(db.func.count(Job.id))) == 0


def test_edit_publish_pause_cancel_through_pages(client, admin, employer, make_job):
    job = make_job(employer, status=JobStatus.ACCEPTED, title="Хүсэлт")
    assert "Зарын мэдээлэл бөглөх" in client.get(f"/admin/jobs/{job.id}").get_data(as_text=True)
    response = client.post(f"/admin/jobs/{job.id}/listing", data=form_data(
        title="Хүсэлт", description=job.description, location=job.location,
        work_date=job.work_date.isoformat(), start_time="09:00", end_time="13:00",
        workers_needed=str(job.workers_needed), wage_per_worker=str(job.wage_per_worker),
    ), follow_redirects=True)
    assert "Зар хадгалагдлаа." in response.get_data(as_text=True)

    client.post(f"/admin/jobs/{job.id}/listing/publish")
    assert reload(job).is_publicly_listed
    client.post(f"/admin/jobs/{job.id}/listing/pause", data={"note": "Түр"})
    assert not reload(job).is_publicly_listed

    response = client.post(f"/admin/jobs/{job.id}/listing/cancel", data={"reason": ""}, follow_redirects=True)
    assert "Цуцлах шалтгааныг" in response.get_data(as_text=True)
    assert reload(job).status == JobStatus.ACCEPTED
    client.post(f"/admin/jobs/{job.id}/listing/cancel", data={"reason": "Ажил олгогч цуцалсан"})
    job = reload(job)
    assert job.status == JobStatus.CANCELLED
    assert job.status_changes[-1].note == "Ажил олгогч цуцалсан"


def test_listing_overview_groups(client, admin, employer, make_listed_job):
    make_listed_job(employer, title="Нийтлэгдсэн А")
    make_listed_job(employer, title="Нийтлээгүй Б", published=False)
    html = client.get("/admin/listings").get_data(as_text=True)
    assert "Нийтлэгдсэн А" in html and "Нийтлээгүй Б" in html
    assert "Нийтлэгдсэн" in html and "Нийтлээгүй" in html


@pytest.mark.parametrize("role", [Role.WORKER, Role.EMPLOYER])
def test_non_admins_cannot_manage_listings(client, make_user, login, employer, make_listed_job, role):
    job = make_listed_job(employer, published=False)
    make_user(phone="99440001", role=role)
    login("99440001")
    for method, url in [("get", "/admin/listings"), ("get", "/admin/listings/new"), ("post", "/admin/listings/new"),
                        ("get", f"/admin/jobs/{job.id}/listing"), ("post", f"/admin/jobs/{job.id}/listing"),
                        ("post", f"/admin/jobs/{job.id}/listing/publish"),
                        ("post", f"/admin/jobs/{job.id}/listing/pause"),
                        ("post", f"/admin/jobs/{job.id}/listing/cancel")]:
        assert getattr(client, method)(url, data={"reason": "хуурамч"}).status_code == 403, url
    job = reload(job)
    assert not job.listing.is_published and job.status == JobStatus.ACCEPTED


def test_anonymous_cannot_manage_listings(client, employer, make_listed_job):
    job = make_listed_job(employer, published=False)
    for url in ("/admin/listings/new", f"/admin/jobs/{job.id}/listing/publish"):
        response = client.post(url)
        assert response.status_code == 302 and response.headers["Location"].startswith("/auth/login")
    assert not reload(job).listing.is_published


def test_publish_requires_post(client, admin, employer, make_listed_job):
    job = make_listed_job(employer, published=False)
    assert client.get(f"/admin/jobs/{job.id}/listing/publish").status_code == 405


def test_employer_request_is_not_published_automatically(client, make_user, login):
    from tests.conftest import job_form_data
    make_user(phone="99330005", role=Role.EMPLOYER)
    login("99330005")
    client.post("/employer/jobs/new", data=job_form_data(title="Ажил олгогчийн шинэ хүсэлт"))
    client.post("/auth/logout")
    assert "Ажил олгогчийн шинэ хүсэлт" not in client.get("/").get_data(as_text=True)
    assert db.session.scalar(db.select(Job)).listing is None


# --- Milestone 8: supervisor contact and worker instructions --------------------------------------

def test_supervisor_and_instructions_saved_from_the_form(client, admin, employer):
    client.post("/admin/listings/new", data=form_data(
        employer=str(employer.id), supervisor_name="Хянагч Болд", supervisor_phone="+976 8877-6655",
        worker_instructions="Хаалган дээр нэрээ хэлнэ"))
    listing = db.session.scalar(db.select(JobListing))
    assert (listing.supervisor_name, listing.supervisor_phone, listing.worker_instructions) == \
        ("Хянагч Болд", "88776655", "Хаалган дээр нэрээ хэлнэ")
    html = client.get(f"/admin/jobs/{listing.job_id}/listing").get_data(as_text=True)
    assert 'value="88776655"' in html and "Хаалган дээр нэрээ хэлнэ" in html


def test_bad_supervisor_phone_is_refused(client, admin, employer, admin_user):
    response = client.post("/admin/listings/new", data=form_data(employer=str(employer.id), supervisor_phone="123"))
    assert response.status_code == 400 and "8 оронтой" in response.get_data(as_text=True)
    with pytest.raises(ListingError, match="Хянагчийн утас"):
        listings.create_job(admin_user, employer, values(supervisor_phone="12ab"))
    assert db.session.scalar(db.select(db.func.count(Job.id))) == 0


def test_older_callers_without_new_fields_still_work(admin_user, employer):
    job = listings.create_job(admin_user, employer, values())
    assert (job.listing.supervisor_name, job.listing.supervisor_phone, job.listing.worker_instructions) == \
        (None, None, "")
    listings.update_job(job.id, admin_user, values(title="Шинэ нэр"))
    assert reload(job).title == "Шинэ нэр"
