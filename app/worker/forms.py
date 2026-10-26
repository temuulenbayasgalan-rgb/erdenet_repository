from flask_wtf import FlaskForm
from wtforms import RadioField, SelectChoice, SelectMultipleField, SubmitField, TextAreaField
from wtforms.validators import Length, Optional, ValidationError
from wtforms.widgets import CheckboxInput, ListWidget

from app.models import Availability, Skill, WorkerProfile
from app.validation import strip_text

INVALID_SKILL = "Сонгосон ур чадвар буруу байна."
CHOOSE_SKILL = "Дор хаяж нэг ур чадвар сонгоно уу."
CHOOSE_AVAILABILITY = "Ажил хийх боломжоо сонгоно уу."


class MultiCheckboxField(SelectMultipleField):
    """A list of checkboxes. Rejects values that are not in `choices`, with Mongolian messages."""
    widget = ListWidget(prefix_label=False)
    option_widget = CheckboxInput()

    def process_formdata(self, valuelist):
        try:
            self.data = [self.coerce(value) for value in valuelist]
        except ValueError:
            self.data = []
            raise ValueError(INVALID_SKILL)

    def pre_validate(self, form):
        allowed = {choice.value for choice in self.choices}
        if any(value not in allowed for value in self.data or []):
            raise ValidationError(INVALID_SKILL)


class ChoiceRadioField(RadioField):
    def pre_validate(self, form):
        if self.data not in {choice.value for choice in self.choices}:
            raise ValidationError(CHOOSE_AVAILABILITY)


def at_least_one(form, field):
    if not field.data:
        raise ValidationError(CHOOSE_SKILL)


class WorkerProfileForm(FlaskForm):
    skills = MultiCheckboxField("Ур чадвар", coerce=int, validators=[at_least_one])
    availability = ChoiceRadioField(
        "Ажил хийх боломж",
        choices=[SelectChoice(value, Availability.LABELS[value]) for value in Availability.ALL],
    )
    description = TextAreaField(
        "Товч танилцуулга",
        filters=[strip_text],
        validators=[
            Optional(),
            Length(
                max=WorkerProfile.DESCRIPTION_MAX_LENGTH,
                message=f"Танилцуулга {WorkerProfile.DESCRIPTION_MAX_LENGTH} тэмдэгтээс ихгүй байна.",
            ),
        ],
    )
    submit = SubmitField("Хадгалах")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only skills the office currently offers can be chosen.
        self.skills.choices = [SelectChoice(skill.id, skill.name) for skill in Skill.active()]
