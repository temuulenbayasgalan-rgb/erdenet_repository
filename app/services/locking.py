"""Row locking that also works on SQLite.

SQLite ignores SELECT ... FOR UPDATE. Instead, the first *write* in a transaction
takes a lock on the whole database file until commit, so we start every protected
operation with a no-op UPDATE. A second request doing the same waits, then reads the
first one's committed result. On PostgreSQL the same UPDATE locks just that row.
(Verified with two-thread tests in tests/test_staffing_service.py,
tests/test_finance_ledger.py and tests/test_referrals.py.)
"""
from sqlalchemy import update

from app.extensions import db


def lock_row(model, row_id):
    table = model.__table__
    # Set a column to itself. Tables with updated_at use it (setting it explicitly also
    # stops an automatic "onupdate" timestamp from firing); others use the primary key.
    column = table.c.updated_at if "updated_at" in table.c else table.c.id
    db.session.execute(
        update(model).where(table.c.id == row_id).values({column.name: column}),
        execution_options={"synchronize_session": False},
    )
    db.session.expire_all()
