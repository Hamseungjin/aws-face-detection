"""Small SQLite transaction store for the single-host kiosk demo.

The database intentionally contains only locker and transaction metadata. Identity
documents, face captures, AWS credentials, and API keys never enter this layer.
"""
import datetime
import math
import secrets
import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path


LOCKER_SEED = (
    ("01", "AVAILABLE"),
    ("02", "AVAILABLE"),
    ("03", "OCCUPIED"),
    ("04", "AVAILABLE"),
    ("05", "AVAILABLE"),
    ("06", "OCCUPIED"),
    ("07", "AVAILABLE"),
    ("08", "DISABLED"),
    ("09", "AVAILABLE"),
    ("10", "AVAILABLE"),
    ("11", "OCCUPIED"),
    ("12", "AVAILABLE"),
)


class KioskStoreError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class LockerNotAvailable(KioskStoreError):
    pass


class StoreTransactionUnavailable(KioskStoreError):
    pass


class RetrievalUnavailable(KioskStoreError):
    pass


class InvalidKioskInput(KioskStoreError):
    pass


def validate_transaction_id(transaction_id):
    if not isinstance(transaction_id, str) or not (1 <= len(transaction_id) <= 128):
        raise InvalidKioskInput("INVALID_TRANSACTION_ID")
    return transaction_id


def validate_retrieval_code(retrieval_code):
    if (
        not isinstance(retrieval_code, str)
        or len(retrieval_code) != 8
        or not retrieval_code.isascii()
        or not retrieval_code.isdigit()
    ):
        raise InvalidKioskInput("INVALID_RETRIEVAL_CODE")
    return retrieval_code


class RetrievalAttemptLimiter:
    """Small process-local failed-attempt limiter for the single-host demo."""

    def __init__(self, max_failures=5, window_seconds=60, clock=None):
        self.max_failures = int(max_failures)
        self.window_seconds = int(window_seconds)
        self.clock = clock or time.monotonic
        self._failures = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, scope, now):
        failures = self._failures[scope]
        cutoff = now - self.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            self._failures.pop(scope, None)
            return None
        return failures

    def retry_after(self, scope):
        now = self.clock()
        with self._lock:
            failures = self._prune(scope, now)
            if failures is None or len(failures) < self.max_failures:
                return 0
            return max(1, int(math.ceil(failures[0] + self.window_seconds - now)))

    def record_failure(self, scope):
        now = self.clock()
        with self._lock:
            failures = self._prune(scope, now)
            if failures is None:
                failures = self._failures[scope]
            failures.append(now)

    def record_success(self, scope):
        with self._lock:
            self._failures.pop(scope, None)


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(value):
    return value.astimezone(datetime.timezone.utc).isoformat(timespec="milliseconds")


class KioskStore:
    """Short-lived sqlite3 connections with serialized, atomic write transactions."""

    def __init__(
        self,
        database_path,
        reservation_seconds=120,
        retrieval_seconds=120,
        clock=None,
    ):
        self.database_path = Path(database_path)
        self.reservation_seconds = int(reservation_seconds)
        self.retrieval_seconds = int(retrieval_seconds)
        self.clock = clock or _utc_now
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self):
        connection = sqlite3.connect(
            str(self.database_path), timeout=5.0, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self):
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS lockers (
                    locker_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK (
                        status IN ('AVAILABLE', 'RESERVED', 'OCCUPIED', 'DISABLED')
                    ),
                    active_transaction_id TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (active_transaction_id)
                        REFERENCES transactions(transaction_id)
                        DEFERRABLE INITIALLY DEFERRED
                );

                CREATE TABLE IF NOT EXISTS transactions (
                    transaction_id TEXT PRIMARY KEY,
                    retrieval_code TEXT UNIQUE,
                    locker_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'RESERVED', 'STORED', 'RETRIEVING',
                            'RETRIEVED', 'CANCELLED', 'EXPIRED'
                        )
                    ),
                    amount INTEGER,
                    mock_payment_id TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    reserved_at TEXT,
                    stored_at TEXT,
                    retrieval_started_at TEXT,
                    retrieved_at TEXT,
                    reservation_expires_at TEXT,
                    FOREIGN KEY (locker_id) REFERENCES lockers(locker_id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS one_active_transaction_per_locker
                ON transactions(locker_id)
                WHERE status IN ('RESERVED', 'STORED', 'RETRIEVING');
                """
            )
            now = _iso(self.clock())
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO lockers (
                        locker_id, status, active_transaction_id, updated_at
                    ) VALUES (?, ?, NULL, ?)
                    """,
                    [(locker_id, status, now) for locker_id, status in LOCKER_SEED],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _release_expired(self, connection, now):
        now_text = _iso(now)
        expired = connection.execute(
            """
            SELECT transaction_id, locker_id
            FROM transactions
            WHERE status = ? AND reservation_expires_at <= ?
            """,
            ("RESERVED", now_text),
        ).fetchall()
        for row in expired:
            connection.execute(
                """
                UPDATE transactions
                SET status = ?
                WHERE transaction_id = ? AND status = ?
                """,
                ("EXPIRED", row["transaction_id"], "RESERVED"),
            )
            connection.execute(
                """
                UPDATE lockers
                SET status = ?, active_transaction_id = NULL, updated_at = ?
                WHERE locker_id = ? AND status = ? AND active_transaction_id = ?
                """,
                (
                    "AVAILABLE", now_text, row["locker_id"], "RESERVED",
                    row["transaction_id"],
                ),
            )
        return len(expired)

    def _recover_stale_retrievals(self, connection, now):
        now_text = _iso(now)
        cutoff_text = _iso(
            now - datetime.timedelta(seconds=self.retrieval_seconds)
        )
        stale = connection.execute(
            """
            SELECT t.transaction_id, t.locker_id
            FROM transactions AS t
            JOIN lockers AS l ON l.locker_id = t.locker_id
            WHERE t.status = ?
              AND (t.retrieval_started_at IS NULL OR t.retrieval_started_at <= ?)
              AND l.status = ?
              AND l.active_transaction_id = t.transaction_id
            """,
            ("RETRIEVING", cutoff_text, "OCCUPIED"),
        ).fetchall()
        for row in stale:
            connection.execute(
                """
                UPDATE transactions
                SET status = ?, retrieval_started_at = NULL
                WHERE transaction_id = ? AND status = ?
                """,
                ("STORED", row["transaction_id"], "RETRIEVING"),
            )
            connection.execute(
                """
                UPDATE lockers SET updated_at = ?
                WHERE locker_id = ? AND status = ? AND active_transaction_id = ?
                """,
                (now_text, row["locker_id"], "OCCUPIED", row["transaction_id"]),
            )
        return len(stale)

    def _cleanup(self, connection, now):
        return (
            self._release_expired(connection, now),
            self._recover_stale_retrievals(connection, now),
        )

    def cleanup_expired(self):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                count, _ = self._cleanup(connection, self.clock())
                connection.commit()
                return count
            except Exception:
                connection.rollback()
                raise

    def list_lockers(self):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._cleanup(connection, self.clock())
                rows = connection.execute(
                    "SELECT locker_id, status, updated_at FROM lockers ORDER BY locker_id"
                ).fetchall()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return [
            {
                "lockerId": row["locker_id"],
                "status": row["status"],
                "updatedAt": row["updated_at"],
            }
            for row in rows
        ]

    def reserve_locker(self, locker_id):
        transaction_id = str(uuid.uuid4())
        now = self.clock()
        now_text = _iso(now)
        expires_text = _iso(
            now + datetime.timedelta(seconds=self.reservation_seconds)
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._cleanup(connection, now)
                updated = connection.execute(
                    """
                    UPDATE lockers
                    SET status = ?, active_transaction_id = ?, updated_at = ?
                    WHERE locker_id = ? AND status = ?
                    """,
                    ("RESERVED", transaction_id, now_text, locker_id, "AVAILABLE"),
                )
                if updated.rowcount != 1:
                    raise LockerNotAvailable("LOCKER_NOT_AVAILABLE")
                connection.execute(
                    """
                    INSERT INTO transactions (
                        transaction_id, locker_id, status, created_at, reserved_at,
                        reservation_expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        transaction_id, locker_id, "RESERVED", now_text, now_text,
                        expires_text,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "transactionId": transaction_id,
            "lockerId": locker_id,
            "status": "RESERVED",
            "reservationExpiresAt": expires_text,
        }

    def cancel_store(self, transaction_id):
        validate_transaction_id(transaction_id)
        now = self.clock()
        now_text = _iso(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._cleanup(connection, now)
                row = connection.execute(
                    """
                    SELECT t.locker_id
                    FROM transactions AS t
                    JOIN lockers AS l ON l.locker_id = t.locker_id
                    WHERE t.transaction_id = ? AND t.status = ?
                      AND l.status = ?
                      AND l.active_transaction_id = t.transaction_id
                    """,
                    (transaction_id, "RESERVED", "RESERVED"),
                ).fetchone()
                if not row:
                    connection.commit()
                    return False
                transaction_updated = connection.execute(
                    """
                    UPDATE transactions SET status = ?
                    WHERE transaction_id = ? AND status = ?
                    """,
                    ("CANCELLED", transaction_id, "RESERVED"),
                )
                released = connection.execute(
                    """
                    UPDATE lockers
                    SET status = ?, active_transaction_id = NULL, updated_at = ?
                    WHERE locker_id = ? AND status = ? AND active_transaction_id = ?
                    """,
                    (
                        "AVAILABLE", now_text, row["locker_id"], "RESERVED",
                        transaction_id,
                    ),
                )
                if transaction_updated.rowcount != 1 or released.rowcount != 1:
                    raise StoreTransactionUnavailable("STORE_TRANSACTION_UNAVAILABLE")
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def _new_retrieval_code(self):
        return "%08d" % secrets.randbelow(100_000_000)

    def _new_mock_payment_id(self):
        return "PAY-" + secrets.token_hex(3).upper()

    def complete_store(self, transaction_id, amount=2000):
        validate_transaction_id(transaction_id)
        now = self.clock()
        now_text = _iso(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._cleanup(connection, now)
                relation = connection.execute(
                    """
                    SELECT t.locker_id, t.status, t.retrieval_code,
                           t.mock_payment_id, t.amount, l.status AS locker_status
                    FROM transactions AS t
                    JOIN lockers AS l ON l.locker_id = t.locker_id
                    WHERE t.transaction_id = ?
                      AND t.status IN (?, ?)
                      AND l.status IN (?, ?)
                      AND l.active_transaction_id = t.transaction_id
                    """,
                    (
                        transaction_id, "RESERVED", "STORED",
                        "RESERVED", "OCCUPIED",
                    ),
                ).fetchone()
                if not relation:
                    raise StoreTransactionUnavailable("STORE_TRANSACTION_UNAVAILABLE")

                if relation["status"] == "STORED":
                    if (
                        relation["locker_status"] != "OCCUPIED"
                        or
                        relation["retrieval_code"] is None
                        or relation["mock_payment_id"] is None
                        or relation["amount"] is None
                    ):
                        raise StoreTransactionUnavailable("STORE_TRANSACTION_UNAVAILABLE")
                    connection.commit()
                    return self._stored_result(relation, transaction_id)

                if relation["locker_status"] != "RESERVED":
                    raise StoreTransactionUnavailable("STORE_TRANSACTION_UNAVAILABLE")

                for _ in range(25):
                    retrieval_code = self._new_retrieval_code()
                    mock_payment_id = self._new_mock_payment_id()
                    try:
                        transaction_updated = connection.execute(
                            """
                            UPDATE transactions
                            SET status = ?, retrieval_code = ?, amount = ?,
                                mock_payment_id = ?, stored_at = ?
                            WHERE transaction_id = ? AND status = ?
                            """,
                            (
                                "STORED", retrieval_code, int(amount), mock_payment_id,
                                now_text, transaction_id, "RESERVED",
                            ),
                        )
                        if transaction_updated.rowcount != 1:
                            raise StoreTransactionUnavailable(
                                "STORE_TRANSACTION_UNAVAILABLE"
                            )
                        break
                    except sqlite3.IntegrityError:
                        continue
                else:
                    raise StoreTransactionUnavailable("CODE_GENERATION_FAILED")

                updated = connection.execute(
                    """
                    UPDATE lockers
                    SET status = ?, updated_at = ?
                    WHERE locker_id = ? AND status = ? AND active_transaction_id = ?
                    """,
                    (
                        "OCCUPIED", now_text, relation["locker_id"], "RESERVED",
                        transaction_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise StoreTransactionUnavailable("STORE_TRANSACTION_UNAVAILABLE")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "transactionId": transaction_id,
            "lockerId": relation["locker_id"],
            "retrievalCode": retrieval_code,
            "mockPaymentId": mock_payment_id,
            "amount": int(amount),
            "status": "STORED",
        }

    def _stored_result(self, row, transaction_id):
        return {
            "transactionId": transaction_id,
            "lockerId": row["locker_id"],
            "retrievalCode": row["retrieval_code"],
            "mockPaymentId": row["mock_payment_id"],
            "amount": int(row["amount"]),
            "status": "STORED",
        }

    def start_retrieval(self, retrieval_code):
        validate_retrieval_code(retrieval_code)
        now = self.clock()
        now_text = _iso(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._cleanup(connection, now)
                relation = connection.execute(
                    """
                    SELECT t.transaction_id, t.locker_id
                    FROM transactions AS t
                    JOIN lockers AS l ON l.locker_id = t.locker_id
                    WHERE t.retrieval_code = ?
                      AND t.status = ?
                      AND l.status = ?
                      AND l.active_transaction_id = t.transaction_id
                    """,
                    (retrieval_code, "STORED", "OCCUPIED"),
                ).fetchone()
                if not relation:
                    raise RetrievalUnavailable("RETRIEVAL_UNAVAILABLE")
                updated = connection.execute(
                    """
                    UPDATE transactions
                    SET status = ?, retrieval_started_at = ?
                    WHERE transaction_id = ? AND status = ?
                    """,
                    (
                        "RETRIEVING", now_text, relation["transaction_id"], "STORED",
                    ),
                )
                if updated.rowcount != 1:
                    raise RetrievalUnavailable("RETRIEVAL_UNAVAILABLE")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "transactionId": relation["transaction_id"],
            "lockerId": relation["locker_id"],
            "additionalFee": 0,
            "status": "RETRIEVING",
        }

    def cancel_retrieval(self, transaction_id):
        validate_transaction_id(transaction_id)
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._cleanup(connection, now)
                relation = connection.execute(
                    """
                    SELECT t.locker_id
                    FROM transactions AS t
                    JOIN lockers AS l ON l.locker_id = t.locker_id
                    WHERE t.transaction_id = ? AND t.status = ?
                      AND l.status = ?
                      AND l.active_transaction_id = t.transaction_id
                    """,
                    (transaction_id, "RETRIEVING", "OCCUPIED"),
                ).fetchone()
                if not relation:
                    connection.commit()
                    return False
                updated = connection.execute(
                    """
                    UPDATE transactions
                    SET status = ?, retrieval_started_at = NULL
                    WHERE transaction_id = ? AND status = ?
                    """,
                    ("STORED", transaction_id, "RETRIEVING"),
                )
                connection.commit()
                return updated.rowcount == 1
            except Exception:
                connection.rollback()
                raise

    def complete_retrieval(self, transaction_id):
        validate_transaction_id(transaction_id)
        now = self.clock()
        now_text = _iso(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._cleanup(connection, now)
                relation = connection.execute(
                    """
                    SELECT t.locker_id
                    FROM transactions AS t
                    JOIN lockers AS l ON l.locker_id = t.locker_id
                    WHERE t.transaction_id = ?
                      AND t.status = ?
                      AND l.status = ?
                      AND l.active_transaction_id = t.transaction_id
                    """,
                    (transaction_id, "RETRIEVING", "OCCUPIED"),
                ).fetchone()
                if not relation:
                    raise RetrievalUnavailable("RETRIEVAL_UNAVAILABLE")
                connection.execute(
                    """
                    UPDATE transactions
                    SET status = ?, retrieved_at = ?
                    WHERE transaction_id = ? AND status = ?
                    """,
                    ("RETRIEVED", now_text, transaction_id, "RETRIEVING"),
                )
                updated = connection.execute(
                    """
                    UPDATE lockers
                    SET status = ?, active_transaction_id = NULL, updated_at = ?
                    WHERE locker_id = ? AND status = ? AND active_transaction_id = ?
                    """,
                    (
                        "AVAILABLE", now_text, relation["locker_id"], "OCCUPIED",
                        transaction_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise RetrievalUnavailable("RETRIEVAL_UNAVAILABLE")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "transactionId": transaction_id,
            "lockerId": relation["locker_id"],
            "status": "RETRIEVED",
        }

    def recover_retrieval(self, retrieval_code):
        validate_retrieval_code(retrieval_code)
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._cleanup(connection, now)
                relation = connection.execute(
                    """
                    SELECT t.transaction_id, t.locker_id, t.status
                    FROM transactions AS t
                    JOIN lockers AS l ON l.locker_id = t.locker_id
                    WHERE t.retrieval_code = ?
                      AND t.status IN (?, ?)
                      AND l.status = ?
                      AND l.active_transaction_id = t.transaction_id
                    """,
                    (retrieval_code, "STORED", "RETRIEVING", "OCCUPIED"),
                ).fetchone()
                if not relation:
                    raise RetrievalUnavailable("RETRIEVAL_UNAVAILABLE")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if relation["status"] == "STORED":
            return {"status": "STORED"}
        return {
            "transactionId": relation["transaction_id"],
            "lockerId": relation["locker_id"],
            "additionalFee": 0,
            "status": "RETRIEVING",
        }

    def get_transaction_status(self, transaction_id):
        validate_transaction_id(transaction_id)
        row = self.get_transaction(transaction_id)
        if not row:
            return None
        result = {
            "transactionId": row["transaction_id"],
            "lockerId": row["locker_id"],
            "status": row["status"],
        }
        if row["status"] == "RESERVED":
            result["reservationExpiresAt"] = row["reservation_expires_at"]
        elif row["status"] == "STORED":
            result.update({
                "retrievalCode": row["retrieval_code"],
                "mockPaymentId": row["mock_payment_id"],
                "amount": row["amount"],
            })
        elif row["status"] == "RETRIEVING":
            result["additionalFee"] = 0
        return result

    def get_transaction(self, transaction_id):
        validate_transaction_id(transaction_id)
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._cleanup(connection, now)
                row = connection.execute(
                    "SELECT * FROM transactions WHERE transaction_id = ?",
                    (transaction_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return dict(row) if row else None
