"""Deterministic ID/FRAME-A consistency tests. No real-face fixtures, no AWS."""
import base64
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

BACKEND = Path(__file__).resolve().parents[1] / "web-ui" / "backend"
sys.path.insert(0, str(BACKEND))

import face_quality  # noqa: E402
import reference_face  # noqa: E402


REPEAT = 20


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def _encode_jpeg(frame):
    import cv2

    ok, buffer = cv2.imencode(".jpg", frame)
    assert ok
    return buffer.tobytes()


def _encode_png(frame):
    import cv2

    ok, buffer = cv2.imencode(".png", frame)
    assert ok
    return buffer.tobytes()


def _gray_frame(width=240, height=320, value=120):
    return np.full((height, width, 3), int(value), dtype=np.uint8)


def _paint_textured_face(frame, x=70, y=80, width=100, height=140):
    frame[y : y + height, x : x + width] = (90, 150, 190)
    frame[y + 30 : y + 42, x + 18 : x + 36] = (20, 20, 20)
    frame[y + 30 : y + 42, x + 64 : x + 82] = (20, 20, 20)
    frame[y + 90 : y + 104, x + 30 : x + 70] = (40, 40, 90)
    return frame


def _valid_jpeg_bytes():
    frame = _gray_frame(value=80)
    _paint_textured_face(frame)
    return _encode_jpeg(frame)


def _valid_png_bytes():
    frame = _gray_frame(value=80)
    _paint_textured_face(frame)
    return _encode_png(frame)


def _settings(**overrides):
    values = dict(
        detection_score_threshold=0.5,
        min_area_ratio=0.08,
        center_tolerance=0.20,
        min_brightness=40.0,
        max_brightness=220.0,
        min_sharpness=15.0,
        min_image_width=80,
        min_image_height=80,
        detect_max_side=320,
        model_path="",
        pose_check_enabled=False,
    )
    values.update(overrides)
    return face_quality.FaceQualitySettings(**values)


class FakeDetector:
    name = "fake"
    trusted = True

    def __init__(self, faces):
        self.faces = list(faces)

    def detect(self, frame_bgr):
        return list(self.faces)


def _face(x, y, width, height, score=1.0):
    return face_quality.DetectedFace(x=x, y=y, width=width, height=height, score=score)


def test_same_jpeg_decodes_identically_twenty_times():
    image_bytes = _valid_jpeg_bytes()
    payload = _b64(image_bytes)
    outcomes = []
    for _ in range(REPEAT):
        decoded, detected = reference_face.decode_face_image(
            payload, content_type="image/jpeg"
        )
        outcomes.append((detected, decoded == image_bytes, len(decoded)))
    assert outcomes == [("image/jpeg", True, len(image_bytes))] * REPEAT


def test_same_png_decodes_identically_twenty_times():
    image_bytes = _valid_png_bytes()
    payload = _b64(image_bytes)
    outcomes = []
    for _ in range(REPEAT):
        decoded, detected = reference_face.decode_face_image(
            payload, content_type="image/png"
        )
        outcomes.append((detected, decoded == image_bytes))
    assert outcomes == [("image/png", True)] * REPEAT


def test_unsupported_image_fails_identically_twenty_times():
    payload = _b64(b"not-an-image")
    reasons = []
    for _ in range(REPEAT):
        with pytest.raises(reference_face.ReferenceFaceError) as error:
            reference_face.decode_face_image(payload, content_type="image/jpeg")
        reasons.append(error.value.reason)
    assert reasons == ["UNSUPPORTED_IMAGE_FORMAT"] * REPEAT


@pytest.mark.parametrize(
    "declared,builder,expected",
    [
        ("image/jpeg", _valid_jpeg_bytes, "image/jpeg"),
        ("image/png", _valid_png_bytes, "image/png"),
        ("image/jpeg", _valid_png_bytes, "image/png"),
        ("image/png", _valid_jpeg_bytes, "image/jpeg"),
    ],
)
def test_allowed_mime_magic_mismatch_trusts_magic(declared, builder, expected):
    image_bytes = builder()
    for _ in range(5):
        decoded, detected = reference_face.decode_face_image(
            _b64(image_bytes), content_type=declared
        )
        assert detected == expected
        assert decoded == image_bytes


def test_declared_jpeg_actual_heic_always_fails():
    heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 32
    info = reference_face.describe_image_payload(
        _b64(heic), content_type="image/jpeg", filename="id.jpg"
    )
    assert info["magic"] == "HEIC"
    assert info["filename_extension"] == "jpg"
    for _ in range(5):
        with pytest.raises(reference_face.ReferenceFaceError) as error:
            reference_face.decode_face_image(_b64(heic), content_type="image/jpeg")
        assert error.value.reason == "UNSUPPORTED_IMAGE_FORMAT"


def test_declared_jpeg_random_bytes_always_fail():
    payload = _b64(b"\x00\x01\x02\x03random-bytes")
    with pytest.raises(reference_face.ReferenceFaceError) as error:
        reference_face.decode_face_image(payload, content_type="image/jpeg")
    assert error.value.reason == "UNSUPPORTED_IMAGE_FORMAT"


def test_exif_orientation_does_not_fail_id_decode():
    jpeg = _valid_jpeg_bytes()
    app1 = b"\xff\xe1\x00\x10Exif\x00\x00" + b"\x00" * 8
    with_exif = jpeg[:2] + app1 + jpeg[2:]
    decoded, detected = reference_face.decode_face_image(
        _b64(with_exif), content_type="image/jpeg"
    )
    assert detected == "image/jpeg"
    assert decoded == with_exif
    info = reference_face.describe_image_payload(_b64(with_exif), content_type="image/jpeg")
    assert info["magic"] == "JPEG"
    assert info["decode_ok"] is True


def test_describe_payload_never_includes_bytes_or_base64():
    image_bytes = _valid_jpeg_bytes()
    payload = _b64(image_bytes)
    info = reference_face.describe_image_payload(
        payload, content_type="image/jpeg", filename="id.jpg"
    )
    dumped = json.dumps(info)
    assert payload not in dumped
    assert "base64" not in dumped.lower()
    assert info["filename_extension"] == "jpg"
    assert info["image_width"] == 240
    assert info["image_height"] == 320


def test_same_id_same_frame_quality_is_deterministic():
    frame = _gray_frame(value=80)
    _paint_textured_face(frame)
    detector = FakeDetector([_face(70, 80, 100, 140)])
    settings = _settings()
    results = [
        face_quality.evaluate_frame(frame, detector=detector, settings=settings)
        for _ in range(REPEAT)
    ]
    reasons = [(item.ok, item.reason) for item in results]
    assert len(set(reasons)) == 1
    assert results[0].ok is True


def test_same_id_decode_pass_when_frame_a_quality_fails():
    id_bytes = _valid_jpeg_bytes()
    id_payload = _b64(id_bytes)
    dark = _gray_frame(value=10)
    dark[80:220, 70:170] = (8, 8, 8)
    blur = _gray_frame(value=80)
    blur[80:220, 70:170] = (90, 150, 190)
    import cv2

    blur = cv2.GaussianBlur(blur, (31, 31), 0)
    tiny = FakeDetector([_face(110, 150, 16, 20)])
    cases = [
        (dark, FakeDetector([_face(70, 80, 100, 140)]), _settings(min_sharpness=0), "TOO_DARK"),
        (blur, FakeDetector([_face(70, 80, 100, 140)]), _settings(min_sharpness=80), "TOO_BLURRY"),
        (_gray_frame(), tiny, _settings(), "FACE_TOO_SMALL"),
        (
            _gray_frame(),
            FakeDetector([_face(4, 8, 90, 120)]),
            _settings(min_area_ratio=0.05, center_tolerance=0.20),
            "FACE_OFF_CENTER",
        ),
    ]
    id_results = []
    quality_reasons = []
    for frame, detector, settings, expected in cases:
        decoded, detected = reference_face.decode_face_image(
            id_payload, content_type="image/jpeg"
        )
        id_results.append((detected, decoded == id_bytes))
        result = face_quality.evaluate_frame(
            frame, detector=detector, settings=settings
        )
        quality_reasons.append(result.reason)
        assert result.ok is False
        assert result.reason == expected
    assert id_results == [("image/jpeg", True)] * len(cases)
    assert quality_reasons == [case[3] for case in cases]


def test_yunet_same_pixels_are_stable():
    frame = _gray_frame(320, 320, value=0)
    settings = face_quality.FaceQualitySettings(
        model_path=str(BACKEND / "models" / "face_detection_yunet_2023mar.onnx"),
        allow_untrusted_detector=False,
        pose_check_enabled=False,
        min_sharpness=0,
    )
    detector = face_quality.build_detector(settings)
    assert detector.name == "yunet"
    results = [
        face_quality.evaluate_frame(frame, detector=detector, settings=settings)
        for _ in range(REPEAT)
    ]
    reasons = [(item.ok, item.reason) for item in results]
    assert len(set(reasons)) == 1


def test_repeat_decode_stats_are_all_pass_or_all_fail():
    jpeg = _b64(_valid_jpeg_bytes())
    bad = _b64(b"not-an-image")
    stats = Counter()
    for _ in range(REPEAT):
        stats["face_verify_total"] += 1
        try:
            reference_face.decode_face_image(jpeg, content_type="image/jpeg")
            stats["id_decode_pass"] += 1
        except reference_face.ReferenceFaceError:
            stats["id_decode_fail"] += 1
        try:
            reference_face.decode_face_image(bad, content_type="image/jpeg")
            stats["unsupported_pass"] += 1
        except reference_face.ReferenceFaceError:
            stats["unsupported_fail"] += 1
    assert stats["id_decode_pass"] == REPEAT
    assert stats["id_decode_fail"] == 0
    assert stats["unsupported_fail"] == REPEAT
    assert stats["unsupported_pass"] == 0
