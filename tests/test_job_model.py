"""Job model rules, database constraints and display helpers."""
from datetime import date, datetime, time, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.formatting import hm, mn_date, mn_datetime, mnt
from app.models import Job, JobStatus, Role


@pytest.fixture
def employer(make_user):
    return make_user(phone="99330001", role=Role.EMPLOYER)


def test_every_status_has_label_badge_and_transitions():
    for status in JobStatus.ALL:
        assert status in JobStatus.LABELS
        assert status in JobStatus.BADGES
        assert status in JobStatus.TRANSITIONS
        assert set(JobStatus.TRANSITIONS[status]) <= set(JobStatus.ALL)


def test_final_statuses_have_no_way_out():
    assert JobStatus.TRANSITIONS[JobStatus.COMPLETED] == ()
    assert JobStatus.TRANSITIONS[JobStatus.CANCELLED] == ()


def test_change_status_records_history(make_job, employer):
    job = make_job(employer)
    job.change_status(JobStatus.ACCEPTED, changed_by=employer, note="ok")
    db.session.commit()
    assert job.status == JobStatus.ACCEPTED
    assert job.status_changes[-1].from_status == JobStatus.PENDING
    assert job.status_changes[-1].note == "ok"


def test_change_status_rejects_disallowed_move(make_job, employer):
    job = make_job(employer)
    with pytest.raises(ValueError):
        job.change_status(JobStatus.COMPLETED, changed_by=employer)
    assert job.status == JobStatus.PENDING
    assert len(job.status_changes) == 1


def test_total_wage_is_integer(make_job, employer):
    job = make_job(employer, workers_needed=3, wage_per_worker=85000)
    assert job.total_wage == 255000
    assert isinstance(job.total_wage, int)


@pytest.mark.parametrize("overrides", [
    {"status": "deleted"},
    {"workers_needed": 0},
    {"wage_per_worker": 0},
    {"wage_per_worker": -5000},
    {"start_time": time(12, 0), "end_time": time(12, 0)},
    {"start_time": time(12, 0), "end_time": time(8, 0)},
])
def test_database_constraints(employer, overrides):
    values = dict(
        employer=employer, title="Ажил", description="Тайлбар тайлбар", location="Хаяг",
        work_date=date(2030, 1, 1), start_time=time(9, 0), end_time=time(17, 0),
        workers_needed=1, wage_per_worker=50000,
    )
    values.update(overrides)
    db.session.add(Job(**values))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


@pytest.mark.parametrize("amount, text", [
    (0, "0 ₮"), (1000, "1 000 ₮"), (80000, "80 000 ₮"), (5_000_000, "5 000 000 ₮"), (None, ""),
])
def test_mnt_format(amount, text):
    assert mnt(amount) == text


def test_date_and_time_formats():
    assert mn_date(date(2026, 10, 28)) == "2026-10-28 (Лхагва)"
    assert hm(time(9, 5)) == "09:05"
    # Stored in UTC, shown in Erdenet time (UTC+8).
    assert mn_datetime(datetime(2026, 10, 27, 20, 30)) == "2026-10-28 04:30"
    assert mn_datetime(datetime(2026, 10, 27, 20, 30, tzinfo=timezone.utc)) == "2026-10-28 04:30"
