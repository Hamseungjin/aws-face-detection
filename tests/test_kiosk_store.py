import datetime
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest


BACKEND = Path(__file__).resolve().parents[1] / "web-ui" / "backend"
sys.path.insert(0, str(BACKEND))

from kiosk_store import (  # noqa: E402
    InvalidKioskInput,
    KioskStore,
    LockerNotAvailable,
    RetrievalAttemptLimiter,
    RetrievalUnavailable,
    StoreTransactionUnavailable,
    validate_retrieval_code,
)


@pytest.fixture
def database_path(tmp_path):
    return tmp_path / "nested" / "kiosk.sqlite3"


@pytest.fixture
def store(database_path):
    return KioskStore(database_path)


def locker_status(store, locker_id):
    return {
        locker["lockerId"]: locker["status"] for locker in store.list_lockers()
    }[locker_id]



def complete(store, transaction_id, face_id="face-id-test"):
    return store.complete_store(
        transaction_id,
        rekognition_collection_id="kiosk-faces-test",
        rekognition_face_id=face_id,
    )


def test_initialization_is_idempotent_and_preserves_state(database_path):
    first = KioskStore(database_path)
    reservation = first.reserve_locker("04")
    second = KioskStore(database_path)

    assert locker_status(second, "04") == "RESERVED"
    assert second.get_transaction(reservation["transactionId"])["status"] == "RESERVED"
    assert len(second.list_lockers()) == 12


def test_seed_locker_states(store):
    states = {locker["lockerId"]: locker["status"] for locker in store.list_lockers()}

    assert [number for number, state in states.items() if state == "AVAILABLE"] == [
        "01", "02", "04", "05", "07", "09", "10", "12"
    ]
    assert [number for number, state in states.items() if state == "OCCUPIED"] == [
        "03", "06", "11"
    ]
    assert [number for number, state in states.items() if state == "DISABLED"] == ["08"]


def test_available_locker_can_be_reserved(store):
    result = store.reserve_locker("04")

    assert result["lockerId"] == "04"
    assert result["status"] == "RESERVED"
    assert locker_status(store, "04") == "RESERVED"
    assert store.get_transaction(result["transactionId"])["status"] == "RESERVED"


@pytest.mark.parametrize("locker_id", ["03", "08"])
def test_occupied_or_disabled_locker_cannot_be_reserved(store, locker_id):
    with pytest.raises(LockerNotAvailable) as error:
        store.reserve_locker(locker_id)

    assert error.value.code == "LOCKER_NOT_AVAILABLE"


def test_already_reserved_locker_cannot_be_reserved(store):
    store.reserve_locker("04")

    with pytest.raises(LockerNotAvailable):
        store.reserve_locker("04")


def test_two_clients_cannot_reserve_same_locker(database_path):
    KioskStore(database_path)
    barrier = Barrier(2)

    def reserve_from_client():
        client_store = KioskStore(database_path)
        barrier.wait()
        try:
            return ("SUCCESS", client_store.reserve_locker("04")["transactionId"])
        except LockerNotAvailable as error:
            return (error.code, None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: reserve_from_client(), range(2)))

    assert [result[0] for result in results].count("SUCCESS") == 1
    assert [result[0] for result in results].count("LOCKER_NOT_AVAILABLE") == 1
    with sqlite3.connect(database_path) as connection:
        active_count = connection.execute(
            """
            SELECT COUNT(*) FROM transactions
            WHERE locker_id = ? AND status IN ('RESERVED', 'STORED', 'RETRIEVING')
            """,
            ("04",),
        ).fetchone()[0]
    assert active_count == 1


def test_cancelling_reserved_transaction_releases_locker(store):
    reservation = store.reserve_locker("04")

    assert store.cancel_store(reservation["transactionId"]) is True
    assert locker_status(store, "04") == "AVAILABLE"
    assert store.get_transaction(reservation["transactionId"])["status"] == "CANCELLED"


def test_completing_store_persists_codes_and_occupies_locker(store):
    reservation = store.reserve_locker("04")
    result = complete(store, reservation["transactionId"])
    transaction = store.get_transaction(reservation["transactionId"])

    assert locker_status(store, "04") == "OCCUPIED"
    assert transaction["status"] == "STORED"
    assert re.fullmatch(r"\d{8}", result["retrievalCode"])
    assert re.fullmatch(r"PAY-[0-9A-F]{6}", result["mockPaymentId"])
    assert result["retrievalCode"] != result["mockPaymentId"]
    assert transaction["amount"] == 2000


def test_completing_store_twice_returns_same_persisted_result(store, database_path):
    reservation = store.reserve_locker("04")

    first = complete(store, reservation["transactionId"])
    second = store.complete_store(reservation["transactionId"])

    assert second == first
    assert second["retrievalCode"] == first["retrievalCode"]
    assert second["mockPaymentId"] == first["mockPaymentId"]
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM transactions WHERE transaction_id = ?",
            (reservation["transactionId"],),
        ).fetchone()[0] == 1


def test_completing_cancelled_store_is_rejected(store):
    reservation = store.reserve_locker("04")
    store.cancel_store(reservation["transactionId"])

    with pytest.raises(StoreTransactionUnavailable):
        store.complete_store(reservation["transactionId"])


def test_completing_expired_store_is_rejected(database_path):
    now = datetime.datetime(2026, 8, 9, tzinfo=datetime.timezone.utc)
    clock_value = [now]
    store = KioskStore(database_path, clock=lambda: clock_value[0])
    reservation = store.reserve_locker("04")
    clock_value[0] = now + datetime.timedelta(seconds=121)

    with pytest.raises(StoreTransactionUnavailable):
        store.complete_store(reservation["transactionId"])


def test_completing_retrieved_store_is_rejected(store):
    stored = complete(store, store.reserve_locker("04")["transactionId"])
    retrieving = store.start_retrieval(stored["retrievalCode"])
    store.complete_retrieval(retrieving["transactionId"])

    with pytest.raises(StoreTransactionUnavailable):
        store.complete_store(stored["transactionId"])


def test_retrieval_code_collision_is_regenerated(store, monkeypatch):
    first = complete(store, store.reserve_locker("04")["transactionId"])
    second_reservation = store.reserve_locker("05")
    candidates = iter([first["retrievalCode"], "12345678"])
    monkeypatch.setattr(store, "_new_retrieval_code", lambda: next(candidates))

    second = complete(store, second_reservation["transactionId"])

    assert second["retrievalCode"] == "12345678"
    assert second["retrievalCode"] != first["retrievalCode"]


def test_retrieval_code_resolves_correct_transaction_and_starts_retrieval(store):
    first = complete(store, store.reserve_locker("04")["transactionId"])
    second = complete(store, store.reserve_locker("05")["transactionId"])

    result = store.start_retrieval(first["retrievalCode"])

    assert result["transactionId"] == first["transactionId"]
    assert result["lockerId"] == "04"
    assert result["additionalFee"] == 0
    assert store.get_transaction(first["transactionId"])["status"] == "RETRIEVING"
    assert store.get_transaction(second["transactionId"])["status"] == "STORED"


def test_second_retrieval_attempt_fails_while_retrieving(store):
    stored = complete(store, store.reserve_locker("04")["transactionId"])
    store.start_retrieval(stored["retrievalCode"])

    with pytest.raises(RetrievalUnavailable) as error:
        store.start_retrieval(stored["retrievalCode"])

    assert error.value.code == "RETRIEVAL_UNAVAILABLE"


def test_retrieving_within_lease_remains_retrieving(database_path):
    now = datetime.datetime(2026, 8, 9, tzinfo=datetime.timezone.utc)
    clock_value = [now]
    store = KioskStore(
        database_path, retrieval_seconds=120, clock=lambda: clock_value[0]
    )
    stored = complete(store, store.reserve_locker("04")["transactionId"])
    store.start_retrieval(stored["retrievalCode"])
    clock_value[0] = now + datetime.timedelta(seconds=119)

    assert store.get_transaction_status(stored["transactionId"])["status"] == "RETRIEVING"
    assert locker_status(store, "04") == "OCCUPIED"


def test_stale_retrieving_recovers_to_stored_and_keeps_locker_occupied(database_path):
    now = datetime.datetime(2026, 8, 9, tzinfo=datetime.timezone.utc)
    clock_value = [now]
    store = KioskStore(
        database_path, retrieval_seconds=120, clock=lambda: clock_value[0]
    )
    stored = complete(store, store.reserve_locker("04")["transactionId"])
    store.start_retrieval(stored["retrievalCode"])
    clock_value[0] = now + datetime.timedelta(seconds=120)

    assert store.get_transaction_status(stored["transactionId"])["status"] == "STORED"
    assert locker_status(store, "04") == "OCCUPIED"
    transaction = store.get_transaction(stored["transactionId"])
    assert transaction["retrieval_started_at"] is None
    with sqlite3.connect(database_path) as connection:
        locker = connection.execute(
            "SELECT status, active_transaction_id FROM lockers WHERE locker_id = ?",
            ("04",),
        ).fetchone()
    assert locker == ("OCCUPIED", stored["transactionId"])


def test_retrieval_can_begin_again_after_stale_lease_recovery(database_path):
    now = datetime.datetime(2026, 8, 9, tzinfo=datetime.timezone.utc)
    clock_value = [now]
    store = KioskStore(
        database_path, retrieval_seconds=120, clock=lambda: clock_value[0]
    )
    stored = complete(store, store.reserve_locker("04")["transactionId"])
    first = store.start_retrieval(stored["retrievalCode"])
    clock_value[0] = now + datetime.timedelta(seconds=121)

    second = store.start_retrieval(stored["retrievalCode"])

    assert second["transactionId"] == first["transactionId"]
    assert store.get_transaction_status(stored["transactionId"])["status"] == "RETRIEVING"


def test_retrieval_recovery_returns_only_active_matching_transaction(store):
    first = complete(store, store.reserve_locker("04")["transactionId"])
    second = complete(store, store.reserve_locker("05")["transactionId"])
    store.start_retrieval(first["retrievalCode"])

    recovered = store.recover_retrieval(first["retrievalCode"])

    assert recovered == {
        "transactionId": first["transactionId"],
        "lockerId": "04",
        "additionalFee": 0,
        "status": "RETRIEVING",
    }
    assert second["transactionId"] not in recovered.values()
    assert store.recover_retrieval(second["retrievalCode"]) == {"status": "STORED"}


def test_cancelling_retrieval_returns_transaction_to_stored(store):
    stored = complete(store, store.reserve_locker("04")["transactionId"])
    retrieving = store.start_retrieval(stored["retrievalCode"])

    assert store.cancel_retrieval(retrieving["transactionId"]) is True
    assert store.get_transaction(stored["transactionId"])["status"] == "STORED"
    assert locker_status(store, "04") == "OCCUPIED"


def test_completing_retrieval_releases_correct_locker(store):
    stored = complete(store, store.reserve_locker("04")["transactionId"])
    retrieving = store.start_retrieval(stored["retrievalCode"])

    result = store.complete_retrieval(retrieving["transactionId"])

    assert result["status"] == "RETRIEVED"
    assert store.get_transaction(stored["transactionId"])["status"] == "RETRIEVED"
    assert locker_status(store, "04") == "AVAILABLE"


def test_retrieval_code_cannot_retrieve_completed_transaction(store):
    stored = complete(store, store.reserve_locker("04")["transactionId"])
    retrieving = store.start_retrieval(stored["retrievalCode"])
    store.complete_retrieval(retrieving["transactionId"])

    with pytest.raises(RetrievalUnavailable):
        store.start_retrieval(stored["retrievalCode"])


def test_database_survives_reopening_after_completed_store(database_path):
    first = KioskStore(database_path)
    stored = complete(first, first.reserve_locker("04")["transactionId"])

    reopened = KioskStore(database_path)

    assert locker_status(reopened, "04") == "OCCUPIED"
    assert reopened.get_transaction(stored["transactionId"])["retrieval_code"] == stored["retrievalCode"]


def test_transaction_status_returns_state_appropriate_fields_only(store):
    reserved = store.reserve_locker("04")
    reserved_status = store.get_transaction_status(reserved["transactionId"])

    assert reserved_status["status"] == "RESERVED"
    assert "retrievalCode" not in reserved_status
    assert "mockPaymentId" not in reserved_status

    stored = complete(store, reserved["transactionId"])
    stored_status = store.get_transaction_status(stored["transactionId"])

    assert stored_status == stored
    assert store.get_transaction_status("unrelated-transaction") is None


@pytest.mark.parametrize(
    "retrieval_code", ["1234567", "123456789", "1234ABCD", "１２３４５６７８"]
)
def test_invalid_retrieval_code_format_is_rejected(retrieval_code):
    with pytest.raises(InvalidKioskInput) as error:
        validate_retrieval_code(retrieval_code)

    assert error.value.code == "INVALID_RETRIEVAL_CODE"


def test_failed_retrieval_attempt_limiter_blocks_and_recovers_after_window():
    now = [100.0]
    limiter = RetrievalAttemptLimiter(
        max_failures=5, window_seconds=60, clock=lambda: now[0]
    )

    for _ in range(5):
        assert limiter.retry_after("session-a") == 0
        limiter.record_failure("session-a")

    assert limiter.retry_after("session-a") == 60
    assert limiter.retry_after("session-b") == 0
    now[0] += 61
    assert limiter.retry_after("session-a") == 0


def test_successful_retrieval_clears_failure_limit_state():
    limiter = RetrievalAttemptLimiter(max_failures=5, window_seconds=60)
    for _ in range(5):
        limiter.record_failure("session-a")
    assert limiter.retry_after("session-a") > 0

    limiter.record_success("session-a")

    assert limiter.retry_after("session-a") == 0


def test_expired_reservation_is_released(database_path):
    now = datetime.datetime(2026, 8, 9, tzinfo=datetime.timezone.utc)
    clock_value = [now]
    store = KioskStore(database_path, reservation_seconds=120, clock=lambda: clock_value[0])
    reservation = store.reserve_locker("04")
    clock_value[0] = now + datetime.timedelta(seconds=121)

    assert locker_status(store, "04") == "AVAILABLE"
    assert store.get_transaction(reservation["transactionId"])["status"] == "EXPIRED"


def test_schema_contains_no_biometric_or_base64_fields(store, database_path):
    with sqlite3.connect(database_path) as connection:
        columns = []
        for table in ("lockers", "transactions"):
            columns.extend(row[1].lower() for row in connection.execute(
                "PRAGMA table_info(%s)" % table
            ).fetchall())

    # Pointers to durable reference objects are allowed; embeddings / raw images are not.
    allowed_face_columns = {
        "reference_frame_id",
        "reference_face_s3_key",
        "reference_face_created_at",
        "reference_face_deleted_at",
        "rekognition_collection_id",
        "rekognition_face_id",
        "face_indexed_at",
        "face_deleted_at",
    }
    forbidden_fragments = (
        "embedding", "vector", "image", "base64", "biometric",
        "credential", "password", "api_key",
    )
    for column in columns:
        if column in allowed_face_columns:
            continue
        assert not any(fragment in column for fragment in forbidden_fragments), column
        # Do not store raw face blobs under other names.
        assert "face_embedding" not in column
        assert "face_vector" not in column


def test_reference_face_columns_migrate_on_existing_database(database_path):
    """Existing DBs without reference columns gain them without data loss."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE lockers (
                locker_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                active_transaction_id TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE transactions (
                transaction_id TEXT PRIMARY KEY,
                retrieval_code TEXT UNIQUE,
                locker_id TEXT NOT NULL,
                status TEXT NOT NULL,
                amount INTEGER,
                mock_payment_id TEXT UNIQUE,
                created_at TEXT NOT NULL,
                reserved_at TEXT,
                stored_at TEXT,
                retrieval_started_at TEXT,
                retrieved_at TEXT,
                reservation_expires_at TEXT
            );
            INSERT INTO lockers VALUES ('04', 'AVAILABLE', NULL, '2026-01-01T00:00:00+00:00');
            """
        )

    store = KioskStore(database_path)
    reservation = store.reserve_locker("04")
    result = complete(store, reservation["transactionId"], face_id="face-id-migrate")
    row = store.get_transaction(result["transactionId"])
    assert row["reference_frame_id"] is None
    assert row["reference_face_s3_key"] is None
    assert row["rekognition_face_id"] == "face-id-migrate"
    assert row["face_indexed_at"]
    assert result["referencePresent"] is True


def test_complete_store_binds_collection_face_id(store):
    reservation = store.reserve_locker("04")
    result = store.complete_store(
        reservation["transactionId"],
        rekognition_collection_id="kiosk-faces-test",
        rekognition_face_id="face-id-1",
    )
    row = store.get_transaction(reservation["transactionId"])
    assert result["referencePresent"] is True
    assert row["rekognition_face_id"] == "face-id-1"
    assert row["rekognition_collection_id"] == "kiosk-faces-test"
    assert row["face_indexed_at"]
    assert row["reference_face_s3_key"] is None


def test_complete_store_requires_face_id_and_is_idempotent(store):
    reservation = store.reserve_locker("04")
    with pytest.raises(InvalidKioskInput) as error:
        store.complete_store(reservation["transactionId"])
    assert error.value.code == "FACE_ID_REQUIRED"
    first = complete(store, reservation["transactionId"], face_id="face-id-orig")
    second = complete(store, reservation["transactionId"], face_id="face-id-other")
    assert second["retrievalCode"] == first["retrievalCode"]
    txn = store.get_transaction(reservation["transactionId"])
    assert txn["rekognition_face_id"] == "face-id-orig"
    assert txn["reference_face_s3_key"] is None


def test_get_stored_by_retrieval_code_returns_face_id_for_one_stored(store):
    first = complete(store, store.reserve_locker("04")["transactionId"], face_id="face-id-a")
    complete(store, store.reserve_locker("05")["transactionId"], face_id="face-id-b")
    found = store.get_stored_by_retrieval_code(first["retrievalCode"])
    assert found["transaction_id"] == first["transactionId"]
    assert found["locker_id"] == "04"
    assert found["rekognition_face_id"] == "face-id-a"
    assert found["reference_face_s3_key"] is None
    assert store.get_stored_by_retrieval_code("00000000") is None


def test_mark_face_deleted_after_retrieval(store):
    stored = complete(store, store.reserve_locker("04")["transactionId"])
    retrieving = store.start_retrieval(stored["retrievalCode"])
    store.complete_retrieval(retrieving["transactionId"])
    assert store.mark_face_deleted(stored["transactionId"]) is True
    txn = store.get_transaction(stored["transactionId"])
    assert txn["face_deleted_at"]
    assert store.mark_face_deleted(stored["transactionId"]) is False
