from flask import Blueprint

bp = Blueprint("employer", __name__, url_prefix="/employer")

from app.employer import routes  # noqa: E402,F401
