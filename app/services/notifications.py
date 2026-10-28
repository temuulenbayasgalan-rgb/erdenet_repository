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
from app.models import Notification, Role, User


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
    """The placement is confirmed (an accepted application or a direct assignment)."""
    job = assignment.job
    notify(
        assignment.worker,
        f"assignment:{assignment.id}:created",
        "Ажил баталгаажлаа",
        f"«{job.title}» · {_when(job)}. Цалин: {mnt(assignment.agreed_wage)}. "
        "Яг хаяг, хянагчийн утас, ирцийн зааврыг аппаас харна уу.",
        url_for("worker.job_detail", job_id=job.id),
    )


def application_rejected(application, reason=""):
    job = application.job
    message = f"«{job.title}» ({_when(job)}) ажилд илгээсэн таны хүсэлтийг товчоо хүлээж авсангүй."
    if reason:
        message += f" Шалтгаан: {reason}"
    notify(
        application.worker,
        f"application:{application.id}:rejected",
        "Хүсэлт татгалзагдлаа",
        message,
        url_for("worker.applications"),
    )


def job_details_changed(job, change_id, field_labels, recipients):
    """Important details of a job changed. Names only *which* details changed; the
    worker reads the new values on their own page (so no exact address goes to
    people who are only applicants)."""
    for worker in recipients:
        notify(
            worker,
            f"job:{job.id}:edited:{change_id}",
            "Ажлын мэдээлэл өөрчлөгдлөө",
            f"«{job.title}» ({_when(job)}): {', '.join(field_labels)} өөрчлөгдсөн. Дэлгэрэнгүйг шалгана уу.",
            url_for("worker.job_detail", job_id=job.id),
        )


def dispute_opened(dispute):
    """Tell every active office admin (except the one who recorded it)."""
    job = dispute.assignment.job
    admins = db.session.scalars(db.select(User).filter_by(role=Role.ADMIN, is_active=True)).all()
    for admin in admins:
        if admin.id == dispute.raised_by_id:
            continue
        notify(
            admin,
            f"dispute:{dispute.id}:opened",
            "Шинэ гомдол (ирц/гүйцэтгэл)",
            f"«{job.title}» ({_when(job)}): {dispute.raised_role_label} гомдол гаргалаа.",
            url_for("admin.job_attendance", job_id=job.id),
        )


def dispute_resolved(dispute):
    assignment = dispute.assignment
    job = assignment.job
    people = [dispute.raised_by] + ([assignment.worker] if assignment.worker is not dispute.raised_by else [])
    for person in people:
        if person.id == dispute.resolved_by_id:
            continue
        if person.role == Role.WORKER:
            link = url_for("worker.job_detail", job_id=job.id)
        elif person.role == Role.EMPLOYER:
            link = url_for("employer.job_detail", job_id=job.id)
        else:
            link = url_for("admin.job_attendance", job_id=job.id)
        notify(
            person,
            f"dispute:{dispute.id}:resolved",
            "Гомдол шийдвэрлэгдлээ",
            f"«{job.title}» ({_when(job)}): {dispute.resolution}",
            link,
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
    message = f"«{job.title}» ({_when(job)}) ажил дууссан гэж товчоо бүртгэлээ."
    if job.finance is not None:
        message += f" Товчоонд төлөх дүн: {mnt(job.finance.total_due)}."
    notify(
        job.employer,
        f"job:{job.id}:completed",
        "Ажил дууслаа",
        message,
        url_for("employer.job_detail", job_id=job.id),
    )
