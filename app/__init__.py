"""Application factory: builds and configures the Flask app."""
import os

from dotenv import load_dotenv
from flask import Flask, render_template

from config import CONFIGS
from app.extensions import csrf, db, migrate


def create_app(config_name=None):
    load_dotenv()
    config_name = config_name or os.environ.get("APP_ENV", "development")
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(CONFIGS[config_name])

    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("SECRET_KEY орчны хувьсагч тохируулагдаагүй байна.")

    os.makedirs(app.instance_path, exist_ok=True)
    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        db_path = os.path.join(app.instance_path, "erdenet_ajil.sqlite3")
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)
    csrf.init_app(app)
    # login_manager.init_app(app) is added in Milestone 1, together with the User model.

    from app.main import bp as main_bp
    app.register_blueprint(main_bp)

    register_error_handlers(app)
    return app


def register_error_handlers(app):
    @app.errorhandler(403)
    def forbidden(error):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(error):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(error):
        db.session.rollback()
        return render_template("errors/500.html"), 500
