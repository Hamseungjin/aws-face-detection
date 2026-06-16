import base64
import json
import time

import pytest

import importlib
import sys
import types

sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *a, **k: None, resource=lambda *a, **k: None))
conditions = types.ModuleType("boto3.dynamodb.conditions")
conditions.Key = lambda name: types.SimpleNamespace(eq=lambda value: (name, value))
conditions.Attr = lambda name: None
sys.modules.setdefault("boto3.dynamodb", types.ModuleType("boto3.dynamodb"))
sys.modules.setdefault("boto3.dynamodb.conditions", conditions)
exceptions = types.ModuleType("botocore.exceptions")
exceptions.ClientError = type("ClientError", (Exception,), {})
exceptions.BotoCoreError = type("BotoCoreError", (Exception,), {})
sys.modules.setdefault("botocore", types.ModuleType("botocore"))
sys.modules.setdefault("botocore.exceptions", exceptions)

fv = importlib.import_module("lambda.faceverifier.faceverifier")


class RekognitionStub:
    def __init__(self, faces=None, matches=None):
        self.faces = faces if faces is not None else [face(99)]
        self.matches = matches if matches is not None else [{"Similarity": 96.42}]

    def detect_faces(self, **kwargs):
        return {"FaceDetails": self.faces}

    def compare_faces(self, **kwargs):
        return {"FaceMatches": self.matches}


class TableStub:
    def __init__(self, items):
        self.items = list(items)

    def query(self, **kwargs):
        return {"Items": [self.items.pop(0)] if self.items else []}


class DdbStub:
    def __init__(self, items):
        self.table = TableStub(items)

    def Table(self, name):
        return self.table


def face(conf):
    return {
        "BoundingBox": {"Left": 0.1, "Top": 0.2, "Width": 0.3, "Height": 0.4},
        "Confidence": conf,
        "Quality": {"Brightness": 80, "Sharpness": 70},
        "Pose": {"Roll": 1, "Yaw": 2, "Pitch": 3},
    }


def frame(ts=None):
    ts = ts or time.time()
    return {
        "frame_id": "f1",
        "s3_bucket": "bucket",
        "s3_key": "frames/f1.jpg",
        "processed_timestamp": ts,
        "approx_capture_timestamp": ts - 0.1,
    }


def config(**overrides):
    c = fv.DEFAULT_CONFIG.copy()
    c.update({"ddb_table": "t", "ddb_gsi_name": "g", "timezone": "UTC"})
    c.update(overrides)
    return c


def event(payload):
    return {"httpMethod": "POST", "body": json.dumps(payload)}


def payload(**overrides):
    p = {
        "id_image_base64": base64.b64encode(b"image-bytes").decode(),
        "id_image_content_type": "image/jpeg",
    }
    p.update(overrides)
    return p


def body(resp):
    return json.loads(resp["body"])


def call(payload_override=None, rek=None, ddb=None, cfg=None):
    return fv.face_verify(
        event(payload(**(payload_override or {}))),
        None,
        config=cfg or config(),
        rekognition_client=rek or RekognitionStub(),
        dynamodb_resource=ddb or DdbStub([frame(), frame(time.time() - 1)]),
    )


def test_success_above_threshold_and_cors_headers():
    resp = call()
    data = body(resp)
    assert data["success"] is True
    assert data["matched"] is True
    assert data["reason"] == "SIMILARITY_ABOVE_THRESHOLD"
    assert resp["headers"]["Access-Control-Allow-Origin"] == "*"


def test_bad_json():
    resp = fv.face_verify({"httpMethod": "POST", "body": "{"}, None, config=config())
    assert body(resp)["reason"] == "INVALID_REQUEST_BODY"


def test_invalid_base64():
    assert body(call({"id_image_base64": "@@@"}))["reason"] == "INVALID_IMAGE_BASE64"


def test_unsupported_type():
    assert body(call({"id_image_content_type": "image/gif"}))["reason"] == "UNSUPPORTED_IMAGE_TYPE"


def test_image_too_large():
    big = base64.b64encode(b"x" * 4).decode()
    assert body(call({"id_image_base64": big}, cfg=config(max_source_image_bytes=3)))["reason"] == "IMAGE_TOO_LARGE"


def test_no_face():
    assert body(call(rek=RekognitionStub(faces=[])))["reason"] == "NO_FACE_IN_ID_IMAGE"


def test_multiple_faces_rejected():
    data = body(call(rek=RekognitionStub(faces=[face(90), face(99)])))
    assert data["reason"] == "MULTIPLE_FACES_IN_ID_IMAGE"
    assert data["id_image_analysis"]["face_count"] == 2


def test_no_latest_frame():
    assert body(call(ddb=DdbStub([])))["reason"] == "NO_LATEST_FRAME"


def test_no_recent_frame():
    old = time.time() - 600
    assert body(call(ddb=DdbStub([frame(old)]), cfg=config(latest_frame_horizon_minutes=1)))["reason"] == "NO_RECENT_FRAME"


def test_missing_ddb_field():
    item = frame()
    del item["s3_key"]
    assert body(call(ddb=DdbStub([item])))["reason"] == "MISSING_DDB_FIELD"


def test_selects_best_similarity_and_below_threshold():
    data = body(call(rek=RekognitionStub(matches=[{"Similarity": 80}, {"Similarity": 89.9}])))
    assert data["similarity"] == 89.9
    assert data["matched"] is False
    assert data["reason"] == "SIMILARITY_BELOW_THRESHOLD"


def test_options_has_cors():
    resp = fv.face_verify({"httpMethod": "OPTIONS"}, None, config=config())
    assert resp["statusCode"] == 200
    assert resp["headers"]["Access-Control-Allow-Methods"] == "POST,OPTIONS"
