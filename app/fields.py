"""Form fields with Mongolian error messages for values that can't be parsed."""
import re

from wtforms import DateField, IntegerField, SelectField, StringField, TimeField
from wtforms.validators import StopValidation, ValidationError

_MONEY_SEPARATORS_RE = re.compile(r"[\s,₮]")
_DIGITS_RE = re.compile(r"[0-9]+")


class _ParseErrorMixin:
    """Show one Mongolian message when the value can't be parsed, and skip the
    remaining validators (they would only add confusing extra messages)."""
    parse_error_message = "Утгыг зөв оруулна уу."

    def process_formdata(self, valuelist):
        if valuelist and not valuelist[0].strip():
            self.data = None  # empty: let InputRequired report "required"
            return
        try:
            super().process_formdata(valuelist)
        except ValueError:
            self.data = None
            raise ValueError(self.parse_error_message)

    def pre_validate(self, form):
        if self.process_errors:
            raise StopValidation()
        super().pre_validate(form)


class MnDateField(_ParseErrorMixin, DateField):
    parse_error_message = "Огноог зөв оруулна уу."


class MnTimeField(_ParseErrorMixin, TimeField):
    parse_error_message = "Цагийг зөв оруулна уу."

    def __init__(self, label=None, validators=None, **kwargs):
        # Browsers send "08:00", or "08:00:00" on some phones.
        kwargs.setdefault("format", ["%H:%M", "%H:%M:%S"])
        super().__init__(label, validators, **kwargs)


class MnSelectField(SelectField):
    """A drop-down that rejects values outside `choices` with a Mongolian message."""

    def __init__(self, label=None, validators=None, invalid_message="Сонголт буруу байна.", **kwargs):
        super().__init__(label, validators, **kwargs)
        self.invalid_message = invalid_message

    def pre_validate(self, form):
        if self.data not in {choice.value for choice in self.choices}:
            raise ValidationError(self.invalid_message)


class MnIntegerField(_ParseErrorMixin, IntegerField):
    parse_error_message = "Бүхэл тоо оруулна уу."


class _MoneyParser(StringField):
    def process_formdata(self, valuelist):
        self.data = None
        if not valuelist or not valuelist[0].strip():
            return
        cleaned = _MONEY_SEPARATORS_RE.sub("", valuelist[0])
        if not _DIGITS_RE.fullmatch(cleaned):
            raise ValueError("invalid amount")
        self.data = int(cleaned)


class MoneyField(_ParseErrorMixin, _MoneyParser):
    """Whole tögrög. Accepts '80000', '80 000', '80,000' and '80 000₮'; rejects decimals."""
    parse_error_message = "Мөнгөн дүнг бүхэл төгрөгөөр оруулна уу."

    def _value(self):
        if self.data is not None:
            return str(self.data)
        return self.raw_data[0] if self.raw_data else ""
