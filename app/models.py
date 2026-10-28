"""Database tables."""
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db, login_manager
from app.validation import office_today


def utcnow():
    return datetime.now(timezone.utc)


COMPANY_NAME_MAX_LENGTH = 100
EMPLOYER_FALLBACK_NAME = "Хувь ажил олгогч"


class TransitionError(ValueError):
    """A status change that the business rules don't allow. The message is Mongolian."""


class Role:
    ADMIN = "admin"
    WORKER = "worker"
    EMPLOYER = "employer"
    ALL = (ADMIN, WORKER, EMPLOYER)

    LABELS = {
        ADMIN: "Админ",
        WORKER: "Ажилтан",
        EMPLOYER: "Ажил олгогч",
    }

    # Where each role lands after logging in.
    DASHBOARDS = {
        ADMIN: "admin.index",
        WORKER: "worker.index",
        EMPLOYER: "employer.index",
    }


class User(UserMixin, db.Model):
    __tablename__ = "users"
    __table_args__ = (
        db.CheckConstraint("role IN ('admin', 'worker', 'employer')", name="role"),
    )

    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(8), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    # Overrides UserMixin.is_active: Flask-Login refuses to log in inactive users.
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    # Employers only, optional. Organisation name shown to workers; never a person's name.
    company_name = db.Column(db.String(COMPANY_NAME_MAX_LENGTH))

    worker_profile = db.relationship(
        "WorkerProfile", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def has_role(self, *roles):
        return self.role in roles

    @property
    def role_label(self):
        return Role.LABELS[self.role]

    @property
    def public_name(self):
        """Name shown to employers: 'Бат Дорж' -> 'Б.Дорж' (initial + given name)."""
        parts = self.full_name.split()
        if len(parts) < 2:
            return self.full_name
        return f"{parts[0][0]}.{parts[-1]}"

    @property
    def employer_display_name(self):
        """What workers see as the employer. Falls back to a generic label, never to
        the account holder's name or phone."""
        return self.company_name or EMPLOYER_FALLBACK_NAME

    @property
    def dashboard_endpoint(self):
        return Role.DASHBOARDS[self.role]

    def __repr__(self):
        return f"<User {self.id} {self.role}>"


class Availability:
    AVAILABLE = "available"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    UNAVAILABLE = "unavailable"
    ALL = (AVAILABLE, TEMPORARILY_UNAVAILABLE, UNAVAILABLE)

    LABELS = {
        AVAILABLE: "Ажил хийх боломжтой",
        TEMPORARILY_UNAVAILABLE: "Түр завгүй",
        UNAVAILABLE: "Ажил хийх боломжгүй",
    }

    # Bootstrap badge colours
    BADGES = {
        AVAILABLE: "success",
        TEMPORARILY_UNAVAILABLE: "warning",
        UNAVAILABLE: "secondary",
    }


# Skills created by the migration on a fresh database: (code, Mongolian name).
DEFAULT_SKILLS = (
    ("construction", "Барилгын туслах ажил"),
    ("loading", "Ачаа ачих, буулгах"),
    ("cleaning", "Цэвэрлэгээ"),
    ("furniture_moving", "Тавилга зөөх"),
    ("general_labor", "Ерөнхий туслах ажил"),
)


worker_skills = db.Table(
    "worker_skills",
    db.Column("worker_profile_id", db.Integer, db.ForeignKey("worker_profiles.id"), primary_key=True),
    db.Column("skill_id", db.Integer, db.ForeignKey("skills.id"), primary_key=True),
)


class Skill(db.Model):
    __tablename__ = "skills"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(100), unique=True, nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    # Retired skills stay in the table (old profiles may reference them) but can't be picked.
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())

    @classmethod
    def active(cls):
        return db.session.scalars(
            db.select(cls).filter_by(is_active=True).order_by(cls.sort_order, cls.id)
        ).all()

    def __repr__(self):
        return f"<Skill {self.code}>"


class WorkerProfile(db.Model):
    __tablename__ = "worker_profiles"
    __table_args__ = (
        db.CheckConstraint(
            "availability IN ('available', 'temporarily_unavailable', 'unavailable')",
            name="availability",
        ),
    )

    DESCRIPTION_MAX_LENGTH = 500

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    description = db.Column(db.String(DESCRIPTION_MAX_LENGTH), nullable=False, default="", server_default="")
    availability = db.Column(
        db.String(30), nullable=False, default=Availability.AVAILABLE, server_default=Availability.AVAILABLE
    )
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    user = db.relationship("User", back_populates="worker_profile")
    skills = db.relationship(
        "Skill", secondary=worker_skills, order_by="Skill.sort_order", lazy="selectin"
    )

    @property
    def availability_label(self):
        return Availability.LABELS[self.availability]

    @property
    def availability_badge(self):
        return Availability.BADGES[self.availability]

    def __repr__(self):
        return f"<WorkerProfile user={self.user_id}>"


class JobStatus:
    """Operational status of a job request. Payment status will be tracked separately."""
    PENDING = "pending"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ALL = (PENDING, ACCEPTED, IN_PROGRESS, COMPLETED, CANCELLED)

    LABELS = {
        PENDING: "Хүлээгдэж буй",
        ACCEPTED: "Зөвшөөрсөн",
        IN_PROGRESS: "Ажил явагдаж байна",
        COMPLETED: "Дууссан",
        CANCELLED: "Цуцлагдсан",
    }

    BADGES = {
        PENDING: "warning",
        ACCEPTED: "primary",
        IN_PROGRESS: "info",
        COMPLETED: "success",
        CANCELLED: "secondary",
    }

    # Allowed next statuses. Completed and cancelled are final.
    TRANSITIONS = {
        PENDING: (ACCEPTED, CANCELLED),
        ACCEPTED: (IN_PROGRESS, CANCELLED),
        IN_PROGRESS: (COMPLETED,),
        COMPLETED: (),
        CANCELLED: (),
    }

    # Employers may cancel their own request only before the office accepts it.
    EMPLOYER_CANCELLABLE = (PENDING,)


class Job(db.Model):
    __tablename__ = "jobs"
    __table_args__ = (
        db.CheckConstraint(
            "status IN ('pending', 'accepted', 'in_progress', 'completed', 'cancelled')",
            name="status",
        ),
        db.CheckConstraint("workers_needed > 0", name="workers_needed_positive"),
        db.CheckConstraint("wage_per_worker > 0", name="wage_positive"),
        db.CheckConstraint("end_time > start_time", name="end_after_start"),
    )

    TITLE_MAX_LENGTH = 100
    DESCRIPTION_MAX_LENGTH = 1000
    LOCATION_MAX_LENGTH = 200
    NOTES_MAX_LENGTH = 500

    id = db.Column(db.Integer, primary_key=True)
    employer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    title = db.Column(db.String(TITLE_MAX_LENGTH), nullable=False)
    description = db.Column(db.String(DESCRIPTION_MAX_LENGTH), nullable=False)
    location = db.Column(db.String(LOCATION_MAX_LENGTH), nullable=False)
    work_date = db.Column(db.Date, nullable=False, index=True)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    workers_needed = db.Column(db.Integer, nullable=False)
    # Whole Mongolian tögrög for the whole shift, per worker. Never a float.
    wage_per_worker = db.Column(db.Integer, nullable=False)
    notes = db.Column(db.String(NOTES_MAX_LENGTH), nullable=False, default="", server_default="")
    status = db.Column(
        db.String(20), nullable=False, default=JobStatus.PENDING,
        server_default=JobStatus.PENDING, index=True,
    )
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    employer = db.relationship("User")
    status_changes = db.relationship(
        "JobStatusChange", back_populates="job", order_by="JobStatusChange.id",
        cascade="all, delete-orphan",
    )
    applications = db.relationship("JobApplication", back_populates="job", order_by="JobApplication.id")
    assignments = db.relationship("Assignment", back_populates="job", order_by="Assignment.id")
    referrals = db.relationship("Referral", back_populates="job", order_by="Referral.id")

    @property
    def status_label(self):
        return JobStatus.LABELS[self.status]

    @property
    def status_badge(self):
        return JobStatus.BADGES[self.status]

    @property
    def total_wage(self):
        return self.wage_per_worker * self.workers_needed

    @property
    def next_statuses(self):
        return JobStatus.TRANSITIONS[self.status]

    @property
    def employer_can_cancel(self):
        return self.status in JobStatus.EMPLOYER_CANCELLABLE

    @property
    def active_assignments(self):
        return [a for a in self.assignments if a.status == AssignmentStatus.ACTIVE]

    @property
    def open_referrals(self):
        """Referrals that hold a position but are not (yet) an assignment."""
        return [r for r in self.referrals if r.status == ReferralStatus.REFERRED]

    @property
    def positions_taken(self):
        # A placed referral is counted through its assignment, never twice.
        return len(self.active_assignments) + len(self.open_referrals)

    @property
    def positions_remaining(self):
        return max(self.workers_needed - self.positions_taken, 0)

    @property
    def public_location(self):
        """The approximate area anyone may see. The exact `location` stays private."""
        if self.listing is not None:
            return self.listing.public_area
        return PUBLIC_AREA_FALLBACK

    @property
    def is_publicly_listed(self):
        """Shown on the public board: published, accepted, not past, positions open."""
        return (
            self.listing is not None
            and self.listing.is_published
            and self.status == JobStatus.ACCEPTED
            and self.work_date >= office_today()
            and self.positions_remaining > 0
        )

    @property
    def listing_state(self):
        if self.status == JobStatus.CANCELLED:
            return ListingState.CANCELLED
        if self.status in (JobStatus.IN_PROGRESS, JobStatus.COMPLETED):
            return ListingState.CLOSED
        if self.work_date < office_today():
            return ListingState.EXPIRED
        if self.listing is None or not self.listing.is_published:
            if self.listing is not None and self.listing.published_at is not None:
                return ListingState.PAUSED
            return ListingState.UNPUBLISHED
        if self.status != JobStatus.ACCEPTED:
            return ListingState.UNPUBLISHED
        if self.positions_remaining == 0:
            return ListingState.FILLED
        return ListingState.PUBLISHED

    @property
    def listing_state_label(self):
        return ListingState.LABELS[self.listing_state]

    @property
    def listing_state_badge(self):
        return ListingState.BADGES[self.listing_state]

    @property
    def is_staffable(self):
        """Accepted by the office and not in the past: workers may be added."""
        return self.status == JobStatus.ACCEPTED and self.work_date >= office_today()

    @property
    def accepting_applications(self):
        """Workers may apply only to a published listing that is still open: accepted,
        not past, positions left. Paused, filled, expired, closed and cancelled jobs,
        and jobs the office never published, refuse applications."""
        return self.listing_state == ListingState.PUBLISHED

    @property
    def pending_applications(self):
        return [a for a in self.applications if a.status == ApplicationStatus.PENDING]

    @property
    def open_disputes(self):
        return [d for a in self.assignments for d in a.disputes if d.status == DisputeStatus.OPEN]

    @property
    def attendance_open(self):
        """Attendance can be recorded from the work day on, until the job is completed or cancelled."""
        return (
            self.status in (JobStatus.ACCEPTED, JobStatus.IN_PROGRESS)
            and self.work_date <= office_today()
        )

    def completion_problem(self):
        """Why this job can't be marked completed yet (Mongolian), or None."""
        active = self.active_assignments
        if not active:
            return "Томилогдсон ажилтангүй ажлыг дуусгах боломжгүй."
        unchecked = [a for a in active if a.attendance == AttendanceStatus.NOT_CHECKED]
        if unchecked:
            return f"{len(unchecked)} ажилтны ирц бүртгэгдээгүй байна."
        if not any(a.attendance == AttendanceStatus.PRESENT for a in active):
            return "Ирсэн ажилтан алга. Ажлыг дуусгах бол дор хаяж нэг ажилтан ирсэн байх ёстой."
        if self.open_disputes:
            return f"{len(self.open_disputes)} шийдвэрлээгүй гомдол байна. Эхлээд гомдлыг шийдвэрлэнэ үү."
        return None

    @property
    def completed_change(self):
        """The history entry that completed the job: who and when."""
        return next((c for c in self.status_changes if c.to_status == JobStatus.COMPLETED), None)

    def change_status(self, new_status, changed_by, note=""):
        """Move to `new_status` and record who did it. Raises TransitionError (a ValueError)
        for a move the rules don't allow."""
        if new_status not in self.next_statuses:
            raise TransitionError("Энэ төлөв рүү шилжүүлэх боломжгүй.")
        if new_status == JobStatus.COMPLETED:
            problem = self.completion_problem()
            if problem:
                raise TransitionError(problem)
        self.status_changes.append(JobStatusChange(
            from_status=self.status, to_status=new_status, changed_by=changed_by, note=note,
        ))
        self.status = new_status
        if new_status == JobStatus.CANCELLED:
            # A cancelled job must not keep blocking its workers' time.
            for assignment in self.active_assignments:
                assignment.cancel(changed_by, "Ажил цуцлагдсан")
            for referral in self.referrals:
                if referral.status in ReferralStatus.ACTIVE:
                    referral.close(changed_by, "Ажил цуцлагдсан")

    def __repr__(self):
        return f"<Job {self.id} {self.status}>"


PUBLIC_AREA_FALLBACK = "Эрдэнэт хот"


class ListingState:
    """Where a job stands on the public board (derived, never stored)."""
    UNPUBLISHED = "unpublished"
    PUBLISHED = "published"
    PAUSED = "paused"
    FILLED = "filled"
    EXPIRED = "expired"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    LABELS = {
        UNPUBLISHED: "Нийтлээгүй",
        PUBLISHED: "Нийтлэгдсэн",
        PAUSED: "Түр зогсоосон",
        FILLED: "Орон тоо дүүрсэн",
        EXPIRED: "Хугацаа дууссан",
        CLOSED: "Хаагдсан (ажил эхэлсэн/дууссан)",
        CANCELLED: "Цуцлагдсан",
    }
    BADGES = {
        UNPUBLISHED: "secondary", PUBLISHED: "success", PAUSED: "warning", FILLED: "info",
        EXPIRED: "dark", CLOSED: "dark", CANCELLED: "danger",
    }


class JobListing(db.Model):
    """The office-managed public side of a job: what the job board may show.

    Kept apart from `jobs` so the public fields are explicit, and so this milestone
    adds a table instead of rebuilding `jobs`. Private details (the exact address in
    jobs.location, employer contact, internal_notes) are never rendered publicly."""
    __tablename__ = "job_listings"
    __table_args__ = (
        # Both empty, or both set and in range. (The IS NOT NULL tests matter: a CHECK
        # whose result is NULL counts as passing, so "lat set, lng empty" would slip through.)
        db.CheckConstraint(
            "(latitude IS NULL AND longitude IS NULL) OR "
            "(latitude IS NOT NULL AND longitude IS NOT NULL "
            "AND latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180)",
            name="coordinates",
        ),
        db.CheckConstraint("length(public_area) > 0", name="public_area_present"),
    )

    PUBLIC_AREA_MAX_LENGTH = 100
    INTERNAL_NOTES_MAX_LENGTH = 500
    SUPERVISOR_NAME_MAX_LENGTH = 100
    INSTRUCTIONS_MAX_LENGTH = 500

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=False, unique=True)
    # Random code used in public URLs instead of the internal job id.
    public_code = db.Column(db.String(16), nullable=False, unique=True)
    public_area = db.Column(db.String(PUBLIC_AREA_MAX_LENGTH), nullable=False)
    # Optional public pin: the office places it near the site, and the board rounds it.
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    skill_id = db.Column(db.Integer, db.ForeignKey("skills.id"))
    internal_notes = db.Column(db.String(INTERNAL_NOTES_MAX_LENGTH), nullable=False, default="", server_default="")
    # Shown only to workers whose placement is confirmed, never on public pages.
    supervisor_name = db.Column(db.String(SUPERVISOR_NAME_MAX_LENGTH))
    supervisor_phone = db.Column(db.String(8))
    worker_instructions = db.Column(db.String(INSTRUCTIONS_MAX_LENGTH), nullable=False, default="", server_default="")
    is_published = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    published_at = db.Column(db.DateTime(timezone=True))
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    job = db.relationship("Job", backref=db.backref("listing", uselist=False))
    skill = db.relationship("Skill")
    updated_by = db.relationship("User")
    changes = db.relationship("ListingChange", back_populates="listing", order_by="ListingChange.id")

    # Public pins are rounded to about 100 m.
    PUBLIC_COORDINATE_DECIMALS = 3

    @property
    def has_pin(self):
        return self.latitude is not None and self.longitude is not None

    @property
    def public_latitude(self):
        return round(self.latitude, self.PUBLIC_COORDINATE_DECIMALS) if self.has_pin else None

    @property
    def public_longitude(self):
        return round(self.longitude, self.PUBLIC_COORDINATE_DECIMALS) if self.has_pin else None


class ListingChange(db.Model):
    """Append-only audit of listing actions: who published, paused or edited, and when."""
    __tablename__ = "listing_changes"
    __table_args__ = (
        db.CheckConstraint("action IN ('created', 'edited', 'published', 'paused')", name="action"),
    )

    LABELS = {"created": "Зар үүсгэсэн", "edited": "Засварласан", "published": "Нийтэлсэн", "paused": "Түр зогсоосон"}

    id = db.Column(db.Integer, primary_key=True)
    listing_id = db.Column(db.Integer, db.ForeignKey("job_listings.id"), nullable=False, index=True)
    action = db.Column(db.String(20), nullable=False)
    note = db.Column(db.String(300), nullable=False, default="", server_default="")
    changed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    listing = db.relationship("JobListing", back_populates="changes")
    changed_by = db.relationship("User")

    @property
    def action_label(self):
        return self.LABELS[self.action]


class ReferralStatus:
    REFERRED = "referred"
    PLACED = "placed"
    CANCELLED = "cancelled"
    ALL = (REFERRED, PLACED, CANCELLED)
    ACTIVE = (REFERRED, PLACED)
    LABELS = {REFERRED: "Илгээсэн", PLACED: "Ажилд орсон", CANCELLED: "Цуцалсан"}
    BADGES = {REFERRED: "primary", PLACED: "success", CANCELLED: "secondary"}


class Referral(db.Model):
    """The office sent a job seeker to an employer.

    A referral is NOT an assignment, attendance or payment. It holds one position
    while "referred". When the employer confirms the worker (and the worker has an
    account) it becomes "placed" and is linked to a normal Assignment, which then
    carries attendance and finance."""
    __tablename__ = "referrals"
    __table_args__ = (
        db.CheckConstraint("status IN ('referred', 'placed', 'cancelled')", name="status"),
        db.CheckConstraint(
            "worker_id IS NOT NULL OR caller_phone IS NOT NULL OR caller_name IS NOT NULL",
            name="someone_identified",
        ),
        db.CheckConstraint("status != 'placed' OR assignment_id IS NOT NULL", name="placed_has_assignment"),
        # The same person can't hold two active referrals for one job.
        db.Index(
            "uq_referrals_active_job_worker", "job_id", "worker_id", unique=True,
            sqlite_where=db.text("status IN ('referred', 'placed') AND worker_id IS NOT NULL"),
            postgresql_where=db.text("status IN ('referred', 'placed') AND worker_id IS NOT NULL"),
        ),
        db.Index(
            "uq_referrals_active_job_caller_phone", "job_id", "caller_phone", unique=True,
            sqlite_where=db.text("status IN ('referred', 'placed') AND caller_phone IS NOT NULL"),
            postgresql_where=db.text("status IN ('referred', 'placed') AND caller_phone IS NOT NULL"),
        ),
    )

    NOTE_MAX_LENGTH = 500

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=False, index=True)
    # A registered worker, or a caller without an account (name and/or phone only).
    worker_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    caller_name = db.Column(db.String(100))
    caller_phone = db.Column(db.String(8))
    status = db.Column(db.String(20), nullable=False, default=ReferralStatus.REFERRED,
                       server_default=ReferralStatus.REFERRED)
    note = db.Column(db.String(NOTE_MAX_LENGTH), nullable=False, default="", server_default="")  # office only
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    # When (and by whom) the employer's contact details were given to this person.
    contact_shared_at = db.Column(db.DateTime(timezone=True))
    contact_shared_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    assignment_id = db.Column(db.Integer, db.ForeignKey("assignments.id"), unique=True)
    closed_at = db.Column(db.DateTime(timezone=True))
    closed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    close_reason = db.Column(db.String(300), nullable=False, default="", server_default="")
    request_token = db.Column(db.String(64), unique=True)

    job = db.relationship("Job", back_populates="referrals")
    worker = db.relationship("User", foreign_keys=[worker_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    contact_shared_by = db.relationship("User", foreign_keys=[contact_shared_by_id])
    closed_by = db.relationship("User", foreign_keys=[closed_by_id])
    assignment = db.relationship("Assignment", backref=db.backref("referral", uselist=False))

    @property
    def status_label(self):
        return ReferralStatus.LABELS[self.status]

    @property
    def status_badge(self):
        return ReferralStatus.BADGES[self.status]

    @property
    def person_label(self):
        if self.worker is not None:
            return f"{self.worker.full_name} ({self.worker.phone})"
        parts = [p for p in (self.caller_name, self.caller_phone) if p]
        return " · ".join(parts) + " (бүртгэлгүй)"

    def close(self, closed_by, reason):
        """Cancel an active referral (frees its position if it was only 'referred')."""
        if self.status not in ReferralStatus.ACTIVE:
            raise TransitionError("Энэ илгээлт аль хэдийн хаагдсан байна.")
        self.status = ReferralStatus.CANCELLED
        self.closed_at = utcnow()
        self.closed_by = closed_by
        self.close_reason = reason[:300]


class JobStatusChange(db.Model):
    """Append-only history of a job's status. Rows are never edited or deleted."""
    __tablename__ = "job_status_changes"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=False, index=True)
    from_status = db.Column(db.String(20))  # empty for the "request created" entry
    to_status = db.Column(db.String(20), nullable=False)
    changed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    note = db.Column(db.String(500), nullable=False, default="", server_default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    job = db.relationship("Job", back_populates="status_changes")
    changed_by = db.relationship("User")

    @property
    def to_label(self):
        return JobStatus.LABELS[self.to_status]


class ApplicationStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    ALL = (PENDING, APPROVED, REJECTED, WITHDRAWN)

    # "approved" keeps its stored name; since Milestone 8 it means the office accepted
    # the application and created the assignment in the same step.
    LABELS = {
        PENDING: "Хянагдаж байна",
        APPROVED: "Баталгаажсан",
        REJECTED: "Татгалзсан",
        WITHDRAWN: "Татан авсан",
    }

    BADGES = {
        PENDING: "warning",
        APPROVED: "success",
        REJECTED: "secondary",
        WITHDRAWN: "light border",
    }


class JobApplication(db.Model):
    """A worker's request to work on a job. It is not an assignment."""
    __tablename__ = "job_applications"
    __table_args__ = (
        db.UniqueConstraint("job_id", "worker_id", name="uq_job_applications_job_worker"),
        db.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'withdrawn')", name="status",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=False, index=True)
    worker_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    status = db.Column(
        db.String(20), nullable=False, default=ApplicationStatus.PENDING,
        server_default=ApplicationStatus.PENDING,
    )
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime(timezone=True))

    job = db.relationship("Job", back_populates="applications")
    worker = db.relationship("User", foreign_keys=[worker_id])
    reviewed_by = db.relationship("User", foreign_keys=[reviewed_by_id])
    changes = db.relationship("ApplicationChange", back_populates="application", order_by="ApplicationChange.id")

    @property
    def assignment(self):
        """The assignment created when this application was accepted. None for
        approvals made before Milestone 8, which had no link."""
        for change in reversed(self.changes):
            if change.action == ApplicationChange.ACCEPTED:
                return change.assignment
        return None

    @property
    def placement_cancelled(self):
        assignment = self.assignment
        return assignment is not None and assignment.status == AssignmentStatus.CANCELLED

    @property
    def status_label(self):
        if self.status == ApplicationStatus.APPROVED and self.placement_cancelled:
            return "Томилолт цуцлагдсан"
        return ApplicationStatus.LABELS[self.status]

    @property
    def status_badge(self):
        if self.status == ApplicationStatus.APPROVED and self.placement_cancelled:
            return "danger"
        return ApplicationStatus.BADGES[self.status]

    def record(self, action, actor, note="", assignment=None):
        """Append to the audit history (rows are never edited or deleted)."""
        self.changes.append(ApplicationChange(
            action=action, changed_by=actor, note=(note or "")[:300], assignment=assignment,
        ))

    def review(self, new_status, admin):
        """Office decision on a pending application."""
        if self.status != ApplicationStatus.PENDING or new_status not in (
            ApplicationStatus.APPROVED, ApplicationStatus.REJECTED,
        ):
            raise ValueError(f"cannot review {self.status} -> {new_status}")
        self.status = new_status
        self.reviewed_by = admin
        self.reviewed_at = utcnow()


class ApplicationChange(db.Model):
    """Append-only history of an application: who applied, withdrew, accepted,
    rejected or cancelled the placement, and when."""
    __tablename__ = "application_changes"
    __table_args__ = (
        db.CheckConstraint(
            "action IN ('applied', 'withdrawn', 'accepted', 'rejected', 'placement_cancelled')", name="action",
        ),
    )

    APPLIED = "applied"
    WITHDRAWN = "withdrawn"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PLACEMENT_CANCELLED = "placement_cancelled"
    LABELS = {
        APPLIED: "Хүсэлт илгээсэн",
        WITHDRAWN: "Татан авсан",
        ACCEPTED: "Баталгаажсан (томилогдсон)",
        REJECTED: "Татгалзсан",
        PLACEMENT_CANCELLED: "Томилолт цуцлагдсан",
    }

    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("job_applications.id"), nullable=False, index=True)
    action = db.Column(db.String(30), nullable=False)
    note = db.Column(db.String(300), nullable=False, default="", server_default="")
    # Set on "accepted" (the assignment it created) and "placement_cancelled" (the one ended).
    assignment_id = db.Column(db.Integer, db.ForeignKey("assignments.id"), index=True)
    changed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    application = db.relationship("JobApplication", back_populates="changes")
    assignment = db.relationship("Assignment")
    changed_by = db.relationship("User")

    @property
    def action_label(self):
        return self.LABELS[self.action]


class AssignmentStatus:
    ACTIVE = "active"
    CANCELLED = "cancelled"
    ALL = (ACTIVE, CANCELLED)

    LABELS = {ACTIVE: "Томилогдсон", CANCELLED: "Цуцлагдсан"}
    BADGES = {ACTIVE: "success", CANCELLED: "secondary"}


class AttendanceStatus:
    NOT_CHECKED = "not_checked"
    PRESENT = "present"
    ABSENT = "absent"
    EXCUSED = "excused"
    ALL = (NOT_CHECKED, PRESENT, ABSENT, EXCUSED)
    # The office can record these. "Not checked" is only the starting state:
    # once attendance is recorded it can be corrected, but not un-recorded.
    RECORDABLE = (PRESENT, ABSENT, EXCUSED)

    LABELS = {
        NOT_CHECKED: "Шалгаагүй",
        PRESENT: "Ирсэн",
        ABSENT: "Ирээгүй",
        EXCUSED: "Чөлөөтэй",
    }
    BADGES = {
        NOT_CHECKED: "light border",
        PRESENT: "success",
        ABSENT: "danger",
        EXCUSED: "info",
    }


class Assignment(db.Model):
    """The office's final decision that a worker works on a job."""
    __tablename__ = "assignments"
    __table_args__ = (
        db.CheckConstraint("status IN ('active', 'cancelled')", name="status"),
        db.CheckConstraint("agreed_wage > 0", name="agreed_wage_positive"),
        # At most one *active* assignment per worker and job. Cancelled rows stay as history.
        db.Index(
            "uq_assignments_active_job_worker", "job_id", "worker_id", unique=True,
            sqlite_where=db.text("status = 'active'"),
            postgresql_where=db.text("status = 'active'"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=False, index=True)
    worker_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    # Whole tögrög for the shift. Copied from the job by default; the office may agree another amount.
    agreed_wage = db.Column(db.Integer, nullable=False)
    status = db.Column(
        db.String(20), nullable=False, default=AssignmentStatus.ACTIVE,
        server_default=AssignmentStatus.ACTIVE,
    )
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    cancelled_at = db.Column(db.DateTime(timezone=True))
    cancelled_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    cancel_reason = db.Column(db.String(300), nullable=False, default="", server_default="")
    # Current attendance. Who changed it and when lives in attendance_changes.
    # (No CHECK constraint here: adding one to an existing SQLite table would mean
    # rebuilding it. The allowed values are enforced by set_attendance and by the
    # CHECK constraint on attendance_changes.to_status.)
    attendance = db.Column(
        db.String(20), nullable=False, default=AttendanceStatus.NOT_CHECKED,
        server_default=AttendanceStatus.NOT_CHECKED,
    )

    job = db.relationship("Job", back_populates="assignments")
    worker = db.relationship("User", foreign_keys=[worker_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    cancelled_by = db.relationship("User", foreign_keys=[cancelled_by_id])
    attendance_changes = db.relationship(
        "AttendanceChange", back_populates="assignment", order_by="AttendanceChange.id",
    )
    disputes = db.relationship("AttendanceDispute", back_populates="assignment", order_by="AttendanceDispute.id")

    @property
    def status_label(self):
        return AssignmentStatus.LABELS[self.status]

    @property
    def status_badge(self):
        return AssignmentStatus.BADGES[self.status]

    @property
    def attendance_label(self):
        return AttendanceStatus.LABELS[self.attendance]

    @property
    def attendance_badge(self):
        return AttendanceStatus.BADGES[self.attendance]

    @property
    def last_attendance_change(self):
        return self.attendance_changes[-1] if self.attendance_changes else None

    @property
    def open_disputes(self):
        return [d for d in self.disputes if d.status == DisputeStatus.OPEN]

    def cancel(self, cancelled_by, reason):
        if self.status != AssignmentStatus.ACTIVE:
            raise ValueError("assignment is not active")
        self.status = AssignmentStatus.CANCELLED
        self.cancelled_at = utcnow()
        self.cancelled_by = cancelled_by
        self.cancel_reason = (reason or "")[:300]

    def set_attendance(self, new_status, changed_by):
        """Record attendance and append it to the history. Raises TransitionError."""
        if self.status != AssignmentStatus.ACTIVE:
            raise TransitionError("Цуцлагдсан томилолтод ирц бүртгэх боломжгүй.")
        if new_status not in AttendanceStatus.RECORDABLE:
            raise TransitionError("Ирцийн төлөв буруу байна.")
        if new_status == self.attendance:
            raise TransitionError(f"Ирц аль хэдийн «{AttendanceStatus.LABELS[new_status]}» байна.")
        self.attendance_changes.append(AttendanceChange(
            from_status=self.attendance, to_status=new_status, changed_by=changed_by,
        ))
        self.attendance = new_status


class AttendanceChange(db.Model):
    """Append-only history of an assignment's attendance. Rows are never edited or deleted."""
    __tablename__ = "attendance_changes"
    __table_args__ = (
        db.CheckConstraint(
            "from_status IN ('not_checked', 'present', 'absent', 'excused')", name="from_status",
        ),
        db.CheckConstraint("to_status IN ('present', 'absent', 'excused')", name="to_status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey("assignments.id"), nullable=False, index=True)
    from_status = db.Column(db.String(20), nullable=False)
    to_status = db.Column(db.String(20), nullable=False)
    changed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    assignment = db.relationship("Assignment", back_populates="attendance_changes")
    changed_by = db.relationship("User")

    @property
    def to_label(self):
        return AttendanceStatus.LABELS[self.to_status]


class DisputeStatus:
    OPEN = "open"
    RESOLVED = "resolved"
    LABELS = {OPEN: "Шийдвэрлээгүй", RESOLVED: "Шийдвэрлэсэн"}
    BADGES = {OPEN: "danger", RESOLVED: "success"}


class AttendanceDispute(db.Model):
    """A disagreement about attendance or completed work on one assignment, raised by
    the worker, the employer, or the office (for example for a supervisor who phoned).

    Only office staff resolve it, with a written resolution. While one is open the job
    can't be completed, so nobody's claim is finalised without review. Rows are never
    deleted; resolving fills in the resolution fields once."""
    __tablename__ = "attendance_disputes"
    __table_args__ = (
        db.CheckConstraint("status IN ('open', 'resolved')", name="status"),
        db.CheckConstraint("raised_role IN ('worker', 'employer', 'admin')", name="raised_role"),
        db.CheckConstraint("length(message) > 0", name="message_present"),
        db.CheckConstraint(
            "(status = 'open' AND resolved_at IS NULL AND resolved_by_id IS NULL)"
            " OR (status = 'resolved' AND resolved_at IS NOT NULL AND resolved_by_id IS NOT NULL"
            " AND length(resolution) > 0)",
            name="resolution_fields",
        ),
        # One open dispute per person per assignment (this also stops double submits).
        db.Index(
            "uq_attendance_disputes_open_assignment_raiser", "assignment_id", "raised_by_id", unique=True,
            sqlite_where=db.text("status = 'open'"), postgresql_where=db.text("status = 'open'"),
        ),
    )

    TEXT_MAX_LENGTH = 500
    ROLE_LABELS = {"worker": "Ажилтан", "employer": "Ажил олгогч", "admin": "Оффис"}

    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey("assignments.id"), nullable=False, index=True)
    raised_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    raised_role = db.Column(db.String(20), nullable=False)
    message = db.Column(db.String(TEXT_MAX_LENGTH), nullable=False)
    status = db.Column(db.String(20), nullable=False, default=DisputeStatus.OPEN, server_default=DisputeStatus.OPEN)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    resolved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    resolved_at = db.Column(db.DateTime(timezone=True))
    resolution = db.Column(db.String(TEXT_MAX_LENGTH), nullable=False, default="", server_default="")

    assignment = db.relationship("Assignment", back_populates="disputes")
    raised_by = db.relationship("User", foreign_keys=[raised_by_id])
    resolved_by = db.relationship("User", foreign_keys=[resolved_by_id])

    @property
    def status_label(self):
        return DisputeStatus.LABELS[self.status]

    @property
    def status_badge(self):
        return DisputeStatus.BADGES[self.status]

    @property
    def raised_role_label(self):
        return self.ROLE_LABELS[self.raised_role]


class Notification(db.Model):
    """An in-app message for one user. Created in the same transaction as the event."""
    __tablename__ = "notifications"
    __table_args__ = (
        # The same event never notifies the same person twice, even if a request is retried.
        db.UniqueConstraint("recipient_id", "event_key", name="uq_notifications_recipient_event"),
    )

    TITLE_MAX_LENGTH = 100
    MESSAGE_MAX_LENGTH = 300

    id = db.Column(db.Integer, primary_key=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    event_key = db.Column(db.String(100), nullable=False)
    title = db.Column(db.String(TITLE_MAX_LENGTH), nullable=False)
    message = db.Column(db.String(MESSAGE_MAX_LENGTH), nullable=False)
    # An internal path such as "/worker/assignments"; never an external URL.
    link = db.Column(db.String(200))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    read_at = db.Column(db.DateTime(timezone=True))

    recipient = db.relationship("User")

    @property
    def is_read(self):
        return self.read_at is not None

    def mark_read(self):
        if self.read_at is None:
            self.read_at = utcnow()


# --- finance (Milestone 6) ----------------------------------------------------------------
# All money is whole Mongolian tögrög stored as integers. Nothing here is ever updated
# or deleted after it is written: corrections are new, linked reversal entries.

class CommissionRate(db.Model):
    """The office's fixed commission per present worker, as set by an admin.

    Append-only: the newest row is the current rate. Snapshots copy the amount, so a
    later change never touches jobs that already have a snapshot."""
    __tablename__ = "commission_rates"
    __table_args__ = (
        db.CheckConstraint("amount_per_worker >= 0", name="amount_non_negative"),
    )

    MAX_AMOUNT = 1_000_000

    id = db.Column(db.Integer, primary_key=True)
    amount_per_worker = db.Column(db.Integer, nullable=False)
    note = db.Column(db.String(300), nullable=False, default="", server_default="")
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    created_by = db.relationship("User")

    @classmethod
    def current(cls):
        return db.session.scalar(db.select(cls).order_by(cls.id.desc()).limit(1))


class JobFinance(db.Model):
    """Frozen money snapshot of one completed job: who is paid what, and what the
    employer owes. Created once; never recalculated."""
    __tablename__ = "job_finances"
    __table_args__ = (
        db.CheckConstraint("total_wages > 0", name="total_wages_positive"),
        db.CheckConstraint("total_commission >= 0", name="total_commission_non_negative"),
        db.CheckConstraint("commission_per_worker >= 0", name="commission_per_worker_non_negative"),
        db.CheckConstraint("total_due = total_wages + total_commission", name="total_due_is_sum"),
    )

    # Bump if the calculation rule ever changes, so old snapshots stay explainable.
    CALCULATION_VERSION = 1

    id = db.Column(db.Integer, primary_key=True)
    # unique: a job can never get a second snapshot, even from two requests at once.
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=False, unique=True)
    commission_rate_id = db.Column(db.Integer, db.ForeignKey("commission_rates.id"), nullable=False)
    commission_per_worker = db.Column(db.Integer, nullable=False)
    total_wages = db.Column(db.Integer, nullable=False)
    total_commission = db.Column(db.Integer, nullable=False)
    total_due = db.Column(db.Integer, nullable=False)
    calculation_version = db.Column(db.Integer, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    job = db.relationship("Job", backref=db.backref("finance", uselist=False))
    commission_rate = db.relationship("CommissionRate")
    created_by = db.relationship("User")
    lines = db.relationship("JobFinanceLine", back_populates="finance", order_by="JobFinanceLine.id")
    entries = db.relationship("LedgerEntry", back_populates="finance", order_by="LedgerEntry.id")


class JobFinanceLine(db.Model):
    """One present worker in a snapshot: their wage and the commission charged for them."""
    __tablename__ = "job_finance_lines"
    __table_args__ = (
        db.CheckConstraint("wage > 0", name="wage_positive"),
        db.CheckConstraint("commission >= 0", name="commission_non_negative"),
    )

    id = db.Column(db.Integer, primary_key=True)
    finance_id = db.Column(db.Integer, db.ForeignKey("job_finances.id"), nullable=False, index=True)
    # unique: an assignment is paid through exactly one line.
    assignment_id = db.Column(db.Integer, db.ForeignKey("assignments.id"), nullable=False, unique=True)
    worker_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    wage = db.Column(db.Integer, nullable=False)
    commission = db.Column(db.Integer, nullable=False)

    finance = db.relationship("JobFinance", back_populates="lines")
    assignment = db.relationship("Assignment")
    worker = db.relationship("User")


class LedgerKind:
    RECEIPT = "receipt"
    PAYOUT = "payout"
    RECEIPT_REVERSAL = "receipt_reversal"
    PAYOUT_REVERSAL = "payout_reversal"
    ALL = (RECEIPT, PAYOUT, RECEIPT_REVERSAL, PAYOUT_REVERSAL)
    REVERSAL_OF = {RECEIPT: RECEIPT_REVERSAL, PAYOUT: PAYOUT_REVERSAL}

    LABELS = {
        RECEIPT: "Ажил олгогчоос орлого",
        PAYOUT: "Ажилтанд олгосон",
        RECEIPT_REVERSAL: "Орлогын буцаалт",
        PAYOUT_REVERSAL: "Олголтын буцаалт",
    }
    BADGES = {
        RECEIPT: "success",
        PAYOUT: "primary",
        RECEIPT_REVERSAL: "danger",
        PAYOUT_REVERSAL: "danger",
    }


class PaymentMethod:
    CASH = "cash"
    BANK_TRANSFER = "bank_transfer"
    OTHER = "other"
    ALL = (CASH, BANK_TRANSFER, OTHER)
    LABELS = {CASH: "Бэлэн мөнгө", BANK_TRANSFER: "Банкны шилжүүлэг", OTHER: "Бусад"}


class LedgerEntry(db.Model):
    """One posted money movement, or the reversal of one. Never edited or deleted.

    Amounts are always positive; the kind says which way the money went. A reversal
    points at the entry it cancels, and an entry can be reversed only once
    (`reverses_id` is unique)."""
    __tablename__ = "ledger_entries"
    __table_args__ = (
        db.CheckConstraint("amount > 0", name="amount_positive"),
        db.CheckConstraint(
            "kind IN ('receipt', 'payout', 'receipt_reversal', 'payout_reversal')", name="kind",
        ),
        db.CheckConstraint("method IN ('cash', 'bank_transfer', 'other')", name="method"),
        # Reversals name their original and give a reason; postings do neither.
        db.CheckConstraint(
            "(kind IN ('receipt_reversal', 'payout_reversal') AND reverses_id IS NOT NULL AND length(reason) > 0)"
            " OR (kind IN ('receipt', 'payout') AND reverses_id IS NULL)",
            name="reversal_fields",
        ),
        # Payouts (and their reversals) belong to a worker line; receipts don't.
        db.CheckConstraint(
            "(kind IN ('payout', 'payout_reversal') AND line_id IS NOT NULL)"
            " OR (kind IN ('receipt', 'receipt_reversal') AND line_id IS NULL)",
            name="line_matches_kind",
        ),
    )

    REFERENCE_MAX_LENGTH = 100
    NOTE_MAX_LENGTH = 300

    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(32), nullable=False, unique=True)
    kind = db.Column(db.String(20), nullable=False, index=True)
    finance_id = db.Column(db.Integer, db.ForeignKey("job_finances.id"), nullable=False, index=True)
    line_id = db.Column(db.Integer, db.ForeignKey("job_finance_lines.id"), index=True)
    amount = db.Column(db.Integer, nullable=False)
    method = db.Column(db.String(20), nullable=False)
    reference = db.Column(db.String(REFERENCE_MAX_LENGTH), nullable=False, default="", server_default="")
    note = db.Column(db.String(NOTE_MAX_LENGTH), nullable=False, default="", server_default="")
    reason = db.Column(db.String(NOTE_MAX_LENGTH), nullable=False, default="", server_default="")
    reverses_id = db.Column(db.Integer, db.ForeignKey("ledger_entries.id"), unique=True)
    # When the money actually changed hands (entered by the office) ...
    occurred_at = db.Column(db.DateTime(timezone=True), nullable=False)
    # ... and when it was recorded, by whom.
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    # One-time token from the form, so a double submit records only one entry.
    request_token = db.Column(db.String(64), unique=True)

    finance = db.relationship("JobFinance", back_populates="entries")
    line = db.relationship("JobFinanceLine")
    created_by = db.relationship("User")
    reverses = db.relationship("LedgerEntry", remote_side=[id], backref=db.backref("reversed_by", uselist=False))

    @property
    def kind_label(self):
        return LedgerKind.LABELS[self.kind]

    @property
    def kind_badge(self):
        return LedgerKind.BADGES[self.kind]

    @property
    def method_label(self):
        return PaymentMethod.LABELS[self.method]

    @property
    def is_reversal(self):
        return self.kind in (LedgerKind.RECEIPT_REVERSAL, LedgerKind.PAYOUT_REVERSAL)

    @property
    def short_id(self):
        return self.public_id[:8].upper()


@login_manager.user_loader
def load_user(user_id):
    try:
        user = db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None
    # A deactivated user's existing session stops working immediately.
    if user is None or not user.is_active:
        return None
    return user
