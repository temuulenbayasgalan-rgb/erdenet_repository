"""Application factory: builds and configures the Flask app."""
import os

from dotenv import load_dotenv
from flask import Flask, render_template
from flask_wtf.csrf import CSRFError

from config import CONFIGS
from app.extensions import csrf, db, login_manager, migrate


def create_app(config_name=None, test_config=None):
    load_dotenv()
    config_name = config_name or os.environ.get("APP_ENV", "development")
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(CONFIGS[config_name])
    if test_config:
        app.config.update(test_config)

    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("SECRET_KEY орчны хувьсагч тохируулагдаагүй байна.")

    os.makedirs(app.instance_path, exist_ok=True)
    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        db_path = os.path.join(app.instance_path, "erdenet_ajil.sqlite3")
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)
    login_manager.init_app(app)
    csrf.init_app(app)

    # Importing models registers them with SQLAlchemy and Flask-Migrate.
    from app import models  # noqa: F401

    from app.main import bp as main_bp
    from app.auth import bp as auth_bp
    from app.admin import bp as admin_bp
    from app.worker import bp as worker_bp
    from app.employer import bp as employer_bp
    from app.notifications import bp as notifications_bp
    app.register_blueprint(notifications_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(worker_bp)
    app.register_blueprint(employer_bp)

    from app.commands import create_admin_command
    app.cli.add_command(create_admin_command)

    from app.formatting import register_filters
    register_filters(app)

    register_error_handlers(app)
    return app


def register_error_handlers(app):
    @app.errorhandler(400)
    @app.errorhandler(CSRFError)
    def bad_request(error):
        return render_template("errors/400.html"), 400

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
