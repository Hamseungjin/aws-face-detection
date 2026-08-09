import base64
import importlib
import pickle
import sys
import types
import uuid
from pathlib import Path

import pytest


IMAGEPROCESSOR_DIR = (
    Path(__file__).resolve().parents[1] / "lambda" / "imageprocessor"
)
sys.path.insert(0, str(IMAGEPROCESSOR_DIR))
ip = importlib.import_module("imageprocessor")


class S3Stub:
    def __init__(self):
        self.calls = []

    def put_object(self, **kwargs):
        self.calls.append(kwargs)


class TableStub:
    def __init__(self):
        self.items = []

    def put_item(self, Item):
        self.items.append(Item)


class DynamoStub:
    def __init__(self, table):
        self.table = table

    def Table(self, name):
        return self.table


def config():
    return {
        "timezone": "UTC",
        "s3_bucket": "frames-bucket",
        "s3_key_frames_root": "frames/",
        "ddb_table": "EnrichedFrame",
        "rekog_max_labels": 10,
        "rekog_min_conf": 50,
        "label_watch_list": [],
        "label_watch_min_conf": 90,
        "ddb_ttl_days": 30,
    }


def event_for(capture_id=...):
    package = {
        "ApproximateCaptureTime": 1_700_000_000.25,
        "FrameCount": 3,
        "ImageBytes": bytearray(b"jpeg"),
    }
    if capture_id is not ...:
        package["CaptureId"] = capture_id
    return {
        "Records": [{
            "kinesis": {
                "data": base64.b64encode(pickle.dumps(package)).decode("ascii")
            }
        }]
    }


@pytest.fixture
def processor(monkeypatch):
    s3 = S3Stub()
    table = TableStub()
    monkeypatch.setenv("ENABLE_DETECT_LABELS", "false")
    monkeypatch.setattr(ip, "load_config", config)
    monkeypatch.setattr(ip.log_util, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(ip.log_util, "flush_to_s3", lambda *a, **k: None)
    monkeypatch.setattr(
        ip.boto3,
        "client",
        lambda service: s3 if service == "s3" else types.SimpleNamespace(),
    )
    monkeypatch.setattr(ip.boto3, "resource", lambda service: DynamoStub(table))
    return s3, table


def test_imageprocessor_persists_capture_id_as_frame_id(processor):
    _, table = processor
    capture_id = str(uuid.uuid4())

    ip.process_image(event_for(capture_id), types.SimpleNamespace(aws_request_id="r1"))

    assert len(table.items) == 1
    assert table.items[0]["frame_id"] == capture_id
    assert table.items[0]["s3_key"].endswith("/%s.jpg" % capture_id)


def test_imageprocessor_preserves_legacy_uuid_generation(processor):
    _, table = processor

    ip.process_image(event_for(), types.SimpleNamespace(aws_request_id="r2"))

    assert len(table.items) == 1
    generated = uuid.UUID(table.items[0]["frame_id"])
    assert generated.version == 4
    assert str(generated) == table.items[0]["frame_id"]


@pytest.mark.parametrize(
    "capture_id",
    ["not-a-uuid", "A" * 1000, str(uuid.uuid4()).upper(), None, 123],
)
def test_imageprocessor_rejects_malformed_capture_id(processor, capture_id):
    s3, table = processor

    ip.process_image(event_for(capture_id), types.SimpleNamespace(aws_request_id="r3"))

    assert table.items == []
    assert s3.calls == []
