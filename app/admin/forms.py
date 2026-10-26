from flask_wtf import FlaskForm
from wtforms import SelectChoice, SubmitField, TextAreaField
from wtforms.validators import Length, Optional

from wtforms.validators import InputRequired

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


class ApplicationFilterForm(FlaskForm):
    class Meta:
        csrf = False

    status = MnSelectField(
        "Өргөдлийн төлөв",
        choices=[SelectChoice("", "Бүгд")]
        + [SelectChoice(status, ApplicationStatus.LABELS[status]) for status in ApplicationStatus.ALL],
        default="",
    )


class ApplicationReviewForm(FlaskForm):
    decision = MnSelectField(
        "Шийдвэр",
        choices=[
            SelectChoice(ApplicationStatus.APPROVED, "Тэнцүүлэх"),
            SelectChoice(ApplicationStatus.REJECTED, "Татгалзах"),
        ],
        invalid_message="Шийдвэр буруу байна.",
    )


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
