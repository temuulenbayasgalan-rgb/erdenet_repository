from flask_wtf import FlaskForm
from wtforms import BooleanField, HiddenField, SelectChoice, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, InputRequired, Length, Optional, ValidationError

from app.employer.forms import JobRequestForm
from app.fields import CoordinateField, MnSelectField, MoneyField
from app.models import COMPANY_NAME_MAX_LENGTH, JobListing, Referral, Role, Skill, User
from app.services.finance import new_request_token
from app.validation import is_valid_phone, normalize_phone, strip_text
from app.extensions import db

REQUIRED = "Энэ талбарыг бөглөнө үү."
NEW_EMPLOYER = "new"


class ListingForm(JobRequestForm):
    """The employer request fields plus the office-only listing fields."""
    skill = MnSelectField("Ажлын төрөл", default="", invalid_message="Ажлын төрөл буруу байна.")
    public_area = StringField(
        "Нийтэд харагдах ойролцоо байршил", filters=[strip_text],
        validators=[DataRequired(REQUIRED),
                    Length(min=3, max=JobListing.PUBLIC_AREA_MAX_LENGTH, message="3-100 тэмдэгт байх ёстой.")],
        description="Жишээ нь: «5-р баг, Шинэ төв орчим». Яг хаягийг энд бүү бичнэ үү.",
    )
    latitude = CoordinateField("Өргөрөг (заавал биш)")
    longitude = CoordinateField("Уртраг (заавал биш)")
    internal_notes = TextAreaField(
        "Дотоод тэмдэглэл (зөвхөн оффис)", filters=[strip_text],
        validators=[Optional(), Length(max=JobListing.INTERNAL_NOTES_MAX_LENGTH, message="500 тэмдэгтээс ихгүй байна.")],
    )
    supervisor_name = StringField(
        "Хянагчийн нэр (заавал биш)", filters=[strip_text],
        validators=[Optional(), Length(max=JobListing.SUPERVISOR_NAME_MAX_LENGTH, message="100 тэмдэгтээс ихгүй байна.")],
    )
    supervisor_phone = StringField("Хянагчийн утас (заавал биш)", filters=[normalize_phone])
    worker_instructions = TextAreaField(
        "Ажилтанд өгөх заавар (заавал биш)", filters=[strip_text],
        validators=[Optional(), Length(max=JobListing.INSTRUCTIONS_MAX_LENGTH, message="500 тэмдэгтээс ихгүй байна.")],
        description="Жишээ нь: «08:45-д хаалган дээр ирж хянагчид нэрээ хэлнэ. Ажлын хувцастай ирнэ.»",
    )
    submit = SubmitField("Хадгалах")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The employer's request form calls this field "type of work"; here it is the title.
        self.title.label.text = "Зарын гарчиг"
        self.location.label.text = "Яг хаяг (нийтэд биш; баталгаажсан ажилтан харна)"
        self.skill.choices = [SelectChoice("", "Сонгоогүй")] + [
            SelectChoice(str(skill.id), skill.name) for skill in Skill.active()
        ]

    def validate_supervisor_phone(self, field):
        if field.data and not is_valid_phone(field.data):
            raise ValidationError("Утасны дугаар 8 оронтой тоо байх ёстой.")

    def validate_longitude(self, field):
        if (self.latitude.data is None) != (field.data is None):
            raise ValidationError("Өргөрөг, уртрагийг хоёуланг нь оруулах эсвэл хоёуланг нь хоосон үлдээнэ үү.")

    def listing_values(self):
        """Plain values for services.listings."""
        return {
            "title": self.title.data, "description": self.description.data, "location": self.location.data,
            "work_date": self.work_date.data, "start_time": self.start_time.data, "end_time": self.end_time.data,
            "workers_needed": self.workers_needed.data, "wage_per_worker": self.wage_per_worker.data,
            "notes": self.notes.data or "", "public_area": self.public_area.data,
            "latitude": self.latitude.data, "longitude": self.longitude.data,
            "skill_id": int(self.skill.data) if self.skill.data else None,
            "internal_notes": self.internal_notes.data or "",
            "supervisor_name": self.supervisor_name.data or None,
            "supervisor_phone": self.supervisor_phone.data or None,
            "worker_instructions": self.worker_instructions.data or "",
        }


class NewListingForm(ListingForm):
    employer = MnSelectField("Ажил олгогч", invalid_message="Ажил олгогч сонгоно уу.")
    employer_name = StringField("Шинэ ажил олгогчийн нэр", filters=[strip_text],
                                validators=[Optional(), Length(min=2, max=100, message="2-100 тэмдэгт байх ёстой.")])
    employer_phone = StringField("Шинэ ажил олгогчийн утас (зөвхөн оффис)", filters=[normalize_phone])
    employer_company = StringField("Байгууллагын нэр (заавал биш)", filters=[strip_text],
                                   validators=[Optional(), Length(min=2, max=COMPANY_NAME_MAX_LENGTH,
                                                                  message="2-100 тэмдэгт байх ёстой.")])
    publish_now = BooleanField("Хадгалаад шууд нийтлэх")
    submit = SubmitField("Зар үүсгэх")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        employers = db.session.scalars(
            db.select(User).filter_by(role=Role.EMPLOYER, is_active=True).order_by(User.full_name)
        ).all()
        self.employer.choices = [SelectChoice(NEW_EMPLOYER, "+ Шинэ ажил олгогч (доор бөглөнө)")] + [
            SelectChoice(str(u.id), f"{u.company_name or u.full_name} · {u.phone}") for u in employers
        ]

    def validate_employer_phone(self, field):
        if self.employer.data != NEW_EMPLOYER:
            return
        if not is_valid_phone(field.data):
            raise ValidationError("Шинэ ажил олгогчийн утас 8 оронтой тоо байх ёстой.")
        if not self.employer_name.data:
            self.employer_name.errors.append(REQUIRED)
            raise ValidationError("Шинэ ажил олгогчийн нэр, утсыг бөглөнө үү.")


class _TokenForm(FlaskForm):
    request_token = HiddenField(validators=[DataRequired("Маягтыг дахин нээнэ үү.")])

    def fill_token(self):
        if not self.request_token.data:
            self.request_token.data = new_request_token()
        return self


class ReferralForm(_TokenForm):
    phone = StringField("Залгасан хүний утас", filters=[normalize_phone],
                        description="Бүртгэлтэй ажилтны дугаар бол бүртгэлтэй нь автоматаар холбогдоно.")
    name = StringField("Нэр (заавал биш)", filters=[strip_text],
                       validators=[Optional(), Length(max=100, message="100 тэмдэгтээс ихгүй байна.")])
    note = TextAreaField("Дотоод тэмдэглэл (заавал биш)", filters=[strip_text],
                         validators=[Optional(), Length(max=Referral.NOTE_MAX_LENGTH, message="500 тэмдэгтээс ихгүй байна.")])
    contact_shared = BooleanField("Ажил олгогчийн утас, яг хаягийг одоо хэлсэн")
    submit = SubmitField("Илгээлт бүртгэх")

    def validate_phone(self, field):
        if field.data and not is_valid_phone(field.data):
            raise ValidationError("Утасны дугаар 8 оронтой тоо байх ёстой.")
        if not field.data and not self.name.data:
            raise ValidationError("Утасны дугаар эсвэл нэрийг оруулна уу.")


class ReasonForm(FlaskForm):
    reason = TextAreaField("Шалтгаан", filters=[strip_text],
                           validators=[DataRequired("Шалтгаанаа бичнэ үү."),
                                       Length(min=3, max=300, message="3-300 тэмдэгт байх ёстой.")])
    submit = SubmitField("Хадгалах")


class PauseForm(FlaskForm):
    note = StringField("Тайлбар (заавал биш)", filters=[strip_text],
                       validators=[Optional(), Length(max=300, message="300 тэмдэгтээс ихгүй байна.")])
    submit = SubmitField("Түр зогсоох")


class PlaceReferralForm(FlaskForm):
    agreed_wage = MoneyField("Тохирсон цалин (₮)", validators=[InputRequired(REQUIRED)])
    submit = SubmitField("Ажилд орсон — томилох")
