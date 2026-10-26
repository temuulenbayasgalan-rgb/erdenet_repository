"""Server-side access checks."""
from functools import wraps
from urllib.parse import urlsplit

from flask import abort
from flask_login import current_user

from app.extensions import login_manager


def role_required(*roles):
    """Allow the view only for logged-in users whose role is in `roles`.

    Anonymous users are sent to the login page; logged-in users with the
    wrong role get 403.
    """
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if not current_user.has_role(*roles):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def is_safe_redirect(target):
    """Only allow redirects to paths on this site (blocks 'next=https://evil.example')."""
    if not target:
        return False
    target = target.replace("\\", "/")
    parts = urlsplit(target)
    return (
        not parts.scheme
        and not parts.netloc
        and target.startswith("/")
        and not target.startswith("//")
    )
