from flask import Blueprint

bp = Blueprint("admin", __name__, url_prefix="/admin")

from app.admin import finance_routes, listing_routes, routes  # noqa: E402,F401
