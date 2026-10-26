"""In-app notifications.

`notify()` only adds the notification to the current database session. The caller
commits it together with the business change that caused it, so either both are
saved or neither is: a failure can't leave an assignment without its notification
or a notification about something that didn't happen.

Each notification has an `event_key` such as "assignment:12:created". The same key
is never stored twice for the same person, so retrying a request can't duplicate it.
"""
from flask import current_app

from app.extensions import db
from app.formatting import hm, mn_date, mnt
from app.models import Notification


def url_for(endpoint, **values):
    """The path for an endpoint. Unlike flask.url_for this also works outside a web
    request (services may be called from tests or command-line tools)."""
    return current_app.url_map.bind("localhost").build(endpoint, values)


def notify(recipient, event_key, title, message, link=None):
    """Queue one notification in the current transaction. Returns it, or None if this
    event was already notified to this person."""
    if link is not None and (not link.startswith("/") or link.startswith("//")):
        raise ValueError("notification links must be internal paths")
    pending = [
        obj for obj in db.session.new
        if isinstance(obj, Notification) and obj.recipient is recipient and obj.event_key == event_key
    ]
    if pending:
        return None
    if recipient.id is not None and db.session.scalar(
        db.select(Notification.id).filter_by(recipient_id=recipient.id, event_key=event_key)
    ):
        return None
    notification = Notification(
        recipient=recipient,
        event_key=event_key,
        title=title[: Notification.TITLE_MAX_LENGTH],
        message=message[: Notification.MESSAGE_MAX_LENGTH],
        link=link,
    )
    db.session.add(notification)
    return notification


def _when(job):
    return f"{mn_date(job.work_date)} {hm(job.start_time)}–{hm(job.end_time)}"


# --- the events -----------------------------------------------------------------------
# Messages mention only the recipient's own job. Worker messages never name other
# workers; employer messages never include any worker's details.

def worker_assigned(assignment):
    job = assignment.job
    notify(
        assignment.worker,
        f"assignment:{assignment.id}:created",
        "Шинэ ажилд томилогдлоо",
        f"«{job.title}» · {_when(job)} · {job.location}. Цалин: {mnt(assignment.agreed_wage)}.",
        url_for("worker.assignments"),
    )


def worker_assignment_cancelled(assignment):
    job = assignment.job
    notify(
        assignment.worker,
        f"assignment:{assignment.id}:cancelled",
        "Томилолт цуцлагдлаа",
        f"«{job.title}» ({_when(job)}) ажлын таны томилолт цуцлагдлаа.",
        url_for("worker.assignments"),
    )


def employer_job_accepted(job):
    notify(
        job.employer,
        f"job:{job.id}:accepted",
        "Хүсэлт зөвшөөрөгдлөө",
        f"«{job.title}» ({_when(job)}) хүсэлтийг товчоо зөвшөөрлөө. Ажилтан томилогдох үед энд харагдана.",
        url_for("employer.job_detail", job_id=job.id),
    )


def employer_job_cancelled(job):
    notify(
        job.employer,
        f"job:{job.id}:cancelled",
        "Хүсэлт цуцлагдлаа",
        f"«{job.title}» ({_when(job)}) хүсэлтийг товчоо цуцаллаа.",
        url_for("employer.job_detail", job_id=job.id),
    )


def employer_job_completed(job):
    notify(
        job.employer,
        f"job:{job.id}:completed",
        "Ажил дууслаа",
        f"«{job.title}» ({_when(job)}) ажил дууссан гэж товчоо бүртгэлээ.",
        url_for("employer.job_detail", job_id=job.id),
    )
