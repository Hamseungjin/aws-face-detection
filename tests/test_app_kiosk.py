import base64
import hashlib
import io
import json
import os
import pickle
import sqlite3
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
from kiosk_store import (  # noqa: E402
    KioskStore,
    RetrievalAttemptLimiter,
    StoreTransactionUnavailable,
)
import face_quality  # noqa: E402
import reference_face  # noqa: E402


JPEG_ID = b"\xff\xd8\xff\xe0" + b"id-photo-bytes" + b"\xff\xd9"
JPEG_FACE_A = b"\xff\xd8\xff\xe0" + b"frame-a-bytes" + b"\xff\xd9"
JPEG_FACE_B = b"\xff\xd8\xff\xe0" + b"frame-b-bytes" + b"\xff\xd9"
JPEG_OTHER = b"\xff\xd8\xff\xe0" + b"other-face-bytes" + b"\xff\xd9"


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


class RequestStub:
    def __init__(self, authenticated=True):
        self.session = {"user": "tester"} if authenticated else {}


class KinesisStub:
    def __init__(self):
        self.calls = []

    def put_record(self, **kwargs):
        self.calls.append(kwargs)
        return {"SequenceNumber": str(len(self.calls)), "ShardId": "shardId-000"}


class S3Stub:
    def __init__(self):
        self.objects = {}
        self.copy_calls = []
        self.put_calls = []
        self.delete_calls = []
        self.head_calls = []
        self.get_calls = []

    def head_object(self, Bucket, Key):
        self.head_calls.append({"Bucket": Bucket, "Key": Key})
        if (Bucket, Key) not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}},
                "HeadObject",
            )
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket, Key, Body, ContentType=None, **kwargs):
        payload = bytes(Body)
        self.put_calls.append(
            {"Bucket": Bucket, "Key": Key, "Body": payload, "ContentType": ContentType}
        )
        self.objects[(Bucket, Key)] = payload
        return {}

    def copy_object(self, Bucket, Key, CopySource, MetadataDirective="COPY"):
        self.copy_calls.append(
            {"Bucket": Bucket, "Key": Key, "CopySource": CopySource}
        )
        src = (CopySource["Bucket"], CopySource["Key"])
        self.objects[(Bucket, Key)] = self.objects.get(src, b"frame-bytes")
        return {}

    def delete_object(self, Bucket, Key):
        self.delete_calls.append({"Bucket": Bucket, "Key": Key})
        self.objects.pop((Bucket, Key), None)
        return {}

    def get_object(self, Bucket, Key, **kwargs):
        self.get_calls.append({"Bucket": Bucket, "Key": Key})
        if (Bucket, Key) not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "Not Found"}},
                "GetObject",
            )
        body = self.objects[(Bucket, Key)]
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}


class DynamoTableStub:
    def __init__(self, frames=None):
        self.frames = frames or {}
        self.get_item_calls = []

    def get_item(self, **kwargs):
        self.get_item_calls.append(kwargs)
        item = self.frames.get(kwargs["Key"]["frame_id"])
        return {"Item": item} if item else {}


class DynamoStub:
    def __init__(self, table):
        self.table = table

    def Table(self, name):
        return self.table


class LambdaStub:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        if not self.results:
            result = {
                "success": True,
                "matched": True,
                "similarity": 96.4,
                "threshold": 90,
                "reason": "SIMILARITY_ABOVE_THRESHOLD",
            }
        else:
            result = self.results.pop(0)
        payload = json.dumps(
            {"statusCode": 200, "body": json.dumps(result)}
        ).encode("utf-8")
        return {"Payload": io.BytesIO(payload)}


class RekognitionStub:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []
        self.index_calls = []
        self.search_calls = []
        self.delete_calls = []
        self.index_results = []
        self.search_results = []
        self.delete_results = []
        self.next_face_id = "face-id-store-a"

    def compare_faces(self, **kwargs):
        self.calls.append(kwargs)
        if self.results:
            item = self.results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return {
            "FaceMatches": [{"Similarity": 96.4}],
            "UnmatchedFaces": [],
        }

    def index_faces(self, **kwargs):
        self.index_calls.append(kwargs)
        if self.index_results:
            item = self.index_results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return {
            "FaceRecords": [{"Face": {"FaceId": self.next_face_id}}],
            "UnindexedFaces": [],
        }

    def search_faces_by_image(self, **kwargs):
        self.search_calls.append(kwargs)
        if self.search_results:
            item = self.search_results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return {
            "FaceMatches": [
                {"Face": {"FaceId": self.next_face_id}, "Similarity": 97.2}
            ]
        }

    def delete_faces(self, **kwargs):
        self.delete_calls.append(kwargs)
        if self.delete_results:
            item = self.delete_results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return {"DeletedFaces": list(kwargs.get("FaceIds") or [])}


def _mismatch_rekognition():
    return {
        "FaceMatches": [{"Similarity": 12.0}],
        "UnmatchedFaces": [],
    }


def _quality_pass(image_bytes, **kwargs):
    return face_quality.FaceQualityResult(
        ok=True,
        reason=None,
        metrics={"face_count": 1, "seen_bytes": bytes(image_bytes)},
        detector_name="test",
    )


def _quality_fail(reason, image_bytes=None):
    metrics = {"face_count": 0}
    if image_bytes is not None:
        metrics["seen_bytes"] = bytes(image_bytes)
    return face_quality.FaceQualityResult(
        ok=False, reason=reason, metrics=metrics, detector_name="test"
    )


@pytest.fixture
def api(tmp_path, monkeypatch):
    store = KioskStore(tmp_path / "api-kiosk.sqlite3")
    limiter = RetrievalAttemptLimiter(max_failures=5, window_seconds=60)
    s3 = S3Stub()
    ddb = DynamoStub(DynamoTableStub({}))
    lambda_client = LambdaStub()
    rekognition = RekognitionStub()
    kinesis = KinesisStub()
    monkeypatch.setattr(kiosk_app, "_kiosk_store", store)
    monkeypatch.setattr(kiosk_app, "_retrieval_limiter", limiter)
    monkeypatch.setattr(kiosk_app, "_rekognition", rekognition)
    # Existing kiosk tests use synthetic JPEG envelopes, not real faces.
    monkeypatch.setattr(face_quality, "evaluate", _quality_pass)
    monkeypatch.setattr(kiosk_app.config, "REKOGNITION_FACE_COLLECTION_ID", "kiosk-faces-test")
    monkeypatch.setattr(kiosk_app.face_collection.config, "REKOGNITION_FACE_COLLECTION_ID", "kiosk-faces-test")
    request = RequestStub()
    return request, store, s3, lambda_client, rekognition, kinesis, ddb


def _ensure_hold(request, locker_id="04"):
    existing = request.session.get(kiosk_app._SESSION_HOLD_TX)
    if existing:
        return existing
    reservation = kiosk_app.kiosk_store_reserve(
        kiosk_app.LockerReserveIn(lockerId=locker_id), request
    )
    return reservation["transactionId"]


def _verify_store_face(request, id_bytes=JPEG_ID, face_bytes=JPEG_FACE_A, locker_id="04"):
    transaction_id = _ensure_hold(request, locker_id=locker_id)
    return kiosk_app.kiosk_store_face_verify(
        kiosk_app.StoreFaceVerifyIn(
            imageBase64=_b64(id_bytes),
            contentType="image/jpeg",
            filename="id.jpg",
            faceImageBase64=_b64(face_bytes),
            transactionId=transaction_id,
        ),
        request,
    )


def reserve_and_store(request, locker_id="04", face_bytes=JPEG_FACE_A):
    reservation = kiosk_app.kiosk_store_reserve(
        kiosk_app.LockerReserveIn(lockerId=locker_id), request
    )
    _verify_store_face(request, face_bytes=face_bytes, locker_id=locker_id)
    return kiosk_app.kiosk_store_complete(
        kiosk_app.StoreCompleteIn(
            transactionId=reservation["transactionId"],
            verifiedFaceImageBase64=_b64(face_bytes),
        ),
        request,
    )


def test_operator_clients_and_routes_are_gone():
    assert not hasattr(kiosk_app, "_kinesis")
    assert not hasattr(kiosk_app, "_lambda")
    assert not hasattr(kiosk_app, "_dynamodb")
    assert not hasattr(kiosk_app, "capture_frame")
    assert not hasattr(kiosk_app, "get_detect_labels")
    assert hasattr(kiosk_app, "_rekognition")
    assert not hasattr(kiosk_app, "_s3")
    assert not hasattr(reference_face, "save_reference_face")
    assert not hasattr(reference_face, "delete_reference_face")
    assert not hasattr(reference_face, "compare_reference_to_face_bytes")
    assert not hasattr(reference_face, "reference_object_exists")
    assert not hasattr(reference_face, "build_reference_face_s3_key")


def test_store_face_verify_calls_rekognition_bytes_to_bytes(api):
    request, _, s3, lambda_client, rekognition, kinesis, ddb = api
    result = _verify_store_face(request)

    assert result["matched"] is True
    assert result["reason"] == "SIMILARITY_ABOVE_THRESHOLD"
    assert request.session["verified_store_face_hash"] == _sha256(JPEG_FACE_A)
    assert "verified_store_at" in request.session
    assert "imageBase64" not in request.session
    assert "verified_store_frame_id" not in request.session
    assert "verifiedFaceImageBase64" not in request.session

    assert len(rekognition.calls) == 1
    call = rekognition.calls[0]
    assert call["SourceImage"] == {"Bytes": JPEG_ID}
    assert call["TargetImage"] == {"Bytes": JPEG_FACE_A}
    assert lambda_client.calls == []
    assert kinesis.calls == []
    assert ddb.table.get_item_calls == []
    assert s3.put_calls == []
    assert s3.copy_calls == []


def test_store_face_verify_mismatch_does_not_create_reference(api):
    request, _, s3, lambda_client, rekognition, kinesis, ddb = api
    rekognition.results.append(_mismatch_rekognition())
    result = _verify_store_face(request)

    assert result["matched"] is False
    assert result["reason"] == "SIMILARITY_BELOW_THRESHOLD"
    assert "verified_store_face_hash" not in request.session
    assert s3.put_calls == []
    assert s3.copy_calls == []
    assert lambda_client.calls == []
    assert kinesis.calls == []
    assert ddb.table.get_item_calls == []


def test_store_face_verify_success_does_not_write_s3(api):
    request, _, s3, _, _, _, _ = api
    _verify_store_face(request)
    assert s3.put_calls == []
    assert s3.copy_calls == []
    assert s3.objects == {}


def test_store_complete_rejects_unverified_session(api):
    request, store, s3, _, _, _, _ = api
    reservation = kiosk_app.kiosk_store_reserve(
        kiosk_app.LockerReserveIn(lockerId="04"), request
    )
    with pytest.raises(HTTPException) as error:
        kiosk_app.kiosk_store_complete(
            kiosk_app.StoreCompleteIn(
                transactionId=reservation["transactionId"],
                verifiedFaceImageBase64=_b64(JPEG_FACE_A),
            ),
            request,
        )
    assert error.value.status_code == 403
    assert error.value.detail == "STORE_FACE_NOT_VERIFIED"
    assert store.get_transaction(reservation["transactionId"])["status"] == "RESERVED"
    assert s3.put_calls == []


def test_store_complete_rejects_hash_mismatch(api):
    request, store, s3, _, _, _, _ = api
    reservation = kiosk_app.kiosk_store_reserve(
        kiosk_app.LockerReserveIn(lockerId="04"), request
    )
    _verify_store_face(request, face_bytes=JPEG_FACE_A)
    with pytest.raises(HTTPException) as error:
        kiosk_app.kiosk_store_complete(
            kiosk_app.StoreCompleteIn(
                transactionId=reservation["transactionId"],
                verifiedFaceImageBase64=_b64(JPEG_OTHER),
            ),
            request,
        )
    assert error.value.status_code == 403
    assert error.value.detail == "STORE_FACE_MISMATCH"
    assert store.get_transaction(reservation["transactionId"])["status"] == "RESERVED"
    assert s3.put_calls == []


def test_store_complete_rejects_expired_verification(api):
    request, store, s3, _, _, _, _ = api
    reservation = kiosk_app.kiosk_store_reserve(
        kiosk_app.LockerReserveIn(lockerId="04"), request
    )
    _verify_store_face(request)
    request.session["verified_store_at"] = "2020-01-01T00:00:00+00:00"
    with pytest.raises(HTTPException) as error:
        kiosk_app.kiosk_store_complete(
            kiosk_app.StoreCompleteIn(
                transactionId=reservation["transactionId"],
                verifiedFaceImageBase64=_b64(JPEG_FACE_A),
            ),
            request,
        )
    assert error.value.status_code == 403
    assert error.value.detail == "STORE_FACE_VERIFICATION_EXPIRED"
    assert store.get_transaction(reservation["transactionId"])["status"] == "RESERVED"
    assert s3.put_calls == []


def test_store_complete_without_collection_id_returns_502(api, monkeypatch):
    request, store, _, _, rekognition, _, _ = api
    monkeypatch.setattr(kiosk_app.config, "REKOGNITION_FACE_COLLECTION_ID", "")
    monkeypatch.setattr(kiosk_app.face_collection.config, "REKOGNITION_FACE_COLLECTION_ID", "")
    reservation = kiosk_app.kiosk_store_reserve(
        kiosk_app.LockerReserveIn(lockerId="04"), request
    )
    _verify_store_face(request)
    with pytest.raises(HTTPException) as error:
        kiosk_app.kiosk_store_complete(
            kiosk_app.StoreCompleteIn(
                transactionId=reservation["transactionId"],
                verifiedFaceImageBase64=_b64(JPEG_FACE_A),
            ),
            request,
        )
    assert error.value.status_code == 502
    assert error.value.detail == "FACE_COLLECTION_NOT_CONFIGURED"
    assert rekognition.index_calls == []
    assert store.get_transaction(reservation["transactionId"])["status"] == "RESERVED"


def test_store_complete_indexes_once_and_does_not_write_reference_jpg(api):
    request, store, s3, lambda_client, rekognition, kinesis, ddb = api
    reservation = kiosk_app.kiosk_store_reserve(
        kiosk_app.LockerReserveIn(lockerId="04"), request
    )
    _verify_store_face(request)
    body = kiosk_app.StoreCompleteIn(
        transactionId=reservation["transactionId"],
        verifiedFaceImageBase64=_b64(JPEG_FACE_A),
    )
    first = kiosk_app.kiosk_store_complete(body, request)
    second = kiosk_app.kiosk_store_complete(body, request)
    status = kiosk_app.kiosk_transaction_status(reservation["transactionId"], request)

    assert second == first == status
    assert store.get_transaction(reservation["transactionId"])["status"] == "STORED"
    assert first["referencePresent"] is True
    assert "referenceFrameId" not in first
    assert "reference_face_s3_key" not in first
    assert "referenceFaceS3Key" not in first
    assert len(rekognition.index_calls) == 1
    assert rekognition.index_calls[0]["Image"]["Bytes"] == JPEG_FACE_A
    assert rekognition.index_calls[0]["CollectionId"] == "kiosk-faces-test"
    assert store.get_transaction(reservation["transactionId"])["rekognition_face_id"] == (
        "face-id-store-a"
    )
    assert s3.put_calls == []
    assert s3.copy_calls == []
    assert lambda_client.calls == []
    assert kinesis.calls == []
    assert ddb.table.get_item_calls == []
    assert "verified_store_face_hash" not in request.session


def test_store_path_does_not_use_operator_pipeline(api):
    request, _, s3, lambda_client, _, kinesis, ddb = api
    reserve_and_store(request)
    assert lambda_client.calls == []
    assert kinesis.calls == []
    assert ddb.table.get_item_calls == []
    assert s3.copy_calls == []
    assert all(
        not key.startswith("frames/")
        for _bucket, key in s3.objects
    )


def test_transaction_status_does_not_include_unrelated_or_uncreated_fields(api):
    request, _, _, _, _, _, _ = api
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
    request, _, _, _, _, _, _ = api

    with pytest.raises(HTTPException) as error:
        kiosk_app.kiosk_retrieve_start(
            kiosk_app.RetrievalStartIn(
                retrievalCode=retrieval_code, faceImageBase64=_b64(JPEG_FACE_B)
            ),
            request,
        )

    assert error.value.status_code == 400
    assert error.value.detail == "INVALID_RETRIEVAL_CODE"


def test_retrieve_lookup_unknown_code_skips_camera_and_aws(api):
    request, _, s3, lambda_client, rekognition, kinesis, ddb = api
    with pytest.raises(HTTPException) as error:
        kiosk_app.kiosk_retrieve_lookup(
            kiosk_app.RetrievalLookupIn(retrievalCode="00000000"), request
        )
    assert error.value.status_code == 409
    assert error.value.detail == "RETRIEVAL_UNAVAILABLE"
    assert rekognition.calls == []
    assert rekognition.search_calls == []
    assert rekognition.index_calls == []
    assert s3.head_calls == []
    assert lambda_client.calls == []
    assert kinesis.calls == []
    assert ddb.table.get_item_calls == []


def test_retrieve_lookup_invalid_format_skips_sqlite_aws(api):
    request, _, _, _, rekognition, _, _ = api
    with pytest.raises(HTTPException) as error:
        kiosk_app.kiosk_retrieve_lookup(
            kiosk_app.RetrievalLookupIn(retrievalCode="12AB"), request
        )
    assert error.value.status_code == 400
    assert error.value.detail == "INVALID_RETRIEVAL_CODE"
    assert rekognition.calls == []


def test_retrieve_lookup_stored_ready_does_not_call_aws_or_expose_face(api):
    request, _, s3, lambda_client, rekognition, kinesis, ddb = api
    stored = reserve_and_store(request)
    rekognition.calls.clear()
    rekognition.search_calls.clear()
    rekognition.index_calls.clear()
    result = kiosk_app.kiosk_retrieve_lookup(
        kiosk_app.RetrievalLookupIn(retrievalCode=stored["retrievalCode"]),
        request,
    )
    assert result == {"ready": True, "status": "STORED"}
    assert "lockerId" not in result
    assert "transactionId" not in result
    assert "rekognition_face_id" not in result
    assert rekognition.calls == []
    assert rekognition.search_calls == []
    assert s3.head_calls == []
    assert lambda_client.calls == []
    assert kinesis.calls == []
    assert ddb.table.get_item_calls == []


def test_retrieve_lookup_without_face_id_is_consistency_error(api):
    request, store, s3, _, rekognition, _, _ = api
    reservation = store.reserve_locker("05")
    stored = store.complete_store(
        reservation["transactionId"],
        rekognition_collection_id="kiosk-faces-test",
        rekognition_face_id="face-id-tmp",
    )
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE transactions SET rekognition_face_id = NULL, face_indexed_at = NULL "
            "WHERE transaction_id = ?",
            (stored["transactionId"],),
        )
        connection.commit()
    rekognition.search_calls.clear()
    s3.head_calls.clear()
    with pytest.raises(HTTPException) as error:
        kiosk_app.kiosk_retrieve_lookup(
            kiosk_app.RetrievalLookupIn(retrievalCode=stored["retrievalCode"]),
            request,
        )
    assert error.value.status_code == 500
    assert error.value.detail == "DATA_CONSISTENCY_ERROR"
    assert rekognition.search_calls == []
    assert s3.head_calls == []
    assert s3.get_calls == []


def test_retrieve_start_requires_face_image(api):
    request, _, _, _, _, _, _ = api
    with pytest.raises(HTTPException) as error:
        kiosk_app.kiosk_retrieve_start(
            kiosk_app.RetrievalStartIn(retrievalCode="12345678"), request
        )
    assert error.value.status_code == 400
    assert error.value.detail == "FACE_IMAGE_REQUIRED"


def test_repeated_failed_retrieval_attempts_return_429(api):
    request, _, _, _, _, _, _ = api

    for suffix in range(5):
        with pytest.raises(HTTPException) as unavailable:
            kiosk_app.kiosk_retrieve_start(
                kiosk_app.RetrievalStartIn(
                    retrievalCode="999999%02d" % suffix,
                    faceImageBase64=_b64(JPEG_FACE_B),
                ),
                request,
            )
        assert unavailable.value.status_code == 409
        assert unavailable.value.detail == "RETRIEVAL_UNAVAILABLE"

    with pytest.raises(HTTPException) as limited:
        kiosk_app.kiosk_retrieve_start(
            kiosk_app.RetrievalStartIn(
                retrievalCode="88888888", faceImageBase64=_b64(JPEG_FACE_B)
            ),
            request,
        )

    assert limited.value.status_code == 429
    assert limited.value.detail == "RETRIEVAL_RATE_LIMITED"
    assert int(limited.value.headers["Retry-After"]) >= 1


def test_retrieve_matching_face_authorizes_locker(api):
    request, store, s3, lambda_client, rekognition, kinesis, ddb = api
    stored = reserve_and_store(request)
    rekognition.calls.clear()
    result = kiosk_app.kiosk_retrieve_start(
        kiosk_app.RetrievalStartIn(
            retrievalCode=stored["retrievalCode"],
            faceImageBase64=_b64(JPEG_FACE_B),
        ),
        request,
    )
    assert result["matched"] is True
    assert result["status"] == "RETRIEVING"
    assert result["lockerId"] == "04"
    assert result["transactionId"] == stored["transactionId"]
    assert "reference_face_s3_key" not in result
    assert "targetFrameId" not in result
    assert store.get_transaction(stored["transactionId"])["status"] == "RETRIEVING"

    assert len(rekognition.search_calls) == 1
    assert rekognition.calls == []
    call = rekognition.search_calls[0]
    assert call["CollectionId"] == "kiosk-faces-test"
    assert call["Image"]["Bytes"] == JPEG_FACE_B
    assert lambda_client.calls == []
    assert kinesis.calls == []
    assert ddb.table.get_item_calls == []
    assert s3.head_calls == []
    assert s3.get_calls == []
    assert s3.delete_calls == []
    assert all(item["Key"] != "frames/" + stored["transactionId"] for item in s3.put_calls)
    # FRAME-B must not be written.
    assert JPEG_FACE_B not in s3.objects.values()


def test_retrieve_nonmatching_face_stays_stored(api):
    request, store, s3, lambda_client, rekognition, kinesis, ddb = api
    stored = reserve_and_store(request)
    rekognition.search_results.append(
        {"FaceMatches": [{"Face": {"FaceId": "face-id-store-a"}, "Similarity": 12.0}]}
    )
    result = kiosk_app.kiosk_retrieve_start(
        kiosk_app.RetrievalStartIn(
            retrievalCode=stored["retrievalCode"],
            faceImageBase64=_b64(JPEG_FACE_B),
        ),
        request,
    )
    assert result["matched"] is False
    assert result["reason"] == "SIMILARITY_BELOW_THRESHOLD"
    assert result.get("status") == "STORED"
    assert "lockerId" not in result
    assert store.get_transaction(stored["transactionId"])["status"] == "STORED"
    assert lambda_client.calls == []
    assert kinesis.calls == []
    assert ddb.table.get_item_calls == []
    assert JPEG_FACE_B not in s3.objects.values()


def test_retrieve_wrong_code_no_locker_authorization(api):
    request, store, _, _, _, _, _ = api
    reserve_and_store(request)
    with pytest.raises(HTTPException) as error:
        kiosk_app.kiosk_retrieve_start(
            kiosk_app.RetrievalStartIn(
                retrievalCode="00000000", faceImageBase64=_b64(JPEG_FACE_B)
            ),
            request,
        )
    assert error.value.status_code == 409
    assert error.value.detail == "RETRIEVAL_UNAVAILABLE"


def test_retrieve_face_id_missing_is_consistency_error(api):
    request, store, s3, _, rekognition, _, _ = api
    reservation = store.reserve_locker("05")
    stored = store.complete_store(
        reservation["transactionId"],
        rekognition_collection_id="kiosk-faces-test",
        rekognition_face_id="face-id-tmp",
    )
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE transactions SET rekognition_face_id = NULL, face_indexed_at = NULL "
            "WHERE transaction_id = ?",
            (stored["transactionId"],),
        )
        connection.commit()
    with pytest.raises(HTTPException) as error:
        kiosk_app.kiosk_retrieve_start(
            kiosk_app.RetrievalStartIn(
                retrievalCode=stored["retrievalCode"],
                faceImageBase64=_b64(JPEG_FACE_B),
            ),
            request,
        )
    assert error.value.status_code == 500
    assert error.value.detail == "DATA_CONSISTENCY_ERROR"
    assert store.get_transaction(stored["transactionId"])["status"] == "STORED"
    assert rekognition.search_calls == []
    assert s3.head_calls == []
    assert s3.get_calls == []
    assert s3.delete_calls == []


def test_retrieve_complete_deletes_reference(api):
    request, store, s3, _, rekognition, _, _ = api
    stored = reserve_and_store(request)
    started = kiosk_app.kiosk_retrieve_start(
        kiosk_app.RetrievalStartIn(
            retrievalCode=stored["retrievalCode"],
            faceImageBase64=_b64(JPEG_FACE_B),
        ),
        request,
    )
    completed = kiosk_app.kiosk_retrieve_complete(
        kiosk_app.TransactionIn(transactionId=started["transactionId"]), request
    )
    assert completed["status"] == "RETRIEVED"
    assert store.get_transaction(started["transactionId"])["status"] == "RETRIEVED"
    assert len(rekognition.delete_calls) == 1
    assert rekognition.delete_calls[0]["FaceIds"] == ["face-id-store-a"]
    assert s3.delete_calls == []
    txn = store.get_transaction(started["transactionId"])
    assert txn["face_deleted_at"]


def test_retrieval_start_response_can_be_recovered_without_reclaiming(api):
    request, store, _, _, _, _, _ = api
    stored = reserve_and_store(request)
    body = kiosk_app.RetrievalStartIn(
        retrievalCode=stored["retrievalCode"], faceImageBase64=_b64(JPEG_FACE_B)
    )

    started = kiosk_app.kiosk_retrieve_start(body, request)
    recovered = kiosk_app.kiosk_retrieve_recover(
        kiosk_app.RetrievalStartIn(retrievalCode=stored["retrievalCode"]), request
    )

    assert recovered["transactionId"] == started["transactionId"]
    assert recovered["status"] == "RETRIEVING"
    with pytest.raises(HTTPException) as repeated_start:
        kiosk_app.kiosk_retrieve_start(body, request)
    assert repeated_start.value.status_code == 409
    assert store.get_transaction(started["transactionId"])["status"] == "RETRIEVING"


def test_stale_retrieving_recovery_still_works(api):
    request, store, _, _, _, _, _ = api
    import datetime

    now = datetime.datetime(2026, 8, 9, tzinfo=datetime.timezone.utc)
    clock = [now]
    store.clock = lambda: clock[0]
    store.retrieval_seconds = 120
    stored = reserve_and_store(request)
    kiosk_app.kiosk_retrieve_start(
        kiosk_app.RetrievalStartIn(
            retrievalCode=stored["retrievalCode"], faceImageBase64=_b64(JPEG_FACE_B)
        ),
        request,
    )
    clock[0] = now + datetime.timedelta(seconds=121)
    status = store.get_transaction_status(stored["transactionId"])
    assert status["status"] == "STORED"
    again = kiosk_app.kiosk_retrieve_start(
        kiosk_app.RetrievalStartIn(
            retrievalCode=stored["retrievalCode"], faceImageBase64=_b64(JPEG_FACE_B)
        ),
        request,
    )
    assert again["status"] == "RETRIEVING"


def test_kiosk_transaction_apis_require_session(api):
    _, _, _, _, _, _, _ = api
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


def test_legacy_s3_helpers_are_gone():
    assert not hasattr(kiosk_app, "_s3")
    assert not hasattr(kiosk_app.config, "FRAME_S3_BUCKET")
    assert not hasattr(reference_face, "build_reference_face_s3_key")
    assert not hasattr(reference_face, "legacy_s3")


def test_hash_face_image_is_sha256():
    assert reference_face.hash_face_image(JPEG_FACE_A) == _sha256(JPEG_FACE_A)


def test_decode_face_image_rejects_non_image_bytes():
    with pytest.raises(reference_face.ReferenceFaceError) as error:
        reference_face.decode_face_image(_b64(b"not-an-image"))
    assert error.value.reason == "UNSUPPORTED_IMAGE_FORMAT"


def test_decode_face_image_accepts_png_declared_as_jpeg():
    png = b"\x89PNG\r\n\x1a\n" + b"png-payload"
    image_bytes, detected = reference_face.decode_face_image(
        _b64(png), content_type="image/jpeg"
    )
    assert detected == "image/png"
    assert image_bytes.startswith(b"\x89PNG")


def test_describe_image_payload_is_metadata_only():
    jpeg = JPEG_ID
    info = reference_face.describe_image_payload(_b64(jpeg), content_type="image/jpeg")
    assert info["magic"] == "JPEG"
    assert info["decoded_bytes"] == len(jpeg)
    assert info["decode_ok"] is True
    dumped = json.dumps(info)
    assert "base64" not in dumped
    assert _b64(jpeg) not in dumped
    assert b"id-photo-bytes".decode("latin1") not in dumped


def test_store_face_verify_stage_logs_share_request_id(api, caplog):
    import logging

    request, _, _, _, rekognition, _, _ = api
    with caplog.at_level(logging.INFO, logger="webui"):
        result = _verify_store_face(request)
    assert result["matched"] is True
    assert rekognition.calls
    messages = [record.getMessage() for record in caplog.records]
    stage_lines = [line for line in messages if "event=face_verify_stage" in line]
    request_ids = set()
    for line in stage_lines:
        for token in line.split():
            if token.startswith("request_id=store-face-verify-"):
                request_ids.add(token.split("=", 1)[1])
    assert len(request_ids) == 1
    request_id = next(iter(request_ids))
    joined = "\n".join(stage_lines)
    assert "stage=id_decode result=PASS" in joined
    assert "stage=frame_a_decode result=PASS" in joined
    assert "stage=quality_gate result=PASS" in joined
    assert "stage=rekognition result=PASS" in joined
    assert "base64" not in joined.lower()
    assert JPEG_ID not in joined.encode("latin1", errors="ignore")
    assert all(request_id in line for line in stage_lines)


def test_store_id_decode_failure_skips_quality_and_rekognition(api, monkeypatch):
    request, _, _, _, rekognition, _, _ = api
    seen = []
    monkeypatch.setattr(
        face_quality, "evaluate", lambda image_bytes, **kwargs: seen.append(image_bytes)
    )
    _ensure_hold(request)
    result = kiosk_app.kiosk_store_face_verify(
        kiosk_app.StoreFaceVerifyIn(
            imageBase64=_b64(b"not-an-image"),
            contentType="image/jpeg",
            filename="id.jpg",
            faceImageBase64=_b64(JPEG_FACE_A),
            transactionId=request.session.get(kiosk_app._SESSION_HOLD_TX),
        ),
        request,
    )
    assert result["matched"] is False
    assert result["reason"] == "UNSUPPORTED_IMAGE_FORMAT"
    assert result["source"] == "id_image"
    assert seen == []
    assert rekognition.calls == []


def test_store_id_decode_failure_logs_later_stages_not_reached(api, monkeypatch, caplog):
    import logging

    request, _, _, _, rekognition, _, _ = api
    monkeypatch.setattr(
        face_quality, "evaluate", lambda image_bytes, **kwargs: (_ for _ in ()).throw(
            AssertionError("quality must not run")
        )
    )
    with caplog.at_level(logging.INFO, logger="webui"):
        result = kiosk_app.kiosk_store_face_verify(
            kiosk_app.StoreFaceVerifyIn(
                imageBase64=_b64(b"not-an-image"),
                contentType="image/jpeg",
                filename="id.jpg",
                faceImageBase64=_b64(JPEG_FACE_A),
                transactionId=_ensure_hold(request),
            ),
            request,
        )
    assert result["source"] == "id_image"
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "stage=id_decode result=FAIL" in joined
    assert "stage=frame_a_decode result=NOT_REACHED" in joined
    assert "stage=quality_gate result=NOT_REACHED" in joined
    assert "stage=rekognition result=NOT_CALLED" in joined
    assert rekognition.calls == []


def test_store_frame_a_decode_failure_skips_quality_and_rekognition(api, monkeypatch):
    request, _, _, _, rekognition, _, _ = api
    seen = []
    monkeypatch.setattr(
        face_quality, "evaluate", lambda image_bytes, **kwargs: seen.append(image_bytes)
    )
    _ensure_hold(request)
    result = kiosk_app.kiosk_store_face_verify(
        kiosk_app.StoreFaceVerifyIn(
            imageBase64=_b64(JPEG_ID),
            contentType="image/jpeg",
            filename="id.jpg",
            faceImageBase64=_b64(b"not-an-image"),
            transactionId=request.session.get(kiosk_app._SESSION_HOLD_TX),
        ),
        request,
    )
    assert result["matched"] is False
    assert result["reason"] == "UNSUPPORTED_IMAGE_FORMAT"
    assert result["source"] == "frame_a"
    assert seen == []
    assert rekognition.calls == []


def test_store_quality_invalid_image_skips_rekognition(api, monkeypatch):
    request, _, _, _, rekognition, _, _ = api
    monkeypatch.setattr(
        face_quality,
        "evaluate",
        lambda image_bytes, **kwargs: _quality_fail("INVALID_IMAGE", image_bytes),
    )
    result = _verify_store_face(request)
    assert result["matched"] is False
    assert result["reason"] == "INVALID_IMAGE"
    assert result["source"] == "quality_gate"
    assert rekognition.calls == []


@pytest.mark.parametrize(
    "reason",
    [
        "NO_FACE",
        "MULTIPLE_FACES",
        "FACE_TOO_SMALL",
        "FACE_OFF_CENTER",
        "TOO_DARK",
        "TOO_BRIGHT",
        "TOO_BLURRY",
        "FACE_TILTED",
        "FACE_POSE_INVALID",
        "QUALITY_DETECTOR_UNAVAILABLE",
    ],
)
def test_store_quality_fail_does_not_call_rekognition(api, monkeypatch, reason):
    request, _, s3, lambda_client, rekognition, kinesis, ddb = api
    seen = []

    def fail_gate(image_bytes, **kwargs):
        seen.append(bytes(image_bytes))
        return _quality_fail(reason, image_bytes)

    monkeypatch.setattr(face_quality, "evaluate", fail_gate)
    result = _verify_store_face(request)

    assert result["matched"] is False
    assert result["reason"] == reason
    assert result["source"] == "quality_gate"
    assert "verified_store_face_hash" not in request.session
    assert rekognition.calls == []
    assert lambda_client.calls == []
    assert kinesis.calls == []
    assert ddb.table.get_item_calls == []
    assert s3.put_calls == []
    assert seen == [JPEG_FACE_A]


def test_store_quality_pass_calls_rekognition_with_same_frame_bytes(api, monkeypatch):
    request, _, _, _, rekognition, _, _ = api
    seen = []

    def pass_gate(image_bytes, **kwargs):
        seen.append(bytes(image_bytes))
        return _quality_pass(image_bytes)

    monkeypatch.setattr(face_quality, "evaluate", pass_gate)
    result = _verify_store_face(request)

    assert result["matched"] is True
    assert result["source"] == "rekognition"
    assert len(rekognition.calls) == 1
    assert seen == [JPEG_FACE_A]
    assert rekognition.calls[0]["TargetImage"]["Bytes"] == JPEG_FACE_A
    assert rekognition.calls[0]["TargetImage"]["Bytes"] == seen[0]


def test_retrieve_quality_fail_skips_aws_and_does_not_count_as_code_failure(
    api, monkeypatch
):
    request, store, s3, lambda_client, rekognition, kinesis, ddb = api
    stored = reserve_and_store(request)
    rekognition.calls.clear()
    s3.head_calls.clear()
    limiter = kiosk_app._retrieval_limiter
    scope = kiosk_app._retrieval_rate_scope(request)
    seen = []

    def fail_gate(image_bytes):
        seen.append(bytes(image_bytes))
        return _quality_fail("TOO_BLURRY", image_bytes)

    monkeypatch.setattr(face_quality, "evaluate", fail_gate)
    result = kiosk_app.kiosk_retrieve_start(
        kiosk_app.RetrievalStartIn(
            retrievalCode=stored["retrievalCode"],
            faceImageBase64=_b64(JPEG_FACE_B),
        ),
        request,
    )

    assert result["reason"] == "TOO_BLURRY"
    assert result["source"] == "quality_gate"
    assert result["matched"] is False
    assert result["status"] == "STORED"
    assert "lockerId" not in result
    assert store.get_transaction(stored["transactionId"])["status"] == "STORED"
    assert rekognition.calls == []
    assert s3.head_calls == []
    assert lambda_client.calls == []
    assert kinesis.calls == []
    assert ddb.table.get_item_calls == []
    assert JPEG_FACE_B not in s3.objects.values()
    assert seen == [JPEG_FACE_B]
    assert limiter.retry_after(scope) == 0


def test_same_id_bad_frame_quality_is_not_id_source(api, monkeypatch, caplog):
    import logging

    request, _, _, _, rekognition, _, _ = api
    monkeypatch.setattr(
        face_quality,
        "evaluate",
        lambda image_bytes, **kwargs: _quality_fail("TOO_BLURRY", image_bytes),
    )
    with caplog.at_level(logging.INFO, logger="webui"):
        result = _verify_store_face(request)
    assert result["reason"] == "TOO_BLURRY"
    assert result["source"] == "quality_gate"
    assert result["source"] != "id_image"
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "stage=id_decode result=PASS" in joined
    assert "stage=quality_gate result=FAIL" in joined
    assert "reason=TOO_BLURRY" in joined
    assert "stage=rekognition result=NOT_CALLED" in joined
    assert rekognition.calls == []


def test_retrieve_quality_pass_uses_same_frame_bytes_for_rekognition(api, monkeypatch):
    request, _, _, _, rekognition, _, _ = api
    stored = reserve_and_store(request)
    rekognition.calls.clear()
    seen = []

    def pass_gate(image_bytes, **kwargs):
        seen.append(bytes(image_bytes))
        return _quality_pass(image_bytes)

    monkeypatch.setattr(face_quality, "evaluate", pass_gate)
    result = kiosk_app.kiosk_retrieve_start(
        kiosk_app.RetrievalStartIn(
            retrievalCode=stored["retrievalCode"],
            faceImageBase64=_b64(JPEG_FACE_B),
        ),
        request,
    )

    assert result["matched"] is True
    assert result["source"] == "rekognition"
    assert len(rekognition.search_calls) == 1
    assert seen == [JPEG_FACE_B]
    assert rekognition.search_calls[0]["Image"]["Bytes"] == JPEG_FACE_B
    assert rekognition.search_calls[0]["Image"]["Bytes"] == seen[0]


def test_store_face_verify_without_hold_skips_rekognition(api):
    request, _, _, _, rekognition, _, _ = api
    result = kiosk_app.kiosk_store_face_verify(
        kiosk_app.StoreFaceVerifyIn(
            imageBase64=_b64(JPEG_ID),
            contentType="image/jpeg",
            filename="id.jpg",
            faceImageBase64=_b64(JPEG_FACE_A),
        ),
        request,
    )
    assert result["reason"] == "LOCKER_HOLD_NOT_OWNED"
    assert result["source"] == "hold"
    assert rekognition.calls == []
    assert rekognition.index_calls == []


def test_store_face_verify_expired_hold_skips_rekognition(api):
    request, store, _, _, rekognition, _, _ = api
    reservation = kiosk_app.kiosk_store_reserve(
        kiosk_app.LockerReserveIn(lockerId="04"), request
    )
    store.cancel_store(reservation["transactionId"])
    result = _verify_store_face(request)
    assert result["reason"] == "LOCKER_HOLD_EXPIRED"
    assert rekognition.calls == []
    assert rekognition.index_calls == []


def test_compare_fail_does_not_index_faces(api):
    request, store, _, _, rekognition, _, _ = api
    reservation = kiosk_app.kiosk_store_reserve(
        kiosk_app.LockerReserveIn(lockerId="04"), request
    )
    rekognition.results.append(_mismatch_rekognition())
    result = _verify_store_face(request)
    assert result["reason"] == "SIMILARITY_BELOW_THRESHOLD"
    with pytest.raises(HTTPException) as error:
        kiosk_app.kiosk_store_complete(
            kiosk_app.StoreCompleteIn(
                transactionId=reservation["transactionId"],
                verifiedFaceImageBase64=_b64(JPEG_FACE_A),
            ),
            request,
        )
    assert error.value.status_code == 403
    assert rekognition.index_calls == []
    assert store.get_transaction(reservation["transactionId"])["status"] == "RESERVED"


def test_cancel_before_complete_does_not_index_faces(api):
    request, store, _, _, rekognition, _, _ = api
    reservation = kiosk_app.kiosk_store_reserve(
        kiosk_app.LockerReserveIn(lockerId="04"), request
    )
    _verify_store_face(request)
    kiosk_app.kiosk_store_cancel(
        kiosk_app.TransactionIn(transactionId=reservation["transactionId"]), request
    )
    assert rekognition.index_calls == []
    assert store.get_transaction(reservation["transactionId"])["status"] == "CANCELLED"


def test_index_faces_uses_verified_frame_a_bytes(api):
    request, _, _, _, rekognition, _, _ = api
    stored = reserve_and_store(request, face_bytes=JPEG_FACE_A)
    assert stored["status"] == "STORED"
    assert rekognition.index_calls[0]["Image"]["Bytes"] == JPEG_FACE_A
    assert rekognition.calls[0]["TargetImage"]["Bytes"] == JPEG_FACE_A


def test_index_faces_without_face_id_does_not_store(api):
    request, store, _, _, rekognition, _, _ = api
    reservation = kiosk_app.kiosk_store_reserve(
        kiosk_app.LockerReserveIn(lockerId="04"), request
    )
    _verify_store_face(request)
    rekognition.index_results.append({"FaceRecords": [], "UnindexedFaces": []})
    with pytest.raises(HTTPException) as error:
        kiosk_app.kiosk_store_complete(
            kiosk_app.StoreCompleteIn(
                transactionId=reservation["transactionId"],
                verifiedFaceImageBase64=_b64(JPEG_FACE_A),
            ),
            request,
        )
    assert error.value.status_code == 502
    assert error.value.detail == "FACE_INDEX_FAILED"
    assert store.get_transaction(reservation["transactionId"])["status"] == "RESERVED"


def test_index_success_db_failure_compensates_with_delete_faces(api, monkeypatch):
    request, _, _, _, rekognition, _, _ = api
    reservation = kiosk_app.kiosk_store_reserve(
        kiosk_app.LockerReserveIn(lockerId="04"), request
    )
    _verify_store_face(request)

    def boom(*args, **kwargs):
        raise StoreTransactionUnavailable("STORE_TRANSACTION_UNAVAILABLE")

    monkeypatch.setattr(kiosk_app._kiosk_store, "complete_store", boom)
    with pytest.raises(HTTPException) as error:
        kiosk_app.kiosk_store_complete(
            kiosk_app.StoreCompleteIn(
                transactionId=reservation["transactionId"],
                verifiedFaceImageBase64=_b64(JPEG_FACE_A),
            ),
            request,
        )
    assert error.value.status_code == 409
    assert len(rekognition.index_calls) == 1
    assert len(rekognition.delete_calls) == 1
    assert rekognition.delete_calls[0]["FaceIds"] == ["face-id-store-a"]


def test_retrieve_other_face_id_is_rejected_even_if_similarity_high(api):
    request, store, _, _, rekognition, _, _ = api
    stored = reserve_and_store(request)
    rekognition.search_results.append(
        {
            "FaceMatches": [
                {"Face": {"FaceId": "someone-else"}, "Similarity": 99.9}
            ]
        }
    )
    result = kiosk_app.kiosk_retrieve_start(
        kiosk_app.RetrievalStartIn(
            retrievalCode=stored["retrievalCode"],
            faceImageBase64=_b64(JPEG_FACE_B),
        ),
        request,
    )
    assert result["matched"] is False
    assert result["reason"] == "EXPECTED_FACE_NOT_FOUND"
    assert store.get_transaction(stored["transactionId"])["status"] == "STORED"


def test_retrieve_expected_face_id_not_required_to_be_top_match(api):
    request, _, _, _, rekognition, _, _ = api
    stored = reserve_and_store(request)
    rekognition.search_results.append(
        {
            "FaceMatches": [
                {"Face": {"FaceId": "other-txn-face"}, "Similarity": 99.1},
                {"Face": {"FaceId": "face-id-store-a"}, "Similarity": 94.0},
            ]
        }
    )
    result = kiosk_app.kiosk_retrieve_start(
        kiosk_app.RetrievalStartIn(
            retrievalCode=stored["retrievalCode"],
            faceImageBase64=_b64(JPEG_FACE_B),
        ),
        request,
    )
    assert result["matched"] is True
    assert result["status"] == "RETRIEVING"
    assert len(rekognition.search_calls) == 1


def test_retrieve_delete_faces_failure_keeps_retrieved(api):
    request, store, _, _, rekognition, _, _ = api
    stored = reserve_and_store(request)
    started = kiosk_app.kiosk_retrieve_start(
        kiosk_app.RetrievalStartIn(
            retrievalCode=stored["retrievalCode"],
            faceImageBase64=_b64(JPEG_FACE_B),
        ),
        request,
    )
    from botocore.exceptions import ClientError

    rekognition.delete_results.append(
        ClientError({"Error": {"Code": "ThrottlingException"}}, "DeleteFaces")
    )
    completed = kiosk_app.kiosk_retrieve_complete(
        kiosk_app.TransactionIn(transactionId=started["transactionId"]), request
    )
    assert completed["status"] == "RETRIEVED"
    txn = store.get_transaction(started["transactionId"])
    assert txn["status"] == "RETRIEVED"
    assert txn["face_deleted_at"] is None
    pending = store.list_pending_face_cleanup()
    assert any(item["transaction_id"] == started["transactionId"] for item in pending)


def test_legacy_s3_branch_is_removed(api):
    assert "legacy_s3" not in open(BACKEND / "kiosk_store.py", encoding="utf-8").read()
    assert "legacy_s3" not in open(BACKEND / "app.py", encoding="utf-8").read()


def test_store_complete_does_not_expose_or_write_s3_reference(api):
    request, store, s3, _, rekognition, _, _ = api
    stored = reserve_and_store(request)
    txn = store.get_transaction(stored["transactionId"])
    assert txn["rekognition_face_id"]
    assert txn["rekognition_collection_id"]
    assert txn["face_indexed_at"]
    assert txn["reference_face_s3_key"] is None
    assert s3.put_calls == []
    assert s3.get_calls == []
    assert not hasattr(reference_face, "save_reference_face")
    assert len(rekognition.index_calls) == 1


def test_collection_retrieve_does_not_call_s3(api):
    request, _, s3, _, rekognition, _, _ = api
    stored = reserve_and_store(request)
    s3.head_calls.clear()
    s3.get_calls.clear()
    s3.delete_calls.clear()
    s3.put_calls.clear()
    rekognition.calls.clear()
    result = kiosk_app.kiosk_retrieve_start(
        kiosk_app.RetrievalStartIn(
            retrievalCode=stored["retrievalCode"],
            faceImageBase64=_b64(JPEG_FACE_B),
        ),
        request,
    )
    assert result["matched"] is True
    assert len(rekognition.search_calls) == 1
    assert rekognition.calls == []
    assert s3.head_calls == []
    assert s3.get_calls == []
    assert s3.put_calls == []
    assert s3.delete_calls == []


def test_legacy_s3_complete_store_rejected(api):
    _, store, _, _, _, _, _ = api
    reservation = store.reserve_locker("05")
    with pytest.raises(TypeError):
        store.complete_store(
            reservation["transactionId"],
            reference_face_s3_key="locker-references/x/reference.jpg",
        )


def test_invalid_retrieval_code_skips_s3_and_rekognition(api):
    request, _, s3, _, rekognition, _, _ = api
    with pytest.raises(HTTPException) as error:
        kiosk_app.kiosk_retrieve_start(
            kiosk_app.RetrievalStartIn(
                retrievalCode="00000000",
                faceImageBase64=_b64(JPEG_FACE_B),
            ),
            request,
        )
    assert error.value.status_code == 409
    assert error.value.detail == "RETRIEVAL_UNAVAILABLE"
    assert s3.head_calls == []
    assert s3.get_calls == []
    assert s3.put_calls == []
    assert s3.delete_calls == []
    assert rekognition.calls == []
    assert rekognition.search_calls == []
    assert rekognition.index_calls == []
    assert rekognition.delete_calls == []


def test_retrieve_quality_fail_does_not_rate_limit_like_bad_codes(api, monkeypatch):
    request, _, _, _, rekognition, _, _ = api
    stored = reserve_and_store(request)
    rekognition.calls.clear()
    monkeypatch.setattr(
        face_quality,
        "evaluate",
        lambda image_bytes, **kwargs: _quality_fail("NO_FACE", image_bytes),
    )

    for _ in range(6):
        result = kiosk_app.kiosk_retrieve_start(
            kiosk_app.RetrievalStartIn(
                retrievalCode=stored["retrievalCode"],
                faceImageBase64=_b64(JPEG_FACE_B),
            ),
            request,
        )
        assert result["reason"] == "NO_FACE"

    assert rekognition.calls == []
    again = kiosk_app.kiosk_retrieve_start(
        kiosk_app.RetrievalStartIn(
            retrievalCode=stored["retrievalCode"],
            faceImageBase64=_b64(JPEG_FACE_B),
        ),
        request,
    )
    assert again["reason"] == "NO_FACE"
