from flask_wtf import FlaskForm
from wtforms import SelectChoice, StringField
from wtforms.validators import Length, NumberRange, Optional

from app.fields import MnSelectField, MoneyField
from app.models import Skill
from app.validation import strip_text

WHEN_CHOICES = [
    SelectChoice("", "Бүх өдөр"),
    SelectChoice("today", "Өнөөдөр"),
    SelectChoice("tomorrow", "Маргааш"),
    SelectChoice("week", "Ойрын 7 хоног"),
]


class BoardFilterForm(FlaskForm):
    """Public job board filters (GET, no account needed, so no CSRF token)."""

    class Meta:
        csrf = False

    q = StringField("Хайх", filters=[strip_text],
                    validators=[Optional(), Length(max=50, message="50 тэмдэгтээс ихгүй байна.")])
    job_type = MnSelectField("Ажлын төрөл", default="")
    when = MnSelectField("Хэзээ", choices=WHEN_CHOICES, default="")
    min_wage = MoneyField("Хамгийн бага цалин (₮)",
                          validators=[Optional(), NumberRange(min=0, max=5_000_000, message="Цалин буруу байна.")])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.job_type.choices = [SelectChoice("", "Бүх төрөл")] + [
            SelectChoice(str(skill.id), skill.name) for skill in Skill.active()
        ]
