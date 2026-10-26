from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth import bp
from app.auth.forms import EmployerRegistrationForm, LoginForm, RegistrationForm
from app.extensions import db
from app.models import Role, User, WorkerProfile
from app.security import is_safe_redirect

INVALID_LOGIN = "Утасны дугаар эсвэл нууц үг буруу байна."


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for(current_user.dashboard_endpoint))

    form = LoginForm()
    if form.validate_on_submit():
        user = db.session.scalar(select(User).filter_by(phone=form.phone.data))
        # Same message for unknown phone, wrong password and inactive account,
        # so the page does not reveal which phone numbers are registered.
        if user is None or not user.check_password(form.password.data) or not user.is_active:
            flash(INVALID_LOGIN, "danger")
        else:
            login_user(user, remember=form.remember.data)
            next_url = request.args.get("next")
            if is_safe_redirect(next_url):
                return redirect(next_url)
            return redirect(url_for(user.dashboard_endpoint))

    return render_template("auth/login.html", form=form)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Та системээс гарлаа.", "info")
    return redirect(url_for("main.index"))


@bp.route("/register/worker", methods=["GET", "POST"])
def register_worker():
    return _register(Role.WORKER, "Ажилтнаар бүртгүүлэх", RegistrationForm)


@bp.route("/register/employer", methods=["GET", "POST"])
def register_employer():
    return _register(Role.EMPLOYER, "Ажил олгогчоор бүртгүүлэх", EmployerRegistrationForm)


def _register(role, title, form_class):
    """Shared registration logic. The role comes from the URL, never from the form,
    so nobody can register themselves as an admin."""
    if current_user.is_authenticated:
        return redirect(url_for(current_user.dashboard_endpoint))

    form = form_class()
    if form.validate_on_submit():
        user = User(phone=form.phone.data, full_name=form.full_name.data, role=role)
        user.set_password(form.password.data)
        if role == Role.WORKER:
            user.worker_profile = WorkerProfile()
        if role == Role.EMPLOYER:
            user.company_name = form.company_name.data or None
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            # Another registration with the same phone finished first.
            db.session.rollback()
            form.phone.errors.append("Энэ утасны дугаар бүртгэлтэй байна.")
        else:
            login_user(user)
            flash("Бүртгэл амжилттай үүслээ.", "success")
            return redirect(url_for(user.dashboard_endpoint))

    return render_template("auth/register.html", form=form, title=title)
