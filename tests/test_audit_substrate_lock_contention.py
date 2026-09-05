"""A held substrate lock is contention, not corruption (#2906).

Two sprint workers resuming within seconds of one another both open the audit
substrate. SQLite answers the loser with ``database is locked``; the substrate
answered the *operator* with ``SubstrateCorruptError ... run forge audits
rebuild`` — a destructive-looking repair of a store whose ``integrity_check``
said ``ok``. These tests pin the split: wait for a lock that clears, name a lock
that does not as its own transient condition, and keep genuine corruption
reporting exactly as before.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from theforge import worker_budget
from theforge.coordinator import audit_storage
from theforge.coordinator import audit_substrate as sub


@pytest.fixture
def substrate(tmp_path: Path) -> Path:
    """A real, healthy substrate on disk."""
    conn = audit_storage.create_or_open(tmp_path)
    conn.close()
    return audit_storage.substrate_path(tmp_path)


def _exclusive_holder(path: Path) -> sqlite3.Connection:
    """Take (and keep) an EXCLUSIVE lock, blocking every other connection."""
    holder = sqlite3.connect(str(path), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    return holder


def test_lock_that_clears_is_waited_out_not_reported_as_corruption(
    substrate: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The open waits for a sibling to finish and then succeeds."""
    monkeypatch.setattr(audit_storage, "_lock_wait_budget", lambda path: 30.0)
    acquired = threading.Event()
    released = threading.Event()

    def hold_briefly() -> None:
        # SQLite connections are thread-affine, so the lock is taken and
        # released on the same thread.
        holder = _exclusive_holder(substrate)
        acquired.set()
        time.sleep(0.25)
        holder.execute("ROLLBACK")
        holder.close()
        released.set()

    sibling = threading.Thread(target=hold_briefly)
    sibling.start()
    assert acquired.wait(10.0), "the sibling never took the lock"
    try:
        conn = audit_storage._open_validated(substrate)
    finally:
        sibling.join()
    assert released.is_set(), "the lock holder never released"
    # The wait produced a usable connection, not an error about a broken store.
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_lock_that_never_clears_raises_lock_timeout_without_rebuild_advice(
    substrate: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exhausted contention is its own error and never names the rebuild."""
    monkeypatch.setattr(audit_storage, "_lock_wait_budget", lambda path: 0.05)
    holder = _exclusive_holder(substrate)
    try:
        with pytest.raises(audit_storage.SubstrateLockTimeoutError) as exc_info:
            audit_storage._open_validated(substrate)
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    message = str(exc_info.value)
    assert not isinstance(exc_info.value, audit_storage.SubstrateCorruptError)
    assert isinstance(exc_info.value, audit_storage.SubstrateError)
    assert "forge audits rebuild" not in message
    assert "corrupt" not in message.lower()
    assert "locked" in message.lower() or "held" in message.lower()


def test_require_substrate_surfaces_lock_timeout_to_its_callers(
    substrate: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime reader entry point propagates the distinct error."""
    monkeypatch.setattr(audit_storage, "_lock_wait_budget", lambda path: 0.05)
    holder = _exclusive_holder(substrate)
    try:
        with pytest.raises(audit_storage.SubstrateLockTimeoutError):
            audit_storage.require_substrate(tmp_path)
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_write_path_lock_is_contention_not_a_raw_sqlite_error(
    substrate: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``create_or_open`` (the audit *write* path) draws the same distinction."""
    monkeypatch.setattr(audit_storage, "_lock_wait_budget", lambda path: 0.05)
    holder = _exclusive_holder(substrate)
    try:
        with pytest.raises(audit_storage.SubstrateLockTimeoutError) as exc_info:
            audit_storage.create_or_open(tmp_path)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert "forge audits rebuild" not in str(exc_info.value)


def test_corrupt_substrate_still_raises_corrupt_with_rebuild_advice(tmp_path: Path) -> None:
    """A file that is not a database is damage, and still says so."""
    path = audit_storage.substrate_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite database, just garbage" * 50)
    with pytest.raises(sub.SubstrateCorruptError) as exc_info:
        audit_storage.require_substrate(tmp_path)
    assert "forge audits rebuild" in str(exc_info.value)


def test_failed_integrity_check_still_raises_corrupt(
    substrate: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-ok ``integrity_check`` is corruption even though the file opened."""
    real_connect = audit_storage._connect

    class _FailingIntegrityCheck:
        """Delegates everything except ``PRAGMA integrity_check``, which reports damage."""

        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def execute(self, sql: str, *args: object):
            if "integrity_check" in sql:
                return self._conn.execute("SELECT 'row 3 missing from index idx' AS r")
            return self._conn.execute(sql, *args)

        def __getattr__(self, name: str):
            return getattr(self._conn, name)

    def fake_connect(path: Path, budget: float, **kwargs: object):
        return _FailingIntegrityCheck(real_connect(path, budget, **kwargs))

    monkeypatch.setattr(audit_storage, "_connect", fake_connect)
    with pytest.raises(audit_storage.SubstrateCorruptError) as exc_info:
        audit_storage._open_validated(substrate)
    assert "failed integrity check" in str(exc_info.value)


def test_non_lock_database_error_still_raises_corrupt(
    substrate: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only contention is re-routed; every other DatabaseError stays corruption."""

    def boom(path: Path, budget: float, **kwargs: object):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(audit_storage, "_connect", boom)
    with pytest.raises(audit_storage.SubstrateCorruptError):
        audit_storage._open_validated(substrate)


class TestLockErrorRecognition:
    def test_message_fallback_identifies_contention(self) -> None:
        assert audit_storage._is_lock_error(sqlite3.OperationalError("database is locked"))
        assert audit_storage._is_lock_error(sqlite3.OperationalError("database table is locked"))

    def test_other_operational_errors_are_not_contention(self) -> None:
        assert not audit_storage._is_lock_error(sqlite3.OperationalError("no such table: x"))
        assert not audit_storage._is_lock_error(sqlite3.DatabaseError("file is not a database"))

    def test_sqlite_result_code_identifies_contention(self, substrate: Path) -> None:
        """The real error carries SQLITE_BUSY, recognised without message matching."""
        holder = _exclusive_holder(substrate)
        try:
            other = sqlite3.connect(str(substrate), timeout=0.01)
            with pytest.raises(sqlite3.OperationalError) as exc_info:
                other.execute("SELECT COUNT(*) FROM audit_records").fetchone()
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        assert audit_storage._is_lock_error(exc_info.value)


class TestLockWaitBudget:
    """The wait is derived, not a second fixed ceiling (#2906)."""

    def test_budget_grows_with_substrate_size(self, substrate: Path) -> None:
        small = audit_storage._lock_wait_budget(substrate)
        assert small >= audit_storage._LOCK_WAIT_FLOOR_SECONDS

        class _SizedPath:
            """Only ``stat().st_size`` is consulted by the budget."""

            def __init__(self, size: int) -> None:
                self._size = size

            def stat(self):
                return type("S", (), {"st_size": self._size})()

        big = audit_storage._lock_wait_budget(_SizedPath(200 * 1024 * 1024))
        assert big > small
        assert big <= audit_storage._LOCK_WAIT_CEILING_SECONDS

    def test_budget_is_clamped_by_the_enclosing_worker_deadline(
        self, substrate: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Waiting on a lock cannot be what runs a story out of its own ceiling."""
        budget = worker_budget.WorkerBudget(
            slug="issue-2906",
            worker_timeout_seconds=40.0,
            started_at=time.monotonic(),
        )
        monkeypatch.setattr(audit_storage, "_LOCK_WAIT_FLOOR_SECONDS", 600.0)
        monkeypatch.setattr(audit_storage, "_LOCK_WAIT_CEILING_SECONDS", 600.0)
        monkeypatch.setattr(worker_budget, "current_worker_budget", lambda: budget)
        waited = audit_storage._lock_wait_budget(substrate)
        assert waited <= 40.0 - audit_storage._LOCK_WAIT_TAIL_RESERVE_SECONDS + 0.5
        assert waited >= audit_storage._LOCK_WAIT_MINIMUM_SECONDS

    def test_exhausted_worker_deadline_still_waits_a_minimum(
        self, substrate: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        budget = worker_budget.WorkerBudget(
            slug="issue-2906",
            worker_timeout_seconds=0.0,
            started_at=time.monotonic() - 100.0,
        )
        monkeypatch.setattr(worker_budget, "current_worker_budget", lambda: budget)
        assert audit_storage._lock_wait_budget(substrate) == pytest.approx(
            audit_storage._LOCK_WAIT_MINIMUM_SECONDS
        )

    def test_no_enclosing_budget_uses_the_size_term_alone(self, substrate: Path) -> None:
        """CLI callers (``forge audits rebuild``) run outside any worker."""
        assert worker_budget.current_worker_budget() is None
        assert audit_storage._lock_wait_budget(substrate) >= (
            audit_storage._LOCK_WAIT_FLOOR_SECONDS
        )
