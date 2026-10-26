from flask import Blueprint

bp = Blueprint("worker", __name__, url_prefix="/worker")

from app.worker import routes  # noqa: E402,F401
