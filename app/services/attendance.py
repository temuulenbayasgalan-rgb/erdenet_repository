"""Office-recorded attendance for assignments."""
from app.extensions import db
from app.models import JobStatus, Role, TransitionError
from app.services import ServiceError


def record_attendance(assignment, new_status, admin):
    """Set attendance on one assignment and append it to the history. Commits."""
    if not admin.has_role(Role.ADMIN):
        raise ServiceError("Зөвхөн админ ирц бүртгэнэ.")
    job = assignment.job
    if not job.attendance_open:
        if job.status in (JobStatus.COMPLETED, JobStatus.CANCELLED):
            raise ServiceError("Дууссан эсвэл цуцлагдсан ажлын ирцийг өөрчлөх боломжгүй.")
        if job.status == JobStatus.PENDING:
            raise ServiceError("Зөвшөөрөгдөөгүй хүсэлтэд ирц бүртгэх боломжгүй.")
        raise ServiceError("Ажлын өдөр болоогүй байна. Ирцийг ажлын өдөр бүртгэнэ.")
    try:
        assignment.set_attendance(new_status, changed_by=admin)
    except TransitionError as error:
        db.session.rollback()
        raise ServiceError(str(error))
    db.session.commit()
