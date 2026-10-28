"""The public job board: no login, only published current jobs, no private data."""
import json
import re
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import Job, JobStatus, Role
from app.validation import office_phone_link, office_today


@pytest.fixture
def employer(make_user):
    user = make_user(phone="99330001", role=Role.EMPLOYER, full_name="Ганбат Дорж")
    user.company_name = "Нууц Трейд ХХК"
    db.session.commit()
    return user


@pytest.fixture
def office_phone(app):
    app.config["OFFICE_PHONE"] = "7000 1234"  # a clearly fake test number
    yield
    app.config["OFFICE_PHONE"] = ""


def board(client, query=""):
    response = client.get("/" + query)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def map_points(html):
    raw = re.search(r'<script type="application/json" id="job-map-data">(.*?)</script>', html, re.S).group(1)
    return json.loads(raw)


# --- visibility ------------------------------------------------------------------------------

def test_board_needs_no_login(client, make_listed_job, employer):
    make_listed_job(employer, title="Ачаа буулгах")
    html = board(client)
    assert "Ачаа буулгах" in html
    assert "Нэвтрэх" in html  # visitor is anonymous


def test_only_published_current_open_jobs_are_shown(client, make_listed_job, make_job, employer, make_worker, assign):
    make_listed_job(employer, title="Нээлттэй зар")
    make_listed_job(employer, title="Нийтлээгүй зар", published=False)
    make_job(employer, status=JobStatus.ACCEPTED, title="Заргүй ажил")  # no listing at all
    make_listed_job(employer, title="Хүлээгдэж буй зар", status=JobStatus.PENDING)
    make_listed_job(employer, title="Цуцлагдсан зар", status=JobStatus.CANCELLED)
    make_listed_job(employer, title="Эхэлсэн зар", status=JobStatus.IN_PROGRESS)
    make_listed_job(employer, title="Дууссан зар", status=JobStatus.COMPLETED)
    make_listed_job(employer, title="Хугацаа дууссан зар", work_date=office_today() - timedelta(days=1))
    full = make_listed_job(employer, title="Дүүрсэн зар", workers_needed=1)
    assign(full, make_worker("99110009"))

    html = board(client)
    assert "Нээлттэй зар" in html
    for hidden in ("Нийтлээгүй зар", "Заргүй ажил", "Хүлээгдэж буй зар", "Цуцлагдсан зар", "Эхэлсэн зар",
                   "Дууссан зар", "Хугацаа дууссан зар", "Дүүрсэн зар"):
        assert hidden not in html, hidden


def test_today_is_shown_yesterday_is_not(client, make_listed_job, employer):
    make_listed_job(employer, title="Өнөөдрийн ажил", work_date=office_today())
    make_listed_job(employer, title="Өчигдрийн ажил", work_date=office_today() - timedelta(days=1))
    html = board(client)
    assert "Өнөөдрийн ажил" in html and "Өчигдрийн ажил" not in html


def test_card_shows_public_fields(client, make_listed_job, employer):
    make_listed_job(employer, title="Тавилга зөөх", workers_needed=3, wage_per_worker=75000,
                    skill_code="furniture_moving", area="3-р баг орчим")
    html = board(client)
    for text in ("Тавилга зөөх", "75 000 ₮", "/ өдөр", "3 хүн хэрэгтэй", "3-р баг орчим",
                 "Тавилга зөөх", "Шинэчилсэн:", "09:00–13:00"):
        assert text in html


def test_remaining_counts_referrals_and_assignments(client, make_listed_job, employer, make_worker, assign, admin_user):
    from app.services.referrals import create_referral
    job = make_listed_job(employer, workers_needed=4)
    assign(job, make_worker("99110001"))
    create_referral(job.id, admin_user.id, phone="88001122")
    assert "2 хүн хэрэгтэй" in board(client)


def test_empty_board_message(client):
    assert "Одоогоор нээлттэй ажлын зар алга." in board(client)


def test_admin_edits_show_on_next_load(client, make_listed_job, employer, admin_user):
    from app.services import listings
    job = make_listed_job(employer, title="Хуучин гарчиг")
    assert "Хуучин гарчиг" in board(client)
    listings.pause(db.session.get(Job, job.id), admin_user)
    assert "Хуучин гарчиг" not in board(client)
    listings.publish(db.session.get(Job, job.id), admin_user)
    assert "Хуучин гарчиг" in board(client)


# --- privacy ------------------------------------------------------------------------------------

def test_no_private_data_on_public_pages(client, make_listed_job, employer, make_worker, assign, admin_user):
    from app.services.referrals import create_referral
    job = make_listed_job(employer, title="Цэвэрлэгээ", location="Яг хаяг: 7-р байр, 3 давхар, 45 тоот",
                          notes="Ажил олгогчийн тэмдэглэл", internal_notes="Дотоод нууц тэмдэглэл",
                          lat=49.0301234, lng=104.0455678, workers_needed=5)
    worker = make_worker("99110001", full_name="Ажилтан Нэртэй")
    assign(job, worker)
    create_referral(job.id, admin_user.id, phone="88001122", name="Залгасан Хүн", note="илгээлтийн тэмдэглэл")

    pages = [board(client), board(client, "?q=цэвэр"), client.get(f"/jobs/{job.listing.public_code}").get_data(as_text=True)]
    private = [employer.phone, employer.full_name, employer.company_name, job.location, job.notes,
               "Дотоод нууц тэмдэглэл", worker.phone, worker.full_name, "88001122", "Залгасан Хүн",
               "илгээлтийн тэмдэглэл", "49.0301234", "104.0455678", "60 000 ₮ × "]
    for html in pages:
        for secret in private:
            assert secret not in html, secret
    # and the job id isn't used in public URLs
    assert f"/jobs/{job.id}\"" not in pages[0]


def test_map_data_contains_only_public_fields(client, make_listed_job, employer):
    make_listed_job(employer, title="Ачаа", lat=49.0301234, lng=104.0455678)
    [point] = map_points(board(client))
    assert set(point) == {"code", "title", "lat", "lng", "area", "wage", "date", "time", "remaining", "url"}
    assert (point["lat"], point["lng"]) == (49.03, 104.046)  # rounded to ~100 m
    assert point["url"] == f"/jobs/{point['code']}"


def test_jobs_without_pin_are_listed_but_not_mapped(client, make_listed_job, employer):
    make_listed_job(employer, title="Цэггүй ажил")
    html = board(client)
    assert "Цэггүй ажил" in html
    assert map_points(html) == []
    assert "газрын зургийн цэг оруулаагүй" in html


def test_map_json_is_escaped(client, make_listed_job, employer):
    make_listed_job(employer, title="</script><script>alert(1)</script>", lat=49.03, lng=104.04)
    html = board(client)
    assert "</script><script>alert(1)" not in html
    assert map_points(html)[0]["title"] == "</script><script>alert(1)</script>"


# --- detail page --------------------------------------------------------------------------------

def test_public_detail_page(client, make_listed_job, employer, office_phone):
    job = make_listed_job(employer, title="Барилгын туслах", description="Зуурмаг зөөнө. " * 20)
    html = client.get(f"/jobs/{job.listing.public_code}").get_data(as_text=True)
    assert "Барилгын туслах" in html
    assert "Зуурмаг зөөнө. " * 20 in html.replace("\n", "")  # full text on detail
    assert 'href="tel:+97670001234"' in html


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.CANCELLED, JobStatus.COMPLETED])
def test_detail_404_for_non_public_jobs(client, make_listed_job, employer, status):
    job = make_listed_job(employer, status=status)
    assert client.get(f"/jobs/{job.listing.public_code}").status_code == 404


def test_detail_404_for_unpublished_unknown_and_numeric_ids(client, make_listed_job, employer):
    job = make_listed_job(employer, published=False)
    assert client.get(f"/jobs/{job.listing.public_code}").status_code == 404
    assert client.get("/jobs/nope").status_code == 404
    assert client.get(f"/jobs/{job.id}").status_code == 404
    assert client.get("/jobs/" + "x" * 40).status_code == 404


# --- filters ------------------------------------------------------------------------------------

def test_search_is_case_insensitive_for_cyrillic(client, make_listed_job, employer):
    make_listed_job(employer, title="Ачаа буулгах")
    make_listed_job(employer, title="Шал угаах")  # not a job-type name, so not in the filter menu
    html = board(client, "?q=АЧАА")
    assert "Ачаа буулгах" in html and "Шал угаах" not in html


def test_filter_by_job_type(client, make_listed_job, employer, skills):
    make_listed_job(employer, title="Тавилгын ажил", skill_code="furniture_moving")
    make_listed_job(employer, title="Цэвэрлэх ажил", skill_code="cleaning")
    html = board(client, f"?job_type={skills['cleaning'].id}")
    assert "Цэвэрлэх ажил" in html and "Тавилгын ажил" not in html


def test_filter_by_date(client, make_listed_job, employer):
    make_listed_job(employer, title="Өнөөдөр ажил", work_date=office_today())
    make_listed_job(employer, title="Маргааш ажил", work_date=office_today() + timedelta(days=1))
    make_listed_job(employer, title="Хол ажил", work_date=office_today() + timedelta(days=20))
    html = board(client, "?when=today")
    assert "Өнөөдөр ажил" in html and "Маргааш ажил" not in html
    html = board(client, "?when=tomorrow")
    assert "Маргааш ажил" in html and "Өнөөдөр ажил" not in html
    html = board(client, "?when=week")
    assert "Хол ажил" not in html and "Маргааш ажил" in html


def test_filter_by_min_wage(client, make_listed_job, employer):
    make_listed_job(employer, title="Бага цалин", wage_per_worker=40000)
    make_listed_job(employer, title="Өндөр цалин", wage_per_worker=90000)
    html = board(client, "?min_wage=50 000")
    assert "Өндөр цалин" in html and "Бага цалин" not in html


@pytest.mark.parametrize("query", ["?when=yesterday", "?min_wage=abc", "?job_type=999", "?q=" + "а" * 60])
def test_invalid_filters_show_warning(client, make_listed_job, employer, query):
    make_listed_job(employer, title="Ажил А")
    html = board(client, query)
    assert "Шүүлтүүрийн утга буруу байна." in html
    assert "Ажил А" in html


def test_no_match_message(client, make_listed_job, employer):
    make_listed_job(employer, title="Ажил А")
    assert "Шүүлтүүрт тохирох ажил олдсонгүй." in board(client, "?q=байхгүйүг")


# --- office phone ---------------------------------------------------------------------------------

def test_call_button_uses_configured_phone(client, office_phone, make_listed_job, employer):
    make_listed_job(employer)
    html = board(client)
    # Milestone 8: workers apply in the app; the phone is for questions and problems.
    assert "Асуух зүйл байна — Оффис руу залгах" in html
    assert 'href="tel:+97670001234"' in html
    assert "7000 1234" in html
    assert "баталгаажуулсны дараа л" in html


def test_missing_phone_shows_notice_not_broken_button(client):
    html = board(client)
    assert 'href="tel:' not in html
    assert "Оффисын утасны дугаар одоогоор тохируулагдаагүй" in html


def test_admin_sees_setup_warning_when_phone_missing(client, admin_user, login):
    login(admin_user.phone)
    assert "OFFICE_PHONE" in client.get("/admin/").get_data(as_text=True)


def test_admin_warning_gone_when_configured(client, admin_user, login, office_phone):
    login(admin_user.phone)
    assert "Тохиргоо дутуу" not in client.get("/admin/").get_data(as_text=True)


@pytest.mark.parametrize("raw, expected", [
    ("70001234", ("7000 1234", "tel:+97670001234")),
    ("+976 7000-1234", ("7000 1234", "tel:+97670001234")),
    ("", (None, None)),
    (None, (None, None)),
    ("12345", (None, None)),
    ('7000"><script>', (None, None)),
])
def test_office_phone_parsing(raw, expected):
    assert office_phone_link(raw) == expected


def test_injected_phone_config_is_not_rendered(client, app):
    app.config["OFFICE_PHONE"] = '"><script>alert(1)</script>'
    html = board(client)
    assert "<script>alert(1)" not in html
    assert 'href="tel:' not in html
    app.config["OFFICE_PHONE"] = ""


# --- leaflet and graceful degradation ---------------------------------------------------------------

def test_leaflet_is_pinned_with_integrity(client):
    html = board(client)
    assert 'integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="' in html
    assert 'integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="' in html


def test_list_is_server_rendered_without_javascript(client, make_listed_job, employer):
    """Job cards are plain HTML: the board works even if Leaflet or tiles fail."""
    make_listed_job(employer, title="JS-гүй ажил")
    html = board(client)
    assert 'class="card job-card' in html and "JS-гүй ажил" in html
    assert 'id="map-problem"' in html  # where the map-failure message appears


def test_registration_links_still_on_board(client):
    html = board(client)
    assert "/auth/register/worker" in html and "/auth/register/employer" in html


# --- workers in the app ------------------------------------------------------------------------------

def test_assigned_worker_sees_exact_address(client, make_listed_job, employer, make_worker, assign, login):
    job = make_listed_job(employer, location="Яг хаяг 12-р байр", area="12-р байр орчим")
    worker = make_worker("99110001")
    login(worker.phone)
    html = client.get(f"/worker/jobs/{job.id}").get_data(as_text=True)
    assert "Яг хаяг 12-р байр" not in html and "12-р байр орчим" in html
    assign(job, worker)
    html = client.get(f"/worker/jobs/{job.id}").get_data(as_text=True)
    assert "Яг хаяг 12-р байр" in html
