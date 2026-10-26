from flask_wtf import FlaskForm
from sqlalchemy import select
from wtforms import BooleanField, PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, EqualTo, Length, ValidationError

from app.employer.forms import company_name_field
from app.extensions import db
from app.models import User
from app.validation import (
    MAX_NAME_LENGTH,
    MAX_PASSWORD_LENGTH,
    MIN_NAME_LENGTH,
    MIN_PASSWORD_LENGTH,
    is_valid_phone,
    normalize_phone,
    strip_text,
)

REQUIRED = "Энэ талбарыг бөглөнө үү."


class LoginForm(FlaskForm):
    phone = StringField(
        "Утасны дугаар",
        filters=[normalize_phone],
        validators=[DataRequired(REQUIRED)],
    )
    password = PasswordField("Нууц үг", validators=[DataRequired(REQUIRED)])
    remember = BooleanField("Намайг сана")
    submit = SubmitField("Нэвтрэх")


class RegistrationForm(FlaskForm):
    full_name = StringField(
        "Овог нэр",
        filters=[strip_text],
        validators=[
            DataRequired(REQUIRED),
            Length(
                min=MIN_NAME_LENGTH,
                max=MAX_NAME_LENGTH,
                message=f"Нэр {MIN_NAME_LENGTH}-{MAX_NAME_LENGTH} тэмдэгт байх ёстой.",
            ),
        ],
    )
    phone = StringField(
        "Утасны дугаар",
        filters=[normalize_phone],
        validators=[DataRequired(REQUIRED)],
    )
    password = PasswordField(
        "Нууц үг",
        validators=[
            DataRequired(REQUIRED),
            Length(
                min=MIN_PASSWORD_LENGTH,
                max=MAX_PASSWORD_LENGTH,
                message=f"Нууц үг хамгийн багадаа {MIN_PASSWORD_LENGTH} тэмдэгт байх ёстой.",
            ),
        ],
    )
    confirm_password = PasswordField(
        "Нууц үг давтах",
        validators=[
            DataRequired(REQUIRED),
            EqualTo("password", message="Нууц үг таарахгүй байна."),
        ],
    )
    submit = SubmitField("Бүртгүүлэх")

    def validate_phone(self, field):
        if not is_valid_phone(field.data):
            raise ValidationError("Утасны дугаар 8 оронтой тоо байх ёстой.")
        if db.session.scalar(select(User.id).filter_by(phone=field.data)) is not None:
            raise ValidationError("Энэ утасны дугаар бүртгэлтэй байна.")


class EmployerRegistrationForm(RegistrationForm):
    company_name = company_name_field()
