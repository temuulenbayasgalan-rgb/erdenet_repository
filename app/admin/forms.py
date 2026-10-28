from flask_wtf import FlaskForm
from wtforms import SelectChoice, SubmitField, TextAreaField
from wtforms.validators import Length, Optional

from wtforms.validators import DataRequired, InputRequired

from app.fields import MnDateField, MnIntegerField, MnSelectField, MoneyField
from app.models import ApplicationStatus, AttendanceStatus, JobStatus
from app.validation import strip_text

REQUIRED = "Энэ талбарыг бөглөнө үү."


class JobFilterForm(FlaskForm):
    """Filters for the admin job list. Sent with GET, so there is no CSRF token."""

    class Meta:
        csrf = False

    status = MnSelectField(
        "Төлөв",
        choices=[SelectChoice("", "Бүгд")]
        + [SelectChoice(status, JobStatus.LABELS[status]) for status in JobStatus.ALL],
        default="",
    )
    work_date = MnDateField("Ажлын өдөр", validators=[Optional()])
    submit = SubmitField("Шүүх")


class JobStatusForm(FlaskForm):
    new_status = MnSelectField(
        "Шинэ төлөв",
        invalid_message="Энэ төлөв рүү шилжүүлэх боломжгүй.",
    )
    note = TextAreaField(
        "Тэмдэглэл (заавал биш)",
        filters=[strip_text],
        validators=[Optional(), Length(max=500, message="500 тэмдэгтээс ихгүй байна.")],
    )
    submit = SubmitField("Төлөв өөрчлөх")

    def __init__(self, job, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only the moves allowed from the job's current status can be submitted.
        self.new_status.choices = [
            SelectChoice(status, JobStatus.LABELS[status]) for status in job.next_statuses
        ]


class ApplicationInboxFilterForm(FlaskForm):
    """Filters for the office application inbox. GET, so no CSRF token."""

    class Meta:
        csrf = False

    job = MnSelectField("Ажил", default="", invalid_message="Ажил буруу байна.")
    status = MnSelectField(
        "Хүсэлтийн төлөв",
        choices=[SelectChoice("", "Бүгд")]
        + [SelectChoice(status, ApplicationStatus.LABELS[status]) for status in ApplicationStatus.ALL],
        default=ApplicationStatus.PENDING,
    )

    def __init__(self, jobs, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.job.choices = [SelectChoice("", "Бүх ажил")] + [
            SelectChoice(str(job.id), f"{job.title} · {job.work_date:%m/%d}") for job in jobs
        ]


class RejectApplicationForm(FlaskForm):
    reason = TextAreaField(
        "Шалтгаан (ажилтанд харагдана, заавал биш)", filters=[strip_text],
        validators=[Optional(), Length(max=300, message="300 тэмдэгтээс ихгүй байна.")],
    )
    submit = SubmitField("Татгалзах")


class CancelPlacementForm(FlaskForm):
    reason = TextAreaField(
        "Цуцлах шалтгаан (дотоод бүртгэлд үлдэнэ)", filters=[strip_text],
        validators=[DataRequired("Цуцлах шалтгаанаа бичнэ үү."),
                    Length(min=3, max=300, message="3-300 тэмдэгт байх ёстой.")],
    )
    submit = SubmitField("Томилолт цуцлах")


class DisputeForm(FlaskForm):
    message = TextAreaField(
        "Юу болсон бэ?", filters=[strip_text],
        validators=[DataRequired("Тайлбараа бичнэ үү."),
                    Length(min=5, max=500, message="5-500 тэмдэгт байх ёстой.")],
    )
    submit = SubmitField("Гомдол илгээх")


class ResolveDisputeForm(FlaskForm):
    resolution = TextAreaField(
        "Шийдвэр (гомдол гаргагчид харагдана)", filters=[strip_text],
        validators=[DataRequired("Шийдвэрээ бичнэ үү."),
                    Length(min=5, max=500, message="5-500 тэмдэгт байх ёстой.")],
    )
    submit = SubmitField("Шийдвэрлэх")


class AttendanceForm(FlaskForm):
    attendance = MnSelectField(
        "Ирц",
        choices=[SelectChoice(status, AttendanceStatus.LABELS[status]) for status in AttendanceStatus.RECORDABLE],
        invalid_message="Ирцийн төлөв буруу байна.",
    )


class AssignmentForm(FlaskForm):
    """The worker list is built on the page, but every value is re-checked by
    services.staffing.assign_worker, so a crafted worker_id gains nothing."""
    worker_id = MnIntegerField("Ажилтан", validators=[InputRequired("Ажилтан сонгоно уу.")])
    agreed_wage = MoneyField("Тохирсон цалин (₮)", validators=[InputRequired(REQUIRED)])
    submit = SubmitField("Томилох")
