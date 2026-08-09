import base64
import importlib
import json
import os
import time
import uuid

import pytest


os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-2")

fc = importlib.import_module("lambda.facecompare.facecompare")


class TableStub:
    def __init__(self, frames, latest_id=None):
        self.frames = frames
        self.latest_id = latest_id
        self.get_calls = []
        self.query_calls = []

    def get_item(self, **kwargs):
        self.get_calls.append(kwargs)
        item = self.frames.get(kwargs["Key"]["frame_id"])
        return {"Item": item} if item else {}

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        item = self.frames.get(self.latest_id)
        return {"Items": [item] if item else []}


class DynamoStub:
    def __init__(self, table):
        self.table = table

    def Table(self, name):
        return self.table


class RekognitionStub:
    def __init__(self):
        self.calls = []

    def compare_faces(self, **kwargs):
        self.calls.append(kwargs)
        return {"FaceMatches": [{"Similarity": 96.5}], "UnmatchedFaces": []}


def config():
    return {
        "ddb_table": "EnrichedFrame",
        "ddb_gsi_name": "processed_year_month-processed_timestamp-index",
        "timezone": "UTC",
        "similarity_threshold": 90,
        "rekognition_api_similarity_threshold": 0,
        "quality_filter": "NONE",
        "latest_frame_horizon_minutes": 5,
        "max_source_image_bytes": 5 * 1024 * 1024,
    }


def frame(frame_id, key, captured=None):
    captured = captured or time.time()
    return {
        "frame_id": frame_id,
        "s3_bucket": "frames-bucket",
        "s3_key": key,
        "processed_timestamp": captured + 0.1,
        "approx_capture_timestamp": captured,
    }


def event(target_frame_id=...):
    payload = {
        "imageBase64": base64.b64encode(b"id-image").decode("ascii"),
        "filename": "id.jpg",
        "contentType": "image/jpeg",
        "similarityThreshold": 90,
    }
    if target_frame_id is not ...:
        payload["targetFrameId"] = target_frame_id
    return {"httpMethod": "POST", "body": json.dumps(payload)}


def invoke(monkeypatch, table, rekognition, target_frame_id=...):
    monkeypatch.setattr(fc, "load_config", config)
    monkeypatch.setattr(fc, "dynamodb", DynamoStub(table))
    monkeypatch.setattr(fc, "rekog_client", rekognition)
    return fc.run(event(target_frame_id))


def test_exact_target_lookup_uses_get_item_and_confirms_target(monkeypatch):
    frame_a_id = str(uuid.uuid4())
    table = TableStub({frame_a_id: frame(frame_a_id, "frames/A.jpg")})
    rekognition = RekognitionStub()

    result = invoke(monkeypatch, table, rekognition, frame_a_id)

    assert result["success"] is True
    assert result["targetFrameId"] == frame_a_id
    assert result["target"]["frame_id"] == frame_a_id
    assert "frame_s3_bucket" not in result["target"]
    assert table.get_calls == [{
        "Key": {"frame_id": frame_a_id}, "ConsistentRead": True
    }]
    assert table.query_calls == []


def test_exact_target_not_ready_never_queries_latest(monkeypatch):
    requested = str(uuid.uuid4())
    newer = str(uuid.uuid4())
    table = TableStub({newer: frame(newer, "frames/B.jpg")}, latest_id=newer)
    rekognition = RekognitionStub()

    result = invoke(monkeypatch, table, rekognition, requested)

    assert result["reason"] == "TARGET_FRAME_NOT_READY"
    assert result["targetFrameId"] == requested
    assert result["error"] == {"code": "TARGET_FRAME_NOT_READY"}
    assert fc.respond(result)["statusCode"] == 200
    assert table.query_calls == []
    assert rekognition.calls == []


def test_newer_frame_b_cannot_replace_requested_frame_a(monkeypatch):
    frame_a_id = str(uuid.uuid4())
    frame_b_id = str(uuid.uuid4())
    table = TableStub({
        frame_a_id: frame(frame_a_id, "frames/A.jpg", time.time() - 2),
        frame_b_id: frame(frame_b_id, "frames/B.jpg", time.time() - 1),
    }, latest_id=frame_b_id)
    rekognition = RekognitionStub()

    result = invoke(monkeypatch, table, rekognition, frame_a_id)

    assert result["targetFrameId"] == frame_a_id
    assert rekognition.calls[0]["TargetImage"] == {
        "S3Object": {"Bucket": "frames-bucket", "Name": "frames/A.jpg"}
    }
    assert table.query_calls == []


def test_legacy_request_without_target_uses_latest_query(monkeypatch):
    latest_id = str(uuid.uuid4())
    table = TableStub({latest_id: frame(latest_id, "frames/latest.jpg")}, latest_id)
    rekognition = RekognitionStub()

    result = invoke(monkeypatch, table, rekognition)

    assert result["success"] is True
    assert result["targetFrameId"] == latest_id
    assert table.get_calls == []
    assert len(table.query_calls) == 1


def test_exact_target_too_old_is_distinct_and_not_compared(monkeypatch):
    target_id = str(uuid.uuid4())
    old_frame = frame(target_id, "frames/old.jpg", time.time() - 600)
    table = TableStub({target_id: old_frame})
    rekognition = RekognitionStub()

    result = invoke(monkeypatch, table, rekognition, target_id)

    assert result["reason"] == "TARGET_FRAME_TOO_OLD"
    assert result["targetFrameId"] == target_id
    assert table.query_calls == []
    assert rekognition.calls == []


def test_exact_target_with_invalid_s3_metadata_is_safe(monkeypatch):
    target_id = str(uuid.uuid4())
    invalid = frame(target_id, "frames/invalid.jpg")
    del invalid["s3_key"]
    table = TableStub({target_id: invalid})
    rekognition = RekognitionStub()

    result = invoke(monkeypatch, table, rekognition, target_id)

    assert result["reason"] == "FRAME_METADATA_INVALID"
    assert result["targetFrameId"] == target_id
    assert "frames-bucket" not in json.dumps(result)
    assert rekognition.calls == []


@pytest.mark.parametrize("target", [None, "", "not-a-uuid", str(uuid.uuid4()).upper()])
def test_malformed_target_is_rejected_without_any_lookup(monkeypatch, target):
    table = TableStub({})
    rekognition = RekognitionStub()

    result = invoke(monkeypatch, table, rekognition, target)

    assert result["reason"] == "INVALID_TARGET_FRAME_ID"
    assert fc.respond(result)["statusCode"] == 400
    assert table.get_calls == table.query_calls == []
    assert rekognition.calls == []
