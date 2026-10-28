"""Office referrals: a job seeker called, the office sends them to the employer.

A referral holds one position while "referred". It is not hiring, attendance or
payment. Placing a referred worker (who has an account) creates a normal assignment
through services.staffing.assign_worker, which takes over the referral's position.
"""
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    AssignmentStatus,
    Assignment,
    Job,
    ListingState,
    Referral,
    ReferralStatus,
    Role,
    TransitionError,
    User,
    utcnow,
)
from app.services import ServiceError
from app.services.locking import lock_row
from app.services.staffing import assign_worker, positions_taken
from app.validation import is_valid_phone, normalize_phone


class ReferralError(ServiceError):
    pass


class DuplicateReferral(ReferralError):
    """The same form was submitted twice; the first one was recorded."""


def _check_admin(user):
    if user is None or not user.has_role(Role.ADMIN):
        raise ReferralError("Зөвхөн оффисын админ илгээлт бүртгэнэ.")


def create_referral(job_id, admin_id, phone="", name="", note="", contact_shared=False, request_token=None):
    """Record that the office is sending this caller to the job. Commits."""
    try:
        lock_row(Job, job_id)  # one referral at a time per database: no overfilling
        if request_token and db.session.scalar(select(Referral.id).filter_by(request_token=request_token)):
            raise DuplicateReferral("Энэ илгээлт аль хэдийн бүртгэгдсэн байна.")
        admin = db.session.get(User, admin_id)
        _check_admin(admin)
        job = db.session.get(Job, job_id)
        if job is None:
            raise ReferralError("Ажил олдсонгүй.")
        # Published and accepted, date not passed. A full listing passes here and is
        # refused below with the clearer "positions filled" message.
        if job.listing_state not in (ListingState.PUBLISHED, ListingState.FILLED):
            raise ReferralError("Энэ зар нийтлэгдээгүй, хаагдсан эсвэл хугацаа нь дууссан байна.")

        phone = normalize_phone((phone or "").strip()) or None
        name = (name or "").strip() or None
        note = (note or "").strip()
        if phone is not None and not is_valid_phone(phone):
            raise ReferralError("Утасны дугаар 8 оронтой тоо байх ёстой.")
        if name is not None and len(name) > 100:
            raise ReferralError("Нэр 100 тэмдэгтээс ихгүй байна.")
        if len(note) > Referral.NOTE_MAX_LENGTH:
            raise ReferralError("Тэмдэглэл 500 тэмдэгтээс ихгүй байна.")
        if phone is None and name is None:
            raise ReferralError("Залгасан хүний утасны дугаар эсвэл нэрийг оруулна уу.")

        worker = None
        if phone is not None:
            account = db.session.scalar(select(User).filter_by(phone=phone))
            if account is not None and account.role == Role.WORKER:
                worker = account  # link to the existing worker account
        if worker is not None:
            if not worker.is_active:
                raise ReferralError("Энэ ажилтны бүртгэл идэвхгүй байна.")
            if db.session.scalar(select(Assignment.id).filter_by(
                    job_id=job.id, worker_id=worker.id, status=AssignmentStatus.ACTIVE)):
                raise ReferralError("Энэ ажилтан энэ ажилд аль хэдийн томилогдсон байна.")
            duplicate = select(Referral.id).filter_by(job_id=job.id, worker_id=worker.id)
        else:
            duplicate = select(Referral.id).filter_by(job_id=job.id, caller_phone=phone) if phone else None
        if duplicate is not None and db.session.scalar(
                duplicate.where(Referral.status.in_(ReferralStatus.ACTIVE))):
            raise ReferralError("Энэ хүнийг энэ ажилд аль хэдийн илгээсэн байна.")

        if positions_taken(job.id) >= job.workers_needed:
            raise ReferralError("Орон тоо дүүрсэн байна. Шинэ илгээлт бүртгэх боломжгүй.")

        now = utcnow()
        referral = Referral(
            job=job, worker=worker,
            caller_phone=None if worker else phone,
            caller_name=None if worker else name,
            note=note, created_by=admin, request_token=request_token or None,
        )
        if contact_shared:
            referral.contact_shared_at = now
            referral.contact_shared_by = admin
        job.listing.updated_at = now
        db.session.add(referral)
        db.session.commit()
        return referral
    except IntegrityError:
        db.session.rollback()
        if request_token and db.session.scalar(select(Referral.id).filter_by(request_token=request_token)):
            raise DuplicateReferral("Энэ илгээлт аль хэдийн бүртгэгдсэн байна.")
        raise ReferralError("Энэ хүнийг энэ ажилд аль хэдийн илгээсэн байна.")
    except Exception:
        db.session.rollback()
        raise


def _own_referral(job_id, referral_id):
    referral = db.session.get(Referral, referral_id)
    if referral is None or referral.job_id != job_id:
        raise ReferralError("Илгээлт энэ ажилд хамаарахгүй.")
    return referral


def mark_contact_shared(job_id, referral_id, admin):
    try:
        _check_admin(admin)
        referral = _own_referral(job_id, referral_id)
        if referral.status not in ReferralStatus.ACTIVE:
            raise ReferralError("Хаагдсан илгээлтэд холбоо барих мэдээлэл өгөх боломжгүй.")
        if referral.contact_shared_at is not None:
            raise ReferralError("Холбоо барих мэдээллийг аль хэдийн өгсөн гэж бүртгэсэн байна.")
        referral.contact_shared_at = utcnow()
        referral.contact_shared_by = admin
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def cancel_referral(job_id, referral_id, admin, reason):
    """The person won't go, or the employer declined them: free the position."""
    try:
        _check_admin(admin)
        referral = _own_referral(job_id, referral_id)
        reason = (reason or "").strip()
        if not 3 <= len(reason) <= 300:
            raise ReferralError("Цуцлах шалтгааныг 3-300 тэмдэгтээр бичнэ үү.")
        if referral.status == ReferralStatus.PLACED:
            raise ReferralError("Ажилд орсон илгээлтийг томилолтыг нь цуцалж хаана уу.")
        try:
            referral.close(admin, reason)
        except TransitionError as error:
            raise ReferralError(str(error))
        if referral.job.listing is not None:
            referral.job.listing.updated_at = utcnow()
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def place_referral(job_id, referral_id, admin, agreed_wage):
    """The employer accepted the referred worker: create the assignment (all the usual
    assignment checks apply). Walk-in callers must register as workers first."""
    referral = _own_referral(job_id, referral_id)
    if referral.status != ReferralStatus.REFERRED:
        raise ReferralError("Зөвхөн «Илгээсэн» төлөвтэй илгээлтийг ажилд оруулна.")
    if referral.worker_id is None:
        raise ReferralError(
            "Бүртгэлгүй хүнийг томилох боломжгүй. Тухайн хүн ажилтнаар бүртгүүлсний дараа "
            "түүний утасны дугаараар дахин илгээлт бүртгэнэ үү."
        )
    return assign_worker(job_id, referral.worker_id, agreed_wage, admin.id)
