"""Command-line tools, run with `flask --app app <command>`."""
import click
from flask.cli import with_appcontext
from sqlalchemy import select

from app.extensions import db
from app.models import Role, User
from app.validation import (
    MAX_NAME_LENGTH,
    MAX_PASSWORD_LENGTH,
    MIN_NAME_LENGTH,
    MIN_PASSWORD_LENGTH,
    is_valid_phone,
    normalize_phone,
)


@click.command("create-admin")
@click.option("--phone", prompt="Утасны дугаар", help="8 оронтой утасны дугаар")
@click.option("--name", "full_name", prompt="Овог нэр", help="Админы нэр")
@with_appcontext
def create_admin_command(phone, full_name):
    """Create an admin account. The password is always typed in, never passed as an argument."""
    phone = normalize_phone(phone)
    if not is_valid_phone(phone):
        raise click.ClickException("Утасны дугаар 8 оронтой тоо байх ёстой.")

    full_name = full_name.strip()
    if not MIN_NAME_LENGTH <= len(full_name) <= MAX_NAME_LENGTH:
        raise click.ClickException(
            f"Нэр {MIN_NAME_LENGTH}-{MAX_NAME_LENGTH} тэмдэгт байх ёстой."
        )

    if db.session.scalar(select(User.id).filter_by(phone=phone)) is not None:
        raise click.ClickException("Энэ утасны дугаар бүртгэлтэй байна.")

    password = click.prompt(
        "Нууц үг",
        hide_input=True,
        confirmation_prompt="Нууц үгээ давтан оруулна уу",
    )
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise click.ClickException(
            f"Нууц үг {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} тэмдэгт байх ёстой."
        )

    user = User(phone=phone, full_name=full_name, role=Role.ADMIN)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    click.echo(f"Админ бүртгэгдлээ: {full_name} ({phone})")
