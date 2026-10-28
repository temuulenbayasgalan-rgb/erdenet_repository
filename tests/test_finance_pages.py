"""Finance pages: admin workflow through HTTP, access control, privacy."""
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models import CommissionRate, Job, LedgerEntry, LedgerKind, Role
from app.services.finance import record_payout, record_receipt, summarize
from app.validation import OFFICE_TZ


def form_now():
    moment = datetime.now(OFFICE_TZ) - timedelta(minutes=2)
    return moment.date().isoformat(), moment.strftime("%H:%M")


def money_form(amount, token, prefix="", method="cash", **extra):
    day, clock = form_now()
    p = f"{prefix}-" if prefix else ""
    return {f"{p}amount": str(amount), f"{p}method": method, f"{p}occurred_date": day,
            f"{p}occurred_time": clock, f"{p}reference": "", f"{p}note": "",
            f"{p}request_token": token, **extra}


@pytest.fixture
def admin(admin_user, login):
    login(admin_user.phone)
    return admin_user


@pytest.fixture
def job(make_completed_job):
    return make_completed_job(workers=[("present", 60000), ("present", 80000)], commission=10000)


def summary(job):
    db.session.expire_all()
    return summarize(db.session.get(Job, job.id).finance)


# --- commission page -----------------------------------------------------------------------------

def test_admin_sets_commission_through_page(client, admin):
    response = client.post("/admin/finance/commission", data={"amount": "12 000", "note": "2026 он"},
                           follow_redirects=True)
    html = response.get_data(as_text=True)
    assert "Шимтгэл хадгалагдлаа." in html
    assert "12 000 ₮" in html and "Товчооны админ" in html
    assert CommissionRate.current().amount_per_worker == 12000


@pytest.mark.parametrize("value", ["-1", "abc", "1.5", "", "1000001"])
def test_commission_page_rejects_bad_values(client, admin, value):
    response = client.post("/admin/finance/commission", data={"amount": value})
    assert response.status_code == 400
    assert CommissionRate.current() is None


# --- job finance page ---------------------------------------------------------------------------------

def test_job_finance_page_shows_snapshot(client, admin, job):
    html = client.get(f"/admin/jobs/{job.id}/finance").get_data(as_text=True)
    for text in ("140 000 ₮", "20 000 ₮", "160 000 ₮", "Ажил олгогчийн төлбөр хүлээж буй",
                 "Төлөгдөөгүй", "Ажил олгогчоос орлого бүртгэх", "тооцооны хувилбар 1"):
        assert text in html
    assert "Цалин олгосныг бүртгэх" not in html  # not before full collection


def test_receipt_through_page(client, admin, job):
    response = client.post(f"/admin/jobs/{job.id}/finance/receipts",
                           data=money_form("100 000", "tok-r1", method="bank_transfer", reference="ХААН-55"),
                           follow_redirects=True)
    html = response.get_data(as_text=True)
    assert "Ажил олгогчоос орлого бүртгэгдлээ." in html
    assert "Хэсэгчлэн төлөгдсөн" in html and "ХААН-55" in html
    entry = db.session.scalar(db.select(LedgerEntry))
    assert (entry.amount, entry.method, entry.created_by_id) == (100000, "bank_transfer", admin.id)


def test_double_submit_of_same_form_records_once(client, admin, job):
    data = money_form("50000", "same-token")
    client.post(f"/admin/jobs/{job.id}/finance/receipts", data=data)
    response = client.post(f"/admin/jobs/{job.id}/finance/receipts", data=data, follow_redirects=True)
    assert "аль хэдийн бүртгэгдсэн" in response.get_data(as_text=True)
    assert summary(job).collected == 50000


@pytest.mark.parametrize("amount, message", [
    ("0", "Дүн эерэг бүхэл төгрөг байх ёстой."),
    ("-5000", "Мөнгөн дүнг бүхэл төгрөгөөр оруулна уу."),
    ("1000.50", "Мөнгөн дүнг бүхэл төгрөгөөр оруулна уу."),
    ("", "Энэ талбарыг бөглөнө үү."),
    ("160001", "үлдэгдлээс их"),
])
def test_receipt_page_validation(client, admin, job, amount, message):
    response = client.post(f"/admin/jobs/{job.id}/finance/receipts", data=money_form(amount, f"tok-{amount}"))
    assert response.status_code == 400
    assert message in response.get_data(as_text=True)
    assert summary(job).collected == 0


def test_receipt_without_token_is_rejected(client, admin, job):
    data = money_form("1000", "")
    response = client.post(f"/admin/jobs/{job.id}/finance/receipts", data=data)
    assert response.status_code == 400
    assert summary(job).collected == 0


def test_payout_form_appears_after_full_collection(client, admin, job):
    client.post(f"/admin/jobs/{job.id}/finance/receipts", data=money_form("160000", "t1"))
    html = client.get(f"/admin/jobs/{job.id}/finance").get_data(as_text=True)
    assert "Ажилтны цалин олгоход бэлэн" in html
    assert html.count("Цалин олгосныг бүртгэх</summary>") == 2


def test_payout_through_page(client, admin, job):
    client.post(f"/admin/jobs/{job.id}/finance/receipts", data=money_form("160000", "t1"))
    line = summary(job).lines[0].line
    response = client.post(f"/admin/jobs/{job.id}/finance/lines/{line.id}/payouts",
                           data=money_form("60000", "t2", prefix=f"line{line.id}"), follow_redirects=True)
    assert "Ажилтанд цалин олгосон нь бүртгэгдлээ." in response.get_data(as_text=True)
    assert summary(job).lines[0].paid == 60000


def test_payout_blocked_before_collection_via_page(client, admin, job):
    line = summary(job).lines[0].line
    response = client.post(f"/admin/jobs/{job.id}/finance/lines/{line.id}/payouts",
                           data=money_form("60000", "t2", prefix=f"line{line.id}"))
    assert response.status_code == 400
    assert "бүрэн төлөгдөөгүй" in response.get_data(as_text=True)


def test_payout_url_with_other_jobs_line(client, admin, job, make_completed_job):
    other = make_completed_job(workers=[("present", 50000)], commission=10000, title="Өөр ажил")
    client.post(f"/admin/jobs/{job.id}/finance/receipts", data=money_form("160000", "t1"))
    foreign_line = summarize(other.finance).lines[0].line
    response = client.post(f"/admin/jobs/{job.id}/finance/lines/{foreign_line.id}/payouts",
                           data=money_form("1000", "t3", prefix=f"line{foreign_line.id}"))
    assert response.status_code == 400
    assert "хамаарахгүй" in response.get_data(as_text=True)
    assert db.session.scalar(db.select(db.func.count(LedgerEntry.id)).filter_by(kind=LedgerKind.PAYOUT)) == 0


def test_reversal_through_page_and_history(client, admin, job):
    client.post(f"/admin/jobs/{job.id}/finance/receipts", data=money_form("100000", "t1"))
    entry = db.session.scalar(db.select(LedgerEntry))
    response = client.post(f"/admin/jobs/{job.id}/finance/entries/{entry.id}/reverse",
                           data={f"rev{entry.id}-reason": "Буруу ажилд бүртгэсэн",
                                 f"rev{entry.id}-request_token": "rev-1"}, follow_redirects=True)
    html = response.get_data(as_text=True)
    assert "Гүйлгээ буцаагдлаа." in html
    assert "Орлогын буцаалт" in html and "Буруу ажилд бүртгэсэн" in html
    assert f"№{entry.short_id}-ийг буцаасан" in html
    assert summary(job).collected == 0

    ledger = client.get("/admin/finance/ledger").get_data(as_text=True)
    assert "Ажил олгогчоос орлого" in ledger and "Орлогын буцаалт" in ledger
    filtered = client.get("/admin/finance/ledger?kind=receipt_reversal").get_data(as_text=True)
    assert "Орлогын буцаалт" in filtered


def test_reversal_without_reason_via_page(client, admin, job):
    client.post(f"/admin/jobs/{job.id}/finance/receipts", data=money_form("100000", "t1"))
    entry = db.session.scalar(db.select(LedgerEntry))
    response = client.post(f"/admin/jobs/{job.id}/finance/entries/{entry.id}/reverse",
                           data={f"rev{entry.id}-reason": "", f"rev{entry.id}-request_token": "rev-1"})
    assert response.status_code == 400
    assert "Шалтгаанаа бичнэ үү." in response.get_data(as_text=True)
    assert summary(job).collected == 100000


def test_create_snapshot_through_page(client, admin, make_completed_job, admin_user):
    from app.services import finance as finance_service
    job = make_completed_job(commission=None)
    html = client.get(f"/admin/jobs/{job.id}/finance").get_data(as_text=True)
    assert "Шимтгэлийн хэмжээ тохируулаагүй" in html
    finance_service.set_commission(5000, admin_user)
    html = client.get(f"/admin/jobs/{job.id}/finance").get_data(as_text=True)
    assert "Урьдчилсан тооцоо" in html and "65 000 ₮" in html
    response = client.post(f"/admin/jobs/{job.id}/finance/snapshot", follow_redirects=True)
    assert "Санхүүгийн тооцоо үүслээ." in response.get_data(as_text=True)
    again = client.post(f"/admin/jobs/{job.id}/finance/snapshot", follow_redirects=True)
    assert "аль хэдийн үүссэн" in again.get_data(as_text=True)


def test_needs_review_page_for_job_without_present_workers(client, admin, make_job, finance_employer):
    from app.models import JobStatus
    legacy = make_job(finance_employer, status=JobStatus.COMPLETED)
    html = client.get(f"/admin/jobs/{legacy.id}/finance").get_data(as_text=True)
    assert "Хянах шаардлагатай" in html
    assert "Тэг дүнтэй тооцоо үүсгэхгүй" in html
    assert "/finance/snapshot" not in html


def test_dashboard_groups_and_totals(client, admin, make_completed_job, admin_user):
    unpaid = make_completed_job(workers=[("present", 60000)], commission=10000, title="Төлөөгүй ажил")
    partial = make_completed_job(workers=[("present", 60000)], commission=None, title="Хэсэгчилсэн ажил")
    ready = make_completed_job(workers=[("present", 60000)], commission=None, title="Бэлэн ажил")
    awaiting = make_completed_job(workers=[("present", 60000)], commission=None, title="Тохиргоо хүлээж буй")
    db.session.delete(awaiting.finance.lines[0])
    db.session.delete(awaiting.finance)
    db.session.commit()
    now = datetime.now(OFFICE_TZ) - timedelta(minutes=1)
    record_receipt(partial.id, 30000, "cash", now, admin_user.id)
    record_receipt(ready.id, 70000, "cash", now, admin_user.id)
    record_payout(ready.id, ready.finance.lines[0].id, 20000, "cash", now, admin_user.id)

    html = client.get("/admin/finance").get_data(as_text=True)
    for title in ("Төлөөгүй ажил", "Хэсэгчилсэн ажил", "Бэлэн ажил", "Тохиргоо хүлээж буй"):
        assert title in html
    assert "110 000 ₮" in html   # employer outstanding: 70 000 + 40 000
    assert "40 000 ₮" in html    # available for payouts on the fully paid job
    assert "20 000 ₮" in html    # wages paid
    assert "Сүүлийн гүйлгээ" in html


# --- access and privacy --------------------------------------------------------------------------------

def finance_routes(job, line_id, entry_id):
    return [
        ("get", "/admin/finance"),
        ("get", "/admin/finance/ledger"),
        ("get", "/admin/finance/commission"),
        ("post", "/admin/finance/commission"),
        ("get", f"/admin/jobs/{job.id}/finance"),
        ("post", f"/admin/jobs/{job.id}/finance/snapshot"),
        ("post", f"/admin/jobs/{job.id}/finance/receipts"),
        ("post", f"/admin/jobs/{job.id}/finance/lines/{line_id}/payouts"),
        ("post", f"/admin/jobs/{job.id}/finance/entries/{entry_id}/reverse"),
    ]


@pytest.mark.parametrize("role", [Role.WORKER, Role.EMPLOYER])
def test_non_admins_cannot_touch_finance(client, make_user, login, job, admin_user, role):
    entry = record_receipt(job.id, 160000, "cash", datetime.now(OFFICE_TZ) - timedelta(minutes=1), admin_user.id)
    line_id = job.finance.lines[0].id
    make_user(phone="99440001", role=role)
    login("99440001")
    for method, url in finance_routes(job, line_id, entry.id):
        data = {
            **money_form("1000", "x"),                       # receipt / commission fields
            **money_form("1000", "x2", prefix=f"line{line_id}"),  # payout fields
            f"rev{entry.id}-reason": "хуурамч", f"rev{entry.id}-request_token": "y",
        }
        assert getattr(client, method)(url, data=data).status_code == 403, url
    db.session.expire_all()
    assert db.session.scalar(db.select(db.func.count(LedgerEntry.id))) == 1
    assert CommissionRate.current().amount_per_worker == 10000


def test_assigned_worker_cannot_mark_self_paid(client, login, job, admin_user):
    record_receipt(job.id, 160000, "cash", datetime.now(OFFICE_TZ) - timedelta(minutes=1), admin_user.id)
    line = summary(job).lines[0].line
    login(line.worker.phone)
    response = client.post(f"/admin/jobs/{job.id}/finance/lines/{line.id}/payouts",
                           data=money_form("60000", "self", prefix=f"line{line.id}"))
    assert response.status_code == 403
    assert summary(job).lines[0].paid == 0


def test_anonymous_redirected_from_finance(client, job):
    for method, url in finance_routes(job, 1, 1):
        response = getattr(client, method)(url)
        assert response.status_code == 302, url
        assert response.headers["Location"].startswith("/auth/login")


def test_employer_sees_only_own_totals(client, login, job, finance_employer, admin_user, make_user, make_completed_job):
    record_receipt(job.id, 100000, "cash", datetime.now(OFFICE_TZ) - timedelta(minutes=1), admin_user.id)
    login(finance_employer.phone)
    html = client.get(f"/employer/jobs/{job.id}").get_data(as_text=True)
    assert "Товчоонд төлөх" in html
    assert "160 000 ₮" in html and "100 000 ₮" in html and "60 000 ₮" in html
    assert "Хэсэгчлэн төлөгдсөн" in html
    # no worker wages, phones, commission breakdown or ledger
    for line in job.finance.lines:
        assert line.worker.phone not in html
    assert "Шимтгэл" not in html and "Гүйлгээний түүх" not in html


def test_employer_cannot_see_other_employers_finance(client, login, job, make_user):
    make_user(phone="99330009", role=Role.EMPLOYER)
    login("99330009")
    assert client.get(f"/employer/jobs/{job.id}").status_code == 404


def test_worker_sees_only_own_pay(client, login, job, admin_user):
    now = datetime.now(OFFICE_TZ) - timedelta(minutes=1)
    record_receipt(job.id, 160000, "cash", now, admin_user.id)
    first, second = summary(job).lines
    record_payout(job.id, first.line.id, 25000, "cash", now, admin_user.id)
    login(first.line.worker.phone)
    html = client.get("/worker/assignments").get_data(as_text=True)
    assert "Хэсэгчлэн төлөгдсөн" in html
    assert "олгосон 25 000 ₮" in html and "үлдэгдэл 35 000 ₮" in html
    assert "80 000 ₮" not in html  # the other worker's wage
    assert second.line.worker.full_name not in html
    assert "160 000 ₮" not in html  # employer total


def test_worker_does_not_see_employer_phone_on_finance_pages(client, login, job, finance_employer):
    login(summary(job).lines[0].line.worker.phone)
    html = client.get("/worker/assignments").get_data(as_text=True)
    assert finance_employer.phone not in html and finance_employer.full_name not in html


def test_audit_attribution_for_two_admins(client, login, job, admin_user, make_user):
    second = make_user(phone="99000002", role=Role.ADMIN, full_name="Нягтлан Сараа")
    login(second.phone)
    client.post(f"/admin/jobs/{job.id}/finance/receipts", data=money_form("10000", "t1"))
    entry = db.session.scalar(db.select(LedgerEntry))
    assert entry.created_by_id == second.id
    html = client.get(f"/admin/jobs/{job.id}/finance").get_data(as_text=True)
    assert "Бүртгэсэн: Нягтлан Сараа" in html
    assert f"Тооцоо үүсгэсэн: {admin_user.full_name}" in html
