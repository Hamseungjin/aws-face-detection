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
    result = store.complete_store(reservation["transactionId"])
    transaction = store.get_transaction(reservation["transactionId"])

    assert locker_status(store, "04") == "OCCUPIED"
    assert transaction["status"] == "STORED"
    assert re.fullmatch(r"\d{8}", result["retrievalCode"])
    assert re.fullmatch(r"PAY-[0-9A-F]{6}", result["mockPaymentId"])
    assert result["retrievalCode"] != result["mockPaymentId"]
    assert transaction["amount"] == 2000


def test_completing_store_twice_returns_same_persisted_result(store, database_path):
    reservation = store.reserve_locker("04")

    first = store.complete_store(reservation["transactionId"])
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
    stored = store.complete_store(store.reserve_locker("04")["transactionId"])
    retrieving = store.start_retrieval(stored["retrievalCode"])
    store.complete_retrieval(retrieving["transactionId"])

    with pytest.raises(StoreTransactionUnavailable):
        store.complete_store(stored["transactionId"])


def test_retrieval_code_collision_is_regenerated(store, monkeypatch):
    first = store.complete_store(store.reserve_locker("04")["transactionId"])
    second_reservation = store.reserve_locker("05")
    candidates = iter([first["retrievalCode"], "12345678"])
    monkeypatch.setattr(store, "_new_retrieval_code", lambda: next(candidates))

    second = store.complete_store(second_reservation["transactionId"])

    assert second["retrievalCode"] == "12345678"
    assert second["retrievalCode"] != first["retrievalCode"]


def test_retrieval_code_resolves_correct_transaction_and_starts_retrieval(store):
    first = store.complete_store(store.reserve_locker("04")["transactionId"])
    second = store.complete_store(store.reserve_locker("05")["transactionId"])

    result = store.start_retrieval(first["retrievalCode"])

    assert result["transactionId"] == first["transactionId"]
    assert result["lockerId"] == "04"
    assert result["additionalFee"] == 0
    assert store.get_transaction(first["transactionId"])["status"] == "RETRIEVING"
    assert store.get_transaction(second["transactionId"])["status"] == "STORED"


def test_second_retrieval_attempt_fails_while_retrieving(store):
    stored = store.complete_store(store.reserve_locker("04")["transactionId"])
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
    stored = store.complete_store(store.reserve_locker("04")["transactionId"])
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
    stored = store.complete_store(store.reserve_locker("04")["transactionId"])
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
    stored = store.complete_store(store.reserve_locker("04")["transactionId"])
    first = store.start_retrieval(stored["retrievalCode"])
    clock_value[0] = now + datetime.timedelta(seconds=121)

    second = store.start_retrieval(stored["retrievalCode"])

    assert second["transactionId"] == first["transactionId"]
    assert store.get_transaction_status(stored["transactionId"])["status"] == "RETRIEVING"


def test_retrieval_recovery_returns_only_active_matching_transaction(store):
    first = store.complete_store(store.reserve_locker("04")["transactionId"])
    second = store.complete_store(store.reserve_locker("05")["transactionId"])
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
    stored = store.complete_store(store.reserve_locker("04")["transactionId"])
    retrieving = store.start_retrieval(stored["retrievalCode"])

    assert store.cancel_retrieval(retrieving["transactionId"]) is True
    assert store.get_transaction(stored["transactionId"])["status"] == "STORED"
    assert locker_status(store, "04") == "OCCUPIED"


def test_completing_retrieval_releases_correct_locker(store):
    stored = store.complete_store(store.reserve_locker("04")["transactionId"])
    retrieving = store.start_retrieval(stored["retrievalCode"])

    result = store.complete_retrieval(retrieving["transactionId"])

    assert result["status"] == "RETRIEVED"
    assert store.get_transaction(stored["transactionId"])["status"] == "RETRIEVED"
    assert locker_status(store, "04") == "AVAILABLE"


def test_retrieval_code_cannot_retrieve_completed_transaction(store):
    stored = store.complete_store(store.reserve_locker("04")["transactionId"])
    retrieving = store.start_retrieval(stored["retrievalCode"])
    store.complete_retrieval(retrieving["transactionId"])

    with pytest.raises(RetrievalUnavailable):
        store.start_retrieval(stored["retrievalCode"])


def test_database_survives_reopening_after_completed_store(database_path):
    first = KioskStore(database_path)
    stored = first.complete_store(first.reserve_locker("04")["transactionId"])

    reopened = KioskStore(database_path)

    assert locker_status(reopened, "04") == "OCCUPIED"
    assert reopened.get_transaction(stored["transactionId"])["retrieval_code"] == stored["retrievalCode"]


def test_transaction_status_returns_state_appropriate_fields_only(store):
    reserved = store.reserve_locker("04")
    reserved_status = store.get_transaction_status(reserved["transactionId"])

    assert reserved_status["status"] == "RESERVED"
    assert "retrievalCode" not in reserved_status
    assert "mockPaymentId" not in reserved_status

    stored = store.complete_store(reserved["transactionId"])
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

    forbidden_fragments = ("image", "face", "biometric", "base64", "credential", "password", "api_key")
    assert not any(fragment in column for column in columns for fragment in forbidden_fragments)
