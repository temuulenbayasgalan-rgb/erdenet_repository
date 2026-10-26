"""Each user's own notifications. Every lookup is filtered by the logged-in user."""
from flask import abort, flash, redirect, render_template, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import Notification, utcnow
from app.notifications import bp
from app.security import is_safe_redirect

LIST_LIMIT = 100


@bp.app_context_processor
def inject_unread_count():
    """Unread count for the navigation bar, on every page."""
    if not current_user.is_authenticated:
        return {}
    count = db.session.scalar(
        db.select(db.func.count(Notification.id)).filter_by(recipient_id=current_user.id, read_at=None)
    )
    return {"unread_notification_count": count}


def _own_notification_or_404(notification_id):
    notification = db.session.scalar(
        db.select(Notification).filter_by(id=notification_id, recipient_id=current_user.id)
    )
    if notification is None:
        abort(404)
    return notification


@bp.route("/")
@login_required
def index():
    items = db.session.scalars(
        db.select(Notification)
        .filter_by(recipient_id=current_user.id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(LIST_LIMIT)
    ).all()
    return render_template("notifications/index.html", notifications=items)


@bp.route("/<int:notification_id>/open", methods=["POST"])
@login_required
def open_notification(notification_id):
    """Mark as read, then go to the page it is about."""
    notification = _own_notification_or_404(notification_id)
    notification.mark_read()
    db.session.commit()
    if is_safe_redirect(notification.link):
        return redirect(notification.link)
    return redirect(url_for("notifications.index"))


@bp.route("/read-all", methods=["POST"])
@login_required
def read_all():
    db.session.execute(
        db.update(Notification)
        .where(Notification.recipient_id == current_user.id, Notification.read_at.is_(None))
        .values(read_at=utcnow())
    )
    db.session.commit()
    flash("Бүх мэдэгдлийг уншсан гэж тэмдэглэлээ.", "info")
    return redirect(url_for("notifications.index"))
