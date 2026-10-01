"""SQLite under concurrent writers (found by the load test in Phase 22).

SQLite allows ONE writer at a time. A second writer waits by sleeping and retrying, up to
a timeout (5 s) - that is not a fair queue, so under heavy writing an unlucky writer can
wait longer than the timeout and fail with "database is locked". The load test hit exactly
that (1 request in 11,000, after a 6 s wait).

ARTHUR's fix: all database work in the process goes through one lock, so writers queue up
in order inside ARTHUR and SQLite never has to make anyone wait.
"""

from concurrent.futures import ThreadPoolExecutor

from app.database.database import Database
from app.scheduler.reminders import ReminderService
from app.security.audit import AuditLog

WORKERS, ROUNDS = 8, 40


def test_many_threads_writing_at_once_never_see_database_is_locked(tmp_path):
    # A 20 ms SQLite timeout makes the problem show up immediately instead of 1 in 11,000:
    # without the queue this test fails with "database is locked" every time.
    db = Database(tmp_path / "arthur.db", busy_timeout_seconds=0.02)
    db.create_tables()
    reminders, audit = ReminderService(db), AuditLog(db)

    def worker(number: int) -> int:
        done = 0
        for i in range(ROUNDS):
            reminder = reminders.create(f"worker {number} item {i}", "in 2 hours")
            audit.record(
                tool="set_reminder", arguments={"i": i}, permission_level=1,
                decision="allowed", success=True,
            )  # fmt: skip
            assert reminders.cancel(reminder.id)  # read, then write: the risky pattern
            done += 1
        return done

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(worker, range(WORKERS)))  # re-raises any worker's exception

    assert results == [ROUNDS] * WORKERS
    assert reminders.upcoming() == []
    assert len(audit.recent(limit=1000)) == WORKERS * ROUNDS
    db.close()
