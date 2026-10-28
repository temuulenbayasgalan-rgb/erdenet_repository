"""The employer-facing worker directory and assigned workers on employer jobs."""
from datetime import time

import pytest

from app.models import Availability, JobStatus, Role


@pytest.fixture
def employer(make_user, login):
    user = make_user(phone="99330001", role=Role.EMPLOYER, full_name="Эрдэнэ Трейд ХХК")
    login("99330001")
    return user


@pytest.fixture
def workers(make_worker):
    return {
        "cleaner": make_worker("99110001", full_name="Батын Дорж", skills=("cleaning",),
                               description="Оффис цэвэрлэгээ 2 жил"),
        "mover": make_worker("99110002", full_name="Сүхийн Болд", skills=("furniture_moving", "loading")),
        "inactive": make_worker("99110003", full_name="Идэвхгүй Хүн", is_active=False),
        "unavailable": make_worker("99110004", full_name="Завгүй Хүн", availability=Availability.UNAVAILABLE),
        "temporary": make_worker("99110005", full_name="Түр Хүн",
                                 availability=Availability.TEMPORARILY_UNAVAILABLE),
        "no_skills": make_worker("99110006", full_name="Профайлгүй Хүн", skills=()),
    }


def directory(client, query=""):
    response = client.get(f"/employer/workers{query}")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_directory_lists_only_eligible_workers(client, employer, workers):
    html = directory(client)
    assert "Б.Дорж" in html
    assert "С.Болд" in html
    for key in ("inactive", "unavailable", "temporary", "no_skills"):
        assert workers[key].public_name not in html, key
    assert "(2)" in html


def test_directory_shows_public_fields(client, employer, workers):
    html = directory(client)
    assert "Цэвэрлэгээ" in html
    assert "Тавилга зөөх" in html
    assert "Ажил хийх боломжтой" in html
    assert "Оффис цэвэрлэгээ 2 жил" in html


def test_directory_hides_private_data(client, employer, workers):
    html = directory(client)
    for worker in workers.values():
        assert worker.phone not in html
        assert worker.full_name not in html  # only the short public name
        assert worker.password_hash not in html
    profile = client.get(f"/employer/workers/{workers['cleaner'].id}").get_data(as_text=True)
    assert workers["cleaner"].phone not in profile
    assert "Батын Дорж" not in profile
    assert "Б.Дорж" in profile


def test_filter_by_skill(client, employer, workers, skills):
    html = directory(client, f"?skill={skills['furniture_moving'].id}")
    assert "С.Болд" in html
    assert "Б.Дорж" not in html


def test_filter_by_free_during_own_job(client, employer, workers, make_job, assign):
    my_job = make_job(employer, status=JobStatus.ACCEPTED, start_time=time(9), end_time=time(13))
    elsewhere = make_job(employer, status=JobStatus.ACCEPTED, start_time=time(12), end_time=time(16))
    assign(elsewhere, workers["cleaner"])

    html = directory(client, f"?job={my_job.id}")
    assert "Б.Дорж" not in html  # busy 12:00-16:00, overlaps 09:00-13:00
    assert "С.Болд" in html
    # Without a chosen time window both are listed.
    assert "Б.Дорж" in directory(client)


def test_back_to_back_worker_still_listed(client, employer, workers, make_job, assign):
    my_job = make_job(employer, status=JobStatus.PENDING, start_time=time(9), end_time=time(13))
    earlier = make_job(employer, status=JobStatus.ACCEPTED, start_time=time(6), end_time=time(9))
    assign(earlier, workers["cleaner"])
    assert "Б.Дорж" in directory(client, f"?job={my_job.id}")


def test_filters_combine(client, employer, workers, skills, make_job):
    my_job = make_job(employer, status=JobStatus.PENDING)
    html = directory(client, f"?skill={skills['cleaning'].id}&job={my_job.id}")
    assert "Б.Дорж" in html and "С.Болд" not in html


@pytest.mark.parametrize("query", ["?skill=abc", "?skill=99999", "?job=abc", "?job=99999"])
def test_invalid_filter_shows_warning(client, employer, workers, query):
    html = directory(client, query)
    assert "Шүүлтүүрийн утга буруу байна." in html
    assert "Б.Дорж" in html


def test_other_employers_job_cannot_be_used_as_filter(client, employer, workers, make_user, make_job, assign):
    other = make_user(phone="99330002", role=Role.EMPLOYER, full_name="Өөр компани")
    their_job = make_job(other, status=JobStatus.ACCEPTED, title="Тэдний нууц ажил")
    assign(their_job, workers["cleaner"])
    html = directory(client, f"?job={their_job.id}")
    assert "Шүүлтүүрийн утга буруу байна." in html
    assert "Тэдний нууц ажил" not in html


def test_final_jobs_are_not_offered_as_filter(client, employer, workers, make_job):
    done = make_job(employer, status=JobStatus.COMPLETED, title="Дууссан ажил")
    html = directory(client)
    assert "Дууссан ажил" not in html
    assert "Шүүлтүүрийн утга буруу" in directory(client, f"?job={done.id}")


@pytest.mark.parametrize("key", ["inactive", "unavailable", "temporary", "no_skills"])
def test_ineligible_public_profile_is_404(client, employer, workers, key):
    assert client.get(f"/employer/workers/{workers[key].id}").status_code == 404


def test_non_worker_public_profile_is_404(client, employer, make_user):
    admin = make_user(phone="99000001", role=Role.ADMIN)
    assert client.get(f"/employer/workers/{admin.id}").status_code == 404
    assert client.get(f"/employer/workers/{employer.id}").status_code == 404
    assert client.get("/employer/workers/99999").status_code == 404


def test_directory_has_no_assignment_actions(client, employer, workers):
    html = directory(client)
    assert "/assignments" not in html
    assert "Томилох" not in html
    profile = client.get(f"/employer/workers/{workers['cleaner'].id}").get_data(as_text=True)
    assert 'method="post"' not in profile.split("<main")[1].split("</main>")[0]


# --- assigned workers on the employer's own jobs ----------------------------------------------

def test_employer_sees_assigned_workers_on_own_job(client, employer, workers, make_job, assign):
    job = make_job(employer, status=JobStatus.ACCEPTED, workers_needed=3)
    assign(job, workers["cleaner"])
    html = client.get(f"/employer/jobs/{job.id}").get_data(as_text=True)
    assert "Томилогдсон ажилтнууд" in html
    assert "Б.Дорж" in html
    assert "(1 / 3)" in html
    assert workers["cleaner"].phone not in html


def test_cancelled_assignment_not_shown_to_employer(client, employer, workers, make_job, assign, admin_user):
    from app.services.staffing import cancel_assignment
    job = make_job(employer, status=JobStatus.ACCEPTED)
    cancel_assignment(assign(job, workers["cleaner"]), admin_user, "Тестийн шалтгаан")
    assert "Б.Дорж" not in client.get(f"/employer/jobs/{job.id}").get_data(as_text=True)


def test_employer_cannot_see_other_employers_assignments(client, employer, workers, make_user, make_job, assign):
    other = make_user(phone="99330002", role=Role.EMPLOYER, full_name="Өөр компани")
    their_job = make_job(other, status=JobStatus.ACCEPTED)
    assign(their_job, workers["cleaner"])
    assert client.get(f"/employer/jobs/{their_job.id}").status_code == 404
    assert "Б.Дорж" not in client.get("/employer/").get_data(as_text=True)
