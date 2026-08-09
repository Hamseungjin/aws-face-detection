import base64
import os
import pickle
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException


BACKEND = Path(__file__).resolve().parents[1] / "web-ui" / "backend"
sys.path.insert(0, str(BACKEND))

# Importing the production module constructs boto3 clients. These fixed test values
# prevent EC2 metadata/network credential lookup; kiosk tests never call AWS.
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ["AWS_DEFAULT_REGION"] = "ap-northeast-2"
_IMPORT_DB_DIR = tempfile.TemporaryDirectory(prefix="kiosk-api-import-")
os.environ["KIOSK_DB_PATH"] = str(Path(_IMPORT_DB_DIR.name) / "kiosk.sqlite3")
os.environ["WEBUI_SESSION_SECRET"] = "test-session-secret"

import app as kiosk_app  # noqa: E402
from kiosk_store import KioskStore, RetrievalAttemptLimiter  # noqa: E402


class RequestStub:
    def __init__(self, authenticated=True):
        self.session = {"user": "tester"} if authenticated else {}


class KinesisStub:
    def __init__(self):
        self.calls = []

    def put_record(self, **kwargs):
        self.calls.append(kwargs)
        return {"SequenceNumber": str(len(self.calls)), "ShardId": "shardId-000"}


@pytest.fixture
def api(tmp_path, monkeypatch):
    store = KioskStore(tmp_path / "api-kiosk.sqlite3")
    limiter = RetrievalAttemptLimiter(max_failures=5, window_seconds=60)
    monkeypatch.setattr(kiosk_app, "_kiosk_store", store)
    monkeypatch.setattr(kiosk_app, "_retrieval_limiter", limiter)
    return RequestStub(), store


def reserve_and_store(request, locker_id="04"):
    reservation = kiosk_app.kiosk_store_reserve(
        kiosk_app.LockerReserveIn(lockerId=locker_id), request
    )
    return kiosk_app.kiosk_store_complete(
        kiosk_app.TransactionIn(transactionId=reservation["transactionId"]), request
    )


def test_capture_frame_returns_unique_uuid_and_preserves_kinesis_record(monkeypatch):
    kinesis = KinesisStub()
    monkeypatch.setattr(kiosk_app, "_kinesis", kinesis)
    request = RequestStub()
    image = base64.b64encode(b"jpeg-bytes").decode("ascii")

    first = kiosk_app.capture_frame(kiosk_app.FrameIn(imageBase64=image), request)
    second = kiosk_app.capture_frame(kiosk_app.FrameIn(
        imageBase64=image, frameCount=7
    ), request)

    first_uuid = uuid.UUID(first["captureId"])
    second_uuid = uuid.UUID(second["captureId"])
    assert first_uuid.version == second_uuid.version == 4
    assert str(first_uuid) == first["captureId"]
    assert str(second_uuid) == second["captureId"]
    assert first["captureId"] != second["captureId"]

    first_record = pickle.loads(kinesis.calls[0]["Data"])
    second_record = pickle.loads(kinesis.calls[1]["Data"])
    assert set(first_record) == {
        "ApproximateCaptureTime", "FrameCount", "ImageBytes", "CaptureId"
    }
    assert first_record["CaptureId"] == first["captureId"]
    assert second_record["CaptureId"] == second["captureId"]
    assert first_record["FrameCount"] == 0
    assert second_record["FrameCount"] == 7
    assert first_record["ImageBytes"] == bytearray(b"jpeg-bytes")
    assert kinesis.calls[0]["StreamName"] == kiosk_app.config.KINESIS_STREAM


def test_store_completion_and_status_api_are_idempotent(api):
    request, store = api
    reservation = kiosk_app.kiosk_store_reserve(
        kiosk_app.LockerReserveIn(lockerId="04"), request
    )
    body = kiosk_app.TransactionIn(transactionId=reservation["transactionId"])

    first = kiosk_app.kiosk_store_complete(body, request)
    second = kiosk_app.kiosk_store_complete(body, request)
    status = kiosk_app.kiosk_transaction_status(reservation["transactionId"], request)

    assert second == first == status
    assert store.get_transaction(reservation["transactionId"])["status"] == "STORED"


def test_transaction_status_does_not_include_unrelated_or_uncreated_fields(api):
    request, _ = api
    reservation = kiosk_app.kiosk_store_reserve(
        kiosk_app.LockerReserveIn(lockerId="04"), request
    )

    status = kiosk_app.kiosk_transaction_status(reservation["transactionId"], request)

    assert status["status"] == "RESERVED"
    assert "retrievalCode" not in status
    assert "mockPaymentId" not in status
    with pytest.raises(HTTPException) as missing:
        kiosk_app.kiosk_transaction_status("unrelated-transaction", request)
    assert missing.value.status_code == 404
    assert missing.value.detail == "TRANSACTION_NOT_FOUND"

    with pytest.raises(HTTPException) as too_long:
        kiosk_app.kiosk_transaction_status("x" * 129, request)
    assert too_long.value.status_code == 400
    assert too_long.value.detail == "INVALID_TRANSACTION_ID"


@pytest.mark.parametrize("retrieval_code", ["1234567", "123456789", "1234ABCD"])
def test_retrieval_code_format_is_validated_before_lookup(api, retrieval_code):
    request, _ = api

    with pytest.raises(HTTPException) as error:
        kiosk_app.kiosk_retrieve_start(
            kiosk_app.RetrievalStartIn(retrievalCode=retrieval_code), request
        )

    assert error.value.status_code == 400
    assert error.value.detail == "INVALID_RETRIEVAL_CODE"


def test_repeated_failed_retrieval_attempts_return_429(api):
    request, _ = api

    for suffix in range(5):
        with pytest.raises(HTTPException) as unavailable:
            kiosk_app.kiosk_retrieve_start(
                kiosk_app.RetrievalStartIn(
                    retrievalCode="999999%02d" % suffix
                ),
                request,
            )
        assert unavailable.value.status_code == 409
        assert unavailable.value.detail == "RETRIEVAL_UNAVAILABLE"

    with pytest.raises(HTTPException) as limited:
        kiosk_app.kiosk_retrieve_start(
            kiosk_app.RetrievalStartIn(retrievalCode="88888888"), request
        )

    assert limited.value.status_code == 429
    assert limited.value.detail == "RETRIEVAL_RATE_LIMITED"
    assert int(limited.value.headers["Retry-After"]) >= 1


def test_retrieval_start_response_can_be_recovered_without_reclaiming(api):
    request, _ = api
    stored = reserve_and_store(request)
    body = kiosk_app.RetrievalStartIn(retrievalCode=stored["retrievalCode"])

    started = kiosk_app.kiosk_retrieve_start(body, request)
    recovered = kiosk_app.kiosk_retrieve_recover(body, request)

    assert recovered == started
    with pytest.raises(HTTPException) as repeated_start:
        kiosk_app.kiosk_retrieve_start(body, request)
    assert repeated_start.value.status_code == 409


def test_kiosk_transaction_apis_require_session(api):
    _, _ = api
    anonymous = RequestStub(authenticated=False)

    with pytest.raises(HTTPException) as lockers:
        kiosk_app.kiosk_lockers(anonymous)
    with pytest.raises(HTTPException) as status:
        kiosk_app.kiosk_transaction_status("anything", anonymous)
    with pytest.raises(HTTPException) as recovery:
        kiosk_app.kiosk_retrieve_recover(
            kiosk_app.RetrievalStartIn(retrievalCode="12345678"), anonymous
        )

    assert lockers.value.status_code == 401
    assert status.value.status_code == 401
    assert recovery.value.status_code == 401
