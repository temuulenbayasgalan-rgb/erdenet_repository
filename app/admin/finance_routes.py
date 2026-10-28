"""Admin finance pages. Every rule is enforced in app/services/finance.py; these
routes only collect form input, call the service and show the result."""
from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.orm import selectinload

from app.admin import bp
from app.admin.finance_forms import CommissionForm, LedgerFilterForm, PayoutForm, ReceiptForm, ReversalForm
from app.extensions import db
from app.models import CommissionRate, Job, JobFinance, JobStatus, LedgerEntry, Role
from app.security import role_required
from app.services import finance as finance_service
from app.services.finance import (
    DuplicateSubmission,
    FinanceError,
    FinanceState,
    PaymentState,
    eligible_assignments,
    summarize,
)

RECENT_LIMIT = 20
LEDGER_LIMIT = 200


def _job_or_404(job_id):
    job = db.session.get(Job, job_id)
    if job is None:
        abort(404)
    return job


# --- dashboard ------------------------------------------------------------------------------

@bp.route("/finance")
@role_required(Role.ADMIN)
def finance_dashboard():
    finances = db.session.scalars(
        db.select(JobFinance).options(selectinload(JobFinance.job), selectinload(JobFinance.lines))
    ).all()
    summaries = [summarize(f) for f in finances]

    without_snapshot = db.session.scalars(
        db.select(Job)
        .outerjoin(JobFinance, JobFinance.job_id == Job.id)
        .where(Job.status == JobStatus.COMPLETED, JobFinance.id.is_(None))
        .order_by(Job.work_date.desc())
    ).all()

    totals = {
        "employer_outstanding": sum(s.outstanding for s in summaries),
        "available_for_payouts": sum(s.wages_outstanding for s in summaries if s.fully_collected),
        "wages_paid": sum(s.wages_paid for s in summaries),
        "wages_outstanding": sum(s.wages_outstanding for s in summaries),
        "commission_charged": sum(s.finance.total_commission for s in summaries),
        "commission_collected_jobs": sum(s.finance.total_commission for s in summaries if s.fully_collected),
    }
    groups = {
        "not_paid": [s for s in summaries if s.employer_state == PaymentState.NOT_PAID],
        "partial": [s for s in summaries if s.employer_state == PaymentState.PARTIAL],
        "ready": [s for s in summaries if s.state == FinanceState.READY_FOR_PAYOUTS],
        "settled_count": sum(1 for s in summaries if s.state == FinanceState.SETTLED),
    }
    recent = db.session.scalars(
        db.select(LedgerEntry).order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc()).limit(RECENT_LIMIT)
    ).all()
    return render_template(
        "admin/finance_dashboard.html", totals=totals, groups=groups, recent=recent,
        without_snapshot=[(job, finance_service.finance_state(job)) for job in without_snapshot],
        rate=CommissionRate.current(), FinanceState=FinanceState,
    )


# --- commission -----------------------------------------------------------------------------

@bp.route("/finance/commission", methods=["GET", "POST"])
@role_required(Role.ADMIN)
def commission():
    form = CommissionForm()
    status = 200
    if form.validate_on_submit():
        try:
            finance_service.set_commission(form.amount.data, current_user, form.note.data or "")
        except FinanceError as error:
            flash(str(error), "danger")
            status = 400
        else:
            flash("Шимтгэл хадгалагдлаа. Зөвхөн цаашид үүсэх тооцоонд хэрэглэгдэнэ.", "success")
            return redirect(url_for("admin.commission"))
    elif form.is_submitted():
        flash("Шимтгэл хадгалагдсангүй. Алдааг засна уу.", "danger")
        status = 400
    history = db.session.scalars(db.select(CommissionRate).order_by(CommissionRate.id.desc())).all()
    return render_template("admin/finance_commission.html", form=form, history=history), status


# --- ledger ------------------------------------------------------------------------------------

@bp.route("/finance/ledger")
@role_required(Role.ADMIN)
def ledger():
    form = LedgerFilterForm(formdata=request.args)
    query = db.select(LedgerEntry).options(selectinload(LedgerEntry.finance).selectinload(JobFinance.job))
    if request.args and not form.validate():
        flash("Шүүлтүүрийн утга буруу байна. Бүх гүйлгээг харуулж байна.", "warning")
    elif form.kind.data:
        query = query.filter_by(kind=form.kind.data)
    entries = db.session.scalars(
        query.order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc()).limit(LEDGER_LIMIT)
    ).all()
    return render_template("admin/finance_ledger.html", entries=entries, form=form)


# --- one job --------------------------------------------------------------------------------------

def _render_job_finance(job, receipt_form=None, payout_forms=None, reversal_forms=None, code=200):
    finance = job.finance
    summary = summarize(finance) if finance else None
    payout_forms = payout_forms or {}
    reversal_forms = reversal_forms or {}
    if summary:
        if receipt_form is None:
            receipt_form = ReceiptForm(formdata=None).fill_defaults(amount=summary.outstanding or None)
        for line_summary in summary.lines:
            payout_forms.setdefault(
                line_summary.line.id,
                PayoutForm(formdata=None, prefix=f"line{line_summary.line.id}")
                .fill_defaults(amount=line_summary.remaining or None),
            )
        for entry in finance.entries:
            if not entry.is_reversal and entry.reversed_by is None:
                reversal_forms.setdefault(
                    entry.id, ReversalForm(formdata=None, prefix=f"rev{entry.id}").fill_token(),
                )
    eligible = eligible_assignments(job) if job.status == JobStatus.COMPLETED else []
    rate = CommissionRate.current()
    preview = None
    if finance is None and eligible and rate is not None:
        wages = sum(a.agreed_wage for a in eligible)
        preview = {"wages": wages, "commission": rate.amount_per_worker * len(eligible),
                   "count": len(eligible), "per_worker": rate.amount_per_worker}
        preview["due"] = preview["wages"] + preview["commission"]
    return render_template(
        "admin/job_finance.html", job=job, finance=finance, summary=summary,
        state=finance_service.finance_state(job), FinanceState=FinanceState,
        receipt_form=receipt_form, payout_forms=payout_forms, reversal_forms=reversal_forms,
        eligible=eligible, rate=rate, preview=preview,
    ), code


@bp.route("/jobs/<int:job_id>/finance")
@role_required(Role.ADMIN)
def job_finance(job_id):
    return _render_job_finance(_job_or_404(job_id))


def _after_post(job_id, action, success_message, form, form_slot):
    """Run a finance service call and turn its outcome into a redirect or an error page."""
    try:
        action()
    except DuplicateSubmission as error:
        flash(str(error), "info")
    except FinanceError as error:
        flash(f"Бүртгэгдсэнгүй: {error}", "danger")
        return _render_job_finance(_job_or_404(job_id), code=400, **{form_slot: form})
    else:
        flash(success_message, "success")
    return redirect(url_for("admin.job_finance", job_id=job_id))


@bp.route("/jobs/<int:job_id>/finance/snapshot", methods=["POST"])
@role_required(Role.ADMIN)
def create_job_snapshot(job_id):
    _job_or_404(job_id)
    try:
        finance_service.create_snapshot(job_id, current_user.id)
    except FinanceError as error:
        flash(str(error), "danger")
    else:
        flash("Санхүүгийн тооцоо үүслээ.", "success")
    return redirect(url_for("admin.job_finance", job_id=job_id))


@bp.route("/jobs/<int:job_id>/finance/receipts", methods=["POST"])
@role_required(Role.ADMIN)
def create_receipt(job_id):
    _job_or_404(job_id)
    form = ReceiptForm()
    if not form.validate_on_submit():
        flash("Орлого бүртгэгдсэнгүй. Алдааг засна уу.", "danger")
        return _render_job_finance(_job_or_404(job_id), receipt_form=form, code=400)
    return _after_post(
        job_id,
        lambda: finance_service.record_receipt(
            job_id, form.amount.data, form.method.data, form.occurred_at(), current_user.id,
            reference=form.reference.data, note=form.note.data, request_token=form.request_token.data,
        ),
        "Ажил олгогчоос орлого бүртгэгдлээ.",
        form, "receipt_form",
    )


@bp.route("/jobs/<int:job_id>/finance/lines/<int:line_id>/payouts", methods=["POST"])
@role_required(Role.ADMIN)
def create_payout(job_id, line_id):
    _job_or_404(job_id)
    form = PayoutForm(prefix=f"line{line_id}")
    if not form.validate_on_submit():
        flash("Цалин олголт бүртгэгдсэнгүй. Алдааг засна уу.", "danger")
        return _render_job_finance(_job_or_404(job_id), payout_forms={line_id: form}, code=400)
    return _after_post(
        job_id,
        lambda: finance_service.record_payout(
            job_id, line_id, form.amount.data, form.method.data, form.occurred_at(), current_user.id,
            reference=form.reference.data, note=form.note.data, request_token=form.request_token.data,
        ),
        "Ажилтанд цалин олгосон нь бүртгэгдлээ.",
        {line_id: form}, "payout_forms",
    )


@bp.route("/jobs/<int:job_id>/finance/entries/<int:entry_id>/reverse", methods=["POST"])
@role_required(Role.ADMIN)
def reverse_ledger_entry(job_id, entry_id):
    _job_or_404(job_id)
    form = ReversalForm(prefix=f"rev{entry_id}")
    if not form.validate_on_submit():
        flash("Буцаалт хийгдсэнгүй. Шалтгаанаа бичнэ үү.", "danger")
        return _render_job_finance(_job_or_404(job_id), reversal_forms={entry_id: form}, code=400)
    return _after_post(
        job_id,
        lambda: finance_service.reverse_entry(
            job_id, entry_id, form.reason.data, current_user.id, request_token=form.request_token.data,
        ),
        "Гүйлгээ буцаагдлаа. Анхны бичлэг түүхэнд хадгалагдсан.",
        {entry_id: form}, "reversal_forms",
    )
