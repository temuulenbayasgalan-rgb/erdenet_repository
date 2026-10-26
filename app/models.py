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
    def positions_remaining(self):
        return max(self.workers_needed - len(self.active_assignments), 0)

    @property
    def is_staffable(self):
        """Accepted by the office and not in the past: workers may be added."""
        return self.status == JobStatus.ACCEPTED and self.work_date >= office_today()

    @property
    def accepting_applications(self):
        return self.is_staffable and self.positions_remaining > 0

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
                assignment.cancel(changed_by)

    def __repr__(self):
        return f"<Job {self.id} {self.status}>"


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

    LABELS = {
        PENDING: "Хянагдаж байна",
        APPROVED: "Тэнцсэн",
        REJECTED: "Тэнцээгүй",
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

    @property
    def status_label(self):
        return ApplicationStatus.LABELS[self.status]

    @property
    def status_badge(self):
        return ApplicationStatus.BADGES[self.status]

    def review(self, new_status, admin):
        """Office decision on a pending application."""
        if self.status != ApplicationStatus.PENDING or new_status not in (
            ApplicationStatus.APPROVED, ApplicationStatus.REJECTED,
        ):
            raise ValueError(f"cannot review {self.status} -> {new_status}")
        self.status = new_status
        self.reviewed_by = admin
        self.reviewed_at = utcnow()


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

    def cancel(self, cancelled_by):
        if self.status != AssignmentStatus.ACTIVE:
            raise ValueError("assignment is not active")
        self.status = AssignmentStatus.CANCELLED
        self.cancelled_at = utcnow()
        self.cancelled_by = cancelled_by

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
