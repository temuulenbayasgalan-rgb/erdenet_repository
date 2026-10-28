from datetime import datetime

from flask_wtf import FlaskForm
from wtforms import HiddenField, SelectChoice, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, InputRequired, Length, NumberRange, Optional

from app.fields import MnDateField, MnSelectField, MnTimeField, MoneyField
from app.models import CommissionRate, LedgerEntry, LedgerKind, PaymentMethod
from app.services.finance import new_request_token
from app.validation import OFFICE_TZ, strip_text

REQUIRED = "Энэ талбарыг бөглөнө үү."


class CommissionForm(FlaskForm):
    amount = MoneyField(
        "Нэг ирсэн ажилтанд ногдох шимтгэл (₮)",
        validators=[
            InputRequired(REQUIRED),
            NumberRange(min=0, max=CommissionRate.MAX_AMOUNT, message="Шимтгэл 0-1 000 000 ₮ бүхэл тоо байх ёстой."),
        ],
    )
    note = TextAreaField(
        "Тайлбар (заавал биш)", filters=[strip_text],
        validators=[Optional(), Length(max=300, message="300 тэмдэгтээс ихгүй байна.")],
    )
    submit = SubmitField("Шимтгэл хадгалах")


class _TokenMixin:
    """Every money form carries a one-time token; the ledger refuses a token twice."""

    def fill_token(self):
        if not self.request_token.data:
            self.request_token.data = new_request_token()
        return self


class _MoneyEntryForm(_TokenMixin, FlaskForm):
    amount = MoneyField(
        "Дүн (₮)",
        validators=[InputRequired(REQUIRED), NumberRange(min=1, message="Дүн эерэг бүхэл төгрөг байх ёстой.")],
    )
    method = MnSelectField(
        "Төлбөрийн хэлбэр",
        choices=[SelectChoice(m, PaymentMethod.LABELS[m]) for m in PaymentMethod.ALL],
        default=PaymentMethod.CASH,
        invalid_message="Төлбөрийн хэлбэр буруу байна.",
    )
    occurred_date = MnDateField("Огноо", validators=[InputRequired(REQUIRED)])
    occurred_time = MnTimeField("Цаг", validators=[InputRequired(REQUIRED)])
    reference = StringField(
        "Лавлах дугаар (заавал биш)", filters=[strip_text],
        validators=[Optional(), Length(max=LedgerEntry.REFERENCE_MAX_LENGTH, message="100 тэмдэгтээс ихгүй байна.")],
    )
    note = TextAreaField(
        "Тэмдэглэл (заавал биш)", filters=[strip_text],
        validators=[Optional(), Length(max=LedgerEntry.NOTE_MAX_LENGTH, message="300 тэмдэгтээс ихгүй байна.")],
    )
    request_token = HiddenField(validators=[DataRequired("Маягтыг дахин нээнэ үү.")])

    def fill_defaults(self, amount=None):
        now = datetime.now(OFFICE_TZ)
        if self.occurred_date.data is None:
            self.occurred_date.data = now.date()
        if self.occurred_time.data is None:
            self.occurred_time.data = now.time().replace(second=0, microsecond=0)
        if amount is not None and self.amount.data is None:
            self.amount.data = amount
        return self.fill_token()

    def occurred_at(self):
        """The typed date and time are Erdenet time; stored as an aware datetime."""
        return datetime.combine(self.occurred_date.data, self.occurred_time.data, tzinfo=OFFICE_TZ)


class ReceiptForm(_MoneyEntryForm):
    submit = SubmitField("Орлого бүртгэх")


class PayoutForm(_MoneyEntryForm):
    submit = SubmitField("Цалин олгосныг бүртгэх")


class ReversalForm(_TokenMixin, FlaskForm):
    reason = TextAreaField(
        "Буцаах шалтгаан", filters=[strip_text],
        validators=[DataRequired("Шалтгаанаа бичнэ үү."),
                    Length(min=5, max=300, message="Шалтгааныг 5-300 тэмдэгтээр бичнэ үү.")],
    )
    request_token = HiddenField(validators=[DataRequired("Маягтыг дахин нээнэ үү.")])
    submit = SubmitField("Буцаах")


class LedgerFilterForm(FlaskForm):
    class Meta:
        csrf = False

    kind = MnSelectField(
        "Төрөл",
        choices=[SelectChoice("", "Бүгд")]
        + [SelectChoice(kind, label) for kind, label in LedgerKind.LABELS.items()],
        default="",
    )
