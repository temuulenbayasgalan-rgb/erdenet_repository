import re

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Availability, Role, User, WorkerProfile

EDIT_URL = "/worker/profile/edit"


@pytest.fixture
def worker(make_user, login):
    """A logged-in worker who registered through the normal form path."""
    user = make_user(phone="99110001", role=Role.WORKER, full_name="Бат Дорж")
    login("99110001")
    return user


def profile_data(skill_ids, availability=Availability.AVAILABLE, description="Барилгад 3 жил ажилласан."):
    return {"skills": skill_ids, "availability": availability, "description": description}


def checked_values(html, name):
    """Values of the checked checkboxes/radios called `name`."""
    inputs = re.findall(r"<input[^>]*>", html)
    return {
        re.search(r'value="([^"]*)"', tag).group(1)
        for tag in inputs
        if f'name="{name}"' in tag and re.search(r"\bchecked\b", tag)
    }


def profile_of(user):
    db.session.expire_all()
    return db.session.get(User, user.id).worker_profile


# --- profile creation -------------------------------------------------------

def test_worker_registration_creates_empty_profile(client):
    client.post("/auth/register/worker", data={
        "full_name": "Сараа Болд", "phone": "99220002",
        "password": "hamgaalalt-123", "confirm_password": "hamgaalalt-123",
    })
    profile = db.session.scalar(db.select(User).filter_by(phone="99220002")).worker_profile
    assert profile is not None
    assert profile.availability == Availability.AVAILABLE
    assert profile.skills == []
    assert profile.description == ""


def test_employer_registration_creates_no_worker_profile(client):
    client.post("/auth/register/employer", data={
        "full_name": "Компани ХХК", "phone": "99220003",
        "password": "hamgaalalt-123", "confirm_password": "hamgaalalt-123",
    })
    assert db.session.scalar(db.select(User).filter_by(phone="99220003")).worker_profile is None


def test_older_worker_account_gets_profile_on_first_visit(client, worker):
    assert worker.worker_profile is None
    assert client.get("/worker/").status_code == 200
    assert profile_of(worker) is not None
    # Visiting again doesn't create a second one (user_id is unique).
    client.get("/worker/")
    assert db.session.scalar(db.select(db.func.count(WorkerProfile.id))) == 1


# --- dashboard ----------------------------------------------------------------

def test_dashboard_prompts_to_fill_empty_profile(client, worker):
    html = client.get("/worker/").get_data(as_text=True)
    assert "Ур чадвар сонгоогүй байна." in html
    assert "профайлаа бөглөж" in html
    assert "Ажил хийх боломжтой" in html
    assert EDIT_URL in html


def test_dashboard_shows_saved_profile(client, worker, skills):
    client.post(EDIT_URL, data=profile_data(
        [skills["cleaning"].id, skills["loading"].id],
        availability=Availability.TEMPORARILY_UNAVAILABLE,
    ))
    html = client.get("/worker/").get_data(as_text=True)
    assert "Цэвэрлэгээ" in html
    assert "Ачаа ачих, буулгах" in html
    assert "Тавилга зөөх" not in html
    assert "Түр завгүй" in html
    assert "Барилгад 3 жил ажилласан." in html
    assert "профайлаа бөглөж" not in html


# --- editing ------------------------------------------------------------------

def test_edit_page_lists_all_skills_in_mongolian(client, worker):
    html = client.get(EDIT_URL).get_data(as_text=True)
    assert "Профайл засах" in html
    for name in ("Барилгын туслах ажил", "Ачаа ачих, буулгах", "Цэвэрлэгээ",
                 "Тавилга зөөх", "Ерөнхий туслах ажил"):
        assert name in html
    for label in Availability.LABELS.values():
        assert label in html


def test_edit_saves_profile_and_shows_success(client, worker, skills):
    ids = [skills["construction"].id, skills["furniture_moving"].id, skills["general_labor"].id]
    response = client.post(EDIT_URL, data=profile_data(ids))
    assert response.status_code == 302
    assert response.headers["Location"] == "/worker/"
    assert "Профайл амжилттай хадгалагдлаа." in client.get("/worker/").get_data(as_text=True)

    profile = profile_of(worker)
    assert sorted(s.id for s in profile.skills) == sorted(ids)
    assert profile.description == "Барилгад 3 жил ажилласан."


def test_edit_page_is_prefilled_with_saved_values(client, worker, skills):
    client.post(EDIT_URL, data=profile_data(
        [skills["cleaning"].id], availability=Availability.UNAVAILABLE, description="Өмнөх тайлбар"
    ))
    html = client.get(EDIT_URL).get_data(as_text=True)
    assert checked_values(html, "skills") == {str(skills["cleaning"].id)}
    assert checked_values(html, "availability") == {Availability.UNAVAILABLE}
    assert "Өмнөх тайлбар" in html


def test_edit_replaces_previous_skills(client, worker, skills):
    client.post(EDIT_URL, data=profile_data([skills["cleaning"].id, skills["loading"].id]))
    client.post(EDIT_URL, data=profile_data([skills["construction"].id]))
    assert [s.code for s in profile_of(worker).skills] == ["construction"]


@pytest.mark.parametrize("status", Availability.ALL)
def test_each_availability_status_can_be_saved(client, worker, skills, status):
    client.post(EDIT_URL, data=profile_data([skills["cleaning"].id], availability=status))
    assert profile_of(worker).availability == status


def test_description_is_optional_and_trimmed(client, worker, skills):
    client.post(EDIT_URL, data=profile_data([skills["cleaning"].id], description="   "))
    assert profile_of(worker).description == ""
    client.post(EDIT_URL, data=profile_data([skills["cleaning"].id], description="  Сайн ажилтан  "))
    assert profile_of(worker).description == "Сайн ажилтан"


def test_description_html_is_escaped(client, worker, skills):
    client.post(EDIT_URL, data=profile_data([skills["cleaning"].id], description="<script>alert(1)</script>"))
    html = client.get("/worker/").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_description_at_max_length_is_accepted(client, worker, skills):
    text = "а" * WorkerProfile.DESCRIPTION_MAX_LENGTH
    assert client.post(EDIT_URL, data=profile_data([skills["cleaning"].id], description=text)).status_code == 302
    assert profile_of(worker).description == text


# --- validation -------------------------------------------------------------------

@pytest.mark.parametrize("data, message", [
    ({"skills": [], "availability": "available"}, "Дор хаяж нэг ур чадвар сонгоно уу."),
    ({"skills": ["9999"], "availability": "available"}, "Сонгосон ур чадвар буруу байна."),
    ({"skills": ["abc"], "availability": "available"}, "Сонгосон ур чадвар буруу байна."),
    ({"skills": ["SKILL"], "availability": ""}, "Ажил хийх боломжоо сонгоно уу."),
    ({"skills": ["SKILL"], "availability": "busy"}, "Ажил хийх боломжоо сонгоно уу."),
    ({"skills": ["SKILL"]}, "Ажил хийх боломжоо сонгоно уу."),
    ({"skills": ["SKILL"], "availability": "available", "description": "а" * 501},
     "Танилцуулга 500 тэмдэгтээс ихгүй байна."),
])
def test_invalid_profile_is_rejected(client, worker, skills, data, message):
    data = dict(data)
    data["skills"] = [str(skills["cleaning"].id) if v == "SKILL" else v for v in data["skills"]]
    response = client.post(EDIT_URL, data=data)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert message in html
    assert "Профайл хадгалагдсангүй." in html


def test_failed_edit_keeps_previous_profile(client, worker, skills):
    client.post(EDIT_URL, data=profile_data([skills["cleaning"].id], description="Хуучин"))
    client.post(EDIT_URL, data=profile_data([], availability="busy", description="Шинэ"))
    profile = profile_of(worker)
    assert [s.code for s in profile.skills] == ["cleaning"]
    assert profile.availability == Availability.AVAILABLE
    assert profile.description == "Хуучин"


def test_inactive_skill_cannot_be_selected(client, worker, skills):
    skills["furniture_moving"].is_active = False
    db.session.commit()
    assert "Тавилга зөөх" not in client.get(EDIT_URL).get_data(as_text=True)

    response = client.post(EDIT_URL, data=profile_data([skills["furniture_moving"].id]))
    assert "Сонгосон ур чадвар буруу байна." in response.get_data(as_text=True)
    assert profile_of(worker).skills == []


def test_database_rejects_unknown_availability(app, make_user):
    user = make_user(phone="99110009")
    db.session.add(WorkerProfile(user=user, availability="on_vacation"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


# --- ownership --------------------------------------------------------------------

def test_worker_edit_only_changes_own_profile(client, make_user, login, skills):
    other = make_user(phone="99110002", full_name="Өөр ажилтан")
    other.worker_profile = WorkerProfile(description="Бусдын профайл")
    db.session.commit()

    me = make_user(phone="99110003")
    login("99110003")
    # Extra fields like user_id are ignored; the profile always comes from the session.
    client.post(EDIT_URL, data={**profile_data([skills["cleaning"].id], description="Миний"),
                                "user_id": other.id, "id": other.worker_profile.id})

    assert profile_of(other).description == "Бусдын профайл"
    assert profile_of(other).skills == []
    assert profile_of(me).description == "Миний"
