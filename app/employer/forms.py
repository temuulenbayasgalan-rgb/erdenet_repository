from datetime import timedelta

from flask_wtf import FlaskForm
from wtforms import SelectChoice, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, InputRequired, Length, NumberRange, Optional, ValidationError

from app.fields import MnDateField, MnIntegerField, MnSelectField, MnTimeField, MoneyField
from app.models import COMPANY_NAME_MAX_LENGTH, Job, Skill
from app.validation import MAX_WAGE, MIN_WAGE, WAGE_RANGE_MESSAGE, office_today, strip_text

REQUIRED = "Энэ талбарыг бөглөнө үү."

MAX_DAYS_AHEAD = 90
MIN_WORKERS, MAX_WORKERS = 1, 50


def _length(min_length, max_length):
    if min_length:
        message = f"{min_length}-{max_length} тэмдэгт байх ёстой."
    else:
        message = f"{max_length} тэмдэгтээс ихгүй байна."
    return Length(min=min_length, max=max_length, message=message)


class JobRequestForm(FlaskForm):
    title = StringField(
        "Ажлын төрөл",
        filters=[strip_text],
        validators=[DataRequired(REQUIRED), _length(3, Job.TITLE_MAX_LENGTH)],
    )
    description = TextAreaField(
        "Хийх ажлын товч тайлбар",
        filters=[strip_text],
        validators=[DataRequired(REQUIRED), _length(10, Job.DESCRIPTION_MAX_LENGTH)],
    )
    location = StringField(
        "Ажлын байршил",
        filters=[strip_text],
        validators=[DataRequired(REQUIRED), _length(3, Job.LOCATION_MAX_LENGTH)],
    )
    work_date = MnDateField("Ажлын өдөр", validators=[InputRequired(REQUIRED)])
    start_time = MnTimeField("Эхлэх цаг", validators=[InputRequired(REQUIRED)])
    end_time = MnTimeField("Дуусах цаг (ойролцоогоор)", validators=[InputRequired(REQUIRED)])
    workers_needed = MnIntegerField(
        "Хэдэн ажилтан хэрэгтэй",
        validators=[
            InputRequired(REQUIRED),
            NumberRange(
                min=MIN_WORKERS, max=MAX_WORKERS,
                message=f"Ажилтны тоо {MIN_WORKERS}-{MAX_WORKERS} байх ёстой.",
            ),
        ],
    )
    wage_per_worker = MoneyField(
        "Нэг ажилтанд олгох цалин (₮)",
        validators=[
            InputRequired(REQUIRED),
            NumberRange(min=MIN_WAGE, max=MAX_WAGE, message=WAGE_RANGE_MESSAGE),
        ],
    )
    notes = TextAreaField(
        "Нэмэлт тэмдэглэл",
        filters=[strip_text],
        validators=[Optional(), _length(0, Job.NOTES_MAX_LENGTH)],
    )
    submit = SubmitField("Хүсэлт илгээх")

    def validate_work_date(self, field):
        if field.data is None:
            return
        today = office_today()
        if field.data < today:
            raise ValidationError("Өнгөрсөн өдөр сонгох боломжгүй.")
        if field.data > today + timedelta(days=MAX_DAYS_AHEAD):
            raise ValidationError(f"Ажлын өдөр {MAX_DAYS_AHEAD} хоногийн дотор байх ёстой.")

    def validate_end_time(self, field):
        if self.start_time.data and field.data and field.data <= self.start_time.data:
            raise ValidationError("Дуусах цаг эхлэх цагаас хойш байх ёстой.")


def company_name_field():
    return StringField(
        "Байгууллагын нэр (заавал биш)",
        filters=[strip_text],
        validators=[Optional(), _length(2, COMPANY_NAME_MAX_LENGTH)],
        description="Ажилтнуудад харагдана. Жишээ нь: Эрдэнэ Трейд ХХК. Хоосон бол «Хувь ажил олгогч» гэж харагдана.",
    )


class CompanyNameForm(FlaskForm):
    company_name = company_name_field()
    submit = SubmitField("Хадгалах")


class WorkerDirectoryFilterForm(FlaskForm):
    """GET filters for the worker directory. `jobs` are the employer's own upcoming requests."""

    class Meta:
        csrf = False

    skill = MnSelectField("Ур чадвар", default="")
    job = MnSelectField("Миний хүсэлтийн цагт чөлөөтэй", default="")
    submit = SubmitField("Шүүх")

    def __init__(self, jobs, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.skill.choices = [SelectChoice("", "Бүгд")] + [
            SelectChoice(str(skill.id), skill.name) for skill in Skill.active()
        ]
        # Only the employer's own requests can be chosen, so no other job's
        # schedule can be probed through this filter.
        self.job.choices = [SelectChoice("", "Цаг сонгоогүй")] + [
            SelectChoice(str(job.id), f"{job.title} · {job.work_date:%m-%d} {job.start_time:%H:%M}–{job.end_time:%H:%M}")
            for job in jobs
        ]
