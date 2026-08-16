"""Local OpenCV face quality gate tests. No AWS, no real-face fixtures."""
import sys
from pathlib import Path

import numpy as np
import pytest

BACKEND = Path(__file__).resolve().parents[1] / "web-ui" / "backend"
sys.path.insert(0, str(BACKEND))

import face_quality  # noqa: E402


class FakeDetector:
    name = "fake"

    def __init__(self, faces):
        self.faces = list(faces)
        self.calls = []

    def detect(self, frame_bgr):
        self.calls.append(frame_bgr.shape[:2])
        return list(self.faces)


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
    )
    values.update(overrides)
    return face_quality.FaceQualitySettings(**values)


def _gray_frame(width=240, height=320, value=120):
    return np.full((height, width, 3), int(value), dtype=np.uint8)


def _paint_rect(frame, x, y, width, height, color):
    frame[y:y + height, x:x + width] = color
    return frame


def _paint_textured_face(frame, x, y, width, height):
    """Synthetic face crop with internal edges so Laplacian variance is non-zero."""
    _paint_rect(frame, x, y, width, height, (90, 150, 190))
    eye_w = max(4, width // 6)
    eye_h = max(4, height // 10)
    _paint_rect(frame, x + width // 5, y + height // 4, eye_w, eye_h, (20, 20, 20))
    _paint_rect(frame, x + width - width // 5 - eye_w, y + height // 4, eye_w, eye_h, (20, 20, 20))
    _paint_rect(
        frame,
        x + width // 3,
        y + (2 * height) // 3,
        max(8, width // 3),
        max(4, height // 12),
        (40, 40, 90),
    )
    return frame


def _encode_jpeg(frame):
    import cv2

    ok, buffer = cv2.imencode(".jpg", frame)
    assert ok
    return buffer.tobytes()


def _face(x, y, width, height, score=1.0, landmarks=None):
    return face_quality.DetectedFace(
        x=x, y=y, width=width, height=height, score=score, landmarks=landmarks
    )


def _landmarks(x, y, width, height, tilt_deg=0.0, yaw_ratio=0.0):
    """Synthetic YuNet-style 5-point landmarks inside a bbox."""
    import math

    mid_x = x + width / 2.0
    mid_y = y + height * 0.35
    half = width * 0.20
    angle = math.radians(tilt_deg)
    # Person's right eye is on the image-left for a frontal face.
    right_eye = (
        mid_x - half * math.cos(angle),
        mid_y - half * math.sin(angle),
    )
    left_eye = (
        mid_x + half * math.cos(angle),
        mid_y + half * math.sin(angle),
    )
    eye_dist = math.hypot(left_eye[0] - right_eye[0], left_eye[1] - right_eye[1])
    nose = (mid_x + yaw_ratio * eye_dist, y + height * 0.55)
    return {
        "right_eye": right_eye,
        "left_eye": left_eye,
        "nose": nose,
        "mouth_right": (x + width * 0.35, y + height * 0.75),
        "mouth_left": (x + width * 0.65, y + height * 0.75),
    }


def test_valid_centered_face_passes():
    frame = _gray_frame(value=80)
    _paint_textured_face(frame, 70, 80, 100, 140)
    detector = FakeDetector([_face(70, 80, 100, 140)])
    result = face_quality.evaluate_frame(
        frame, detector=detector, settings=_settings(min_area_ratio=0.08)
    )
    assert result.ok is True
    assert result.reason is None
    assert result.metrics["face_count"] == 1
    assert result.metrics["face_ratio"] > 0.08
    assert result.metrics["brightness"] is not None
    assert result.metrics["sharpness"] is not None


def test_no_face():
    frame = _gray_frame()
    result = face_quality.evaluate_frame(
        frame, detector=FakeDetector([]), settings=_settings()
    )
    assert result.ok is False
    assert result.reason == "NO_FACE"
    assert result.metrics["face_count"] == 0


def test_multiple_faces():
    frame = _gray_frame()
    detector = FakeDetector([_face(20, 40, 80, 100), _face(140, 40, 80, 100)])
    result = face_quality.evaluate_frame(
        frame, detector=detector, settings=_settings()
    )
    assert result.ok is False
    assert result.reason == "MULTIPLE_FACES"
    assert result.metrics["face_count"] == 2


def test_face_too_small():
    frame = _gray_frame()
    detector = FakeDetector([_face(110, 150, 16, 20)])
    result = face_quality.evaluate_frame(
        frame, detector=detector, settings=_settings(min_area_ratio=0.08)
    )
    assert result.ok is False
    assert result.reason == "FACE_TOO_SMALL"


def test_face_off_center():
    frame = _gray_frame()
    detector = FakeDetector([_face(4, 8, 90, 120)])
    result = face_quality.evaluate_frame(
        frame,
        detector=detector,
        settings=_settings(min_area_ratio=0.05, center_tolerance=0.20),
    )
    assert result.ok is False
    assert result.reason == "FACE_OFF_CENTER"


def test_too_dark():
    frame = _gray_frame(value=10)
    _paint_rect(frame, 70, 80, 100, 140, (8, 8, 8))
    detector = FakeDetector([_face(70, 80, 100, 140)])
    result = face_quality.evaluate_frame(
        frame,
        detector=detector,
        settings=_settings(min_brightness=40, min_sharpness=0),
    )
    assert result.ok is False
    assert result.reason == "TOO_DARK"


def test_too_bright():
    frame = _gray_frame(value=250)
    _paint_rect(frame, 70, 80, 100, 140, (255, 255, 255))
    detector = FakeDetector([_face(70, 80, 100, 140)])
    result = face_quality.evaluate_frame(
        frame,
        detector=detector,
        settings=_settings(max_brightness=220, min_sharpness=0),
    )
    assert result.ok is False
    assert result.reason == "TOO_BRIGHT"


def test_too_blurry():
    frame = _gray_frame(value=80)
    _paint_rect(frame, 70, 80, 100, 140, (90, 150, 190))
    import cv2

    frame = cv2.GaussianBlur(frame, (31, 31), 0)
    detector = FakeDetector([_face(70, 80, 100, 140)])
    result = face_quality.evaluate_frame(
        frame,
        detector=detector,
        settings=_settings(min_sharpness=80.0, min_brightness=10, max_brightness=250),
    )
    assert result.ok is False
    assert result.reason == "TOO_BLURRY"


def test_invalid_image_bytes():
    result = face_quality.evaluate(b"not-an-image")
    assert result.ok is False
    assert result.reason == "INVALID_IMAGE"


def test_missing_opencv_is_detector_unavailable(monkeypatch):
    def boom():
        raise ImportError("No module named 'cv2'")

    monkeypatch.setattr(face_quality, "_cv2_module", boom)
    result = face_quality.evaluate(b"\xff\xd8\xff\xe0" + b"x" * 80)
    assert result.ok is False
    assert result.reason == "QUALITY_DETECTOR_UNAVAILABLE"
    assert result.detector_name == "none"


def test_quality_debug_logs_have_no_image_or_base64(caplog):
    import logging

    frame = _gray_frame(value=80)
    _paint_textured_face(frame, 70, 80, 100, 140)
    image_bytes = _encode_jpeg(frame)
    detector = FakeDetector([_face(70, 80, 100, 140)])
    with caplog.at_level(logging.INFO, logger="webui.face_quality"):
        result = face_quality.evaluate(
            image_bytes, detector=detector, settings=_settings(min_area_ratio=0.08)
        )
    assert result.ok is True
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "event=quality_gate_debug" in joined
    assert "result=PASS" in joined
    assert "detection_score=" in joined
    assert "base64" not in joined.lower()
    assert "data:image" not in joined.lower()
    assert image_bytes[:16] not in joined.encode("utf-8", errors="ignore")


def test_empty_bytes_are_invalid():
    result = face_quality.evaluate(b"")
    assert result.ok is False
    assert result.reason == "INVALID_IMAGE"


def test_tiny_frame_is_invalid():
    frame = _gray_frame(width=20, height=20)
    result = face_quality.evaluate_frame(
        frame, detector=FakeDetector([_face(2, 2, 10, 12)]), settings=_settings()
    )
    assert result.ok is False
    assert result.reason == "INVALID_IMAGE"


def test_detector_exception_is_quality_check_failed():
    class Boom:
        name = "boom"

        def detect(self, frame_bgr):
            raise RuntimeError("detector exploded")

    result = face_quality.evaluate_frame(
        _gray_frame(), detector=Boom(), settings=_settings()
    )
    assert result.ok is False
    assert result.reason == "QUALITY_CHECK_FAILED"


def test_low_score_faces_count_as_no_face():
    frame = _gray_frame()
    detector = FakeDetector([_face(70, 80, 100, 140, score=0.1)])
    result = face_quality.evaluate_frame(
        frame, detector=detector, settings=_settings(detection_score_threshold=0.6)
    )
    assert result.ok is False
    assert result.reason == "NO_FACE"


def test_evaluate_bytes_reuses_decoded_metrics_and_is_fast():
    frame = _gray_frame(value=80)
    _paint_textured_face(frame, 70, 80, 100, 140)
    image_bytes = _encode_jpeg(frame)
    detector = FakeDetector([_face(70, 80, 100, 140)])
    result = face_quality.evaluate(
        image_bytes, detector=detector, settings=_settings(min_area_ratio=0.08)
    )
    assert result.ok is True
    assert result.metrics["elapsed_ms"] < 2000


def test_untrusted_detector_is_fail_closed_unless_allowed():
    class Untrusted:
        name = "contour_fallback"
        trusted = False

        def detect(self, frame_bgr):
            return [_face(70, 80, 100, 140)]

    frame = _gray_frame()
    blocked = face_quality.evaluate_frame(
        frame,
        detector=Untrusted(),
        settings=_settings(allow_untrusted_detector=False),
    )
    assert blocked.ok is False
    assert blocked.reason == "QUALITY_DETECTOR_UNAVAILABLE"


def test_quality_fail_reasons_are_distinct():
    assert "BAD_IMAGE" not in face_quality.QUALITY_FAIL_REASONS
    assert face_quality.REASON_NO_FACE != face_quality.REASON_TOO_BLURRY
    assert len(face_quality.QUALITY_FAIL_REASONS) >= 8


def _official_yunet_path():
    return BACKEND / "models" / "face_detection_yunet_2023mar.onnx"


def test_official_yunet_model_exists_and_matches_pinned_sha256():
    import hashlib

    model = _official_yunet_path()
    checksum = Path(str(model) + ".sha256")
    assert model.is_file()
    expected = checksum.read_text(encoding="utf-8").strip().split()[0]
    actual = hashlib.sha256(model.read_bytes()).hexdigest()
    assert actual == expected
    assert expected == "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"


def test_facedetectoryn_loads_official_yunet_without_crash():
    import cv2

    model = str(_official_yunet_path())
    detector = cv2.FaceDetectorYN.create(model, "", (320, 320), 0.6, 0.3, 5000)
    assert detector is not None
    blank = _gray_frame(320, 320, value=0)
    detector.setInputSize((320, 320))
    raw = detector.detect(blank)
    assert raw is not None


def test_build_detector_selects_trusted_yunet_from_file_path():
    settings = _settings(model_path=str(_official_yunet_path()))
    detector = face_quality.build_detector(settings)
    assert detector.name == "yunet"
    assert detector.trusted is True


def test_resolve_yunet_uses_file_relative_models_dir_not_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    found = face_quality.resolve_yunet_model_path("")
    assert found
    assert found.endswith("face_detection_yunet_2023mar.onnx")
    assert Path(found).is_file()


def test_missing_yunet_production_is_unavailable(monkeypatch):
    monkeypatch.setattr(face_quality, "resolve_yunet_model_path", lambda configured="": "")
    monkeypatch.setattr(face_quality, "_try_haar_detector", lambda: None)
    detector = face_quality.build_detector(_settings(allow_untrusted_detector=False))
    assert detector.name == "unavailable"
    assert detector.trusted is False
    result = face_quality.evaluate_frame(
        _gray_frame(), detector=detector, settings=_settings(allow_untrusted_detector=False)
    )
    assert result.ok is False
    assert result.reason == "QUALITY_DETECTOR_UNAVAILABLE"


def test_missing_yunet_dev_flag_allows_contour_fallback(monkeypatch):
    monkeypatch.setattr(face_quality, "resolve_yunet_model_path", lambda configured="": "")
    monkeypatch.setattr(face_quality, "_try_haar_detector", lambda: None)
    detector = face_quality.build_detector(_settings(allow_untrusted_detector=True))
    assert detector.name == "contour_fallback"
    assert detector.trusted is False


def test_strict_default_profile_is_stricter_than_original():
    settings = face_quality.FaceQualitySettings()
    assert settings.detection_score_threshold == 0.75
    assert settings.min_area_ratio == 0.08
    assert settings.center_tolerance == 0.20
    assert settings.min_brightness == 55.0
    assert settings.max_brightness == 205.0
    assert settings.min_sharpness == 30.0
    assert settings.pose_check_enabled is True
    assert settings.max_eye_tilt_degrees == 20.0
    assert settings.max_yaw_ratio == 0.38


def test_strict_score_threshold_rejects_mid_confidence():
    frame = _gray_frame()
    _paint_textured_face(frame, 70, 80, 100, 140)
    detector = FakeDetector([_face(70, 80, 100, 140, score=0.70)])
    result = face_quality.evaluate_frame(
        frame, detector=detector, settings=face_quality.FaceQualitySettings()
    )
    assert result.ok is False
    assert result.reason == "NO_FACE"
    assert result.metrics["detection_score"] == 0.70


def test_strict_min_area_rejects_former_borderline_face():
    frame = _gray_frame()
    # 62x80 / (240x320) ≈ 0.0646, above the old 0.05 and below 0.08.
    detector = FakeDetector([_face(89, 120, 62, 80)])
    result = face_quality.evaluate_frame(
        frame, detector=detector, settings=face_quality.FaceQualitySettings()
    )
    assert result.ok is False
    assert result.reason == "FACE_TOO_SMALL"
    assert 0.05 < result.metrics["face_ratio"] < 0.08


def test_strict_center_tolerance_rejects_former_borderline_offset():
    frame = _gray_frame(value=80)
    _paint_textured_face(frame, 17, 100, 90, 120)
    # center_x = 17 + 45 = 62; |62/240 - 0.5| = 0.2417, between 0.20 and 0.28.
    detector = FakeDetector([_face(17, 100, 90, 120)])
    loose = face_quality.FaceQualitySettings(center_tolerance=0.28)
    strict = face_quality.FaceQualitySettings()
    loose_result = face_quality.evaluate_frame(
        frame, detector=detector, settings=loose
    )
    strict_result = face_quality.evaluate_frame(
        frame, detector=detector, settings=strict
    )
    assert 0.20 < strict_result.metrics["offset_x"] < 0.28
    assert loose_result.ok is True
    assert strict_result.ok is False
    assert strict_result.reason == "FACE_OFF_CENTER"


def test_strict_min_brightness_rejects_former_dim_face():
    frame = _gray_frame(value=80)
    _paint_rect(frame, 70, 80, 100, 140, (48, 48, 48))
    detector = FakeDetector([_face(70, 80, 100, 140)])
    result = face_quality.evaluate_frame(
        frame,
        detector=detector,
        settings=face_quality.FaceQualitySettings(min_sharpness=0),
    )
    assert result.ok is False
    assert result.reason == "TOO_DARK"
    assert 40 <= result.metrics["brightness"] < 55


def test_strict_max_brightness_rejects_former_bright_face():
    frame = _gray_frame(value=80)
    _paint_rect(frame, 70, 80, 100, 140, (215, 215, 215))
    detector = FakeDetector([_face(70, 80, 100, 140)])
    result = face_quality.evaluate_frame(
        frame,
        detector=detector,
        settings=face_quality.FaceQualitySettings(min_sharpness=0),
    )
    assert result.ok is False
    assert result.reason == "TOO_BRIGHT"
    assert 205 < result.metrics["brightness"] <= 220


def test_strict_sharpness_rejects_flat_face_roi():
    frame = _gray_frame(value=80)
    _paint_rect(frame, 70, 80, 100, 140, (90, 150, 190))
    detector = FakeDetector([_face(70, 80, 100, 140)])
    result = face_quality.evaluate_frame(
        frame, detector=detector, settings=face_quality.FaceQualitySettings()
    )
    assert result.ok is False
    assert result.reason == "TOO_BLURRY"
    assert result.metrics["sharpness"] < 30


def test_frontal_landmarks_pass_pose_check():
    frame = _gray_frame(value=80)
    _paint_textured_face(frame, 70, 80, 100, 140)
    detector = FakeDetector(
        [_face(70, 80, 100, 140, landmarks=_landmarks(70, 80, 100, 140))]
    )
    result = face_quality.evaluate_frame(
        frame, detector=detector, settings=face_quality.FaceQualitySettings()
    )
    assert result.ok is True
    assert result.metrics["eye_angle"] is not None
    assert abs(result.metrics["eye_angle"]) < 5
    assert abs(result.metrics["yaw_ratio"]) < 0.1


def test_tilted_landmarks_fail_face_tilted():
    frame = _gray_frame(value=80)
    _paint_textured_face(frame, 70, 80, 100, 140)
    detector = FakeDetector(
        [
            _face(
                70,
                80,
                100,
                140,
                landmarks=_landmarks(70, 80, 100, 140, tilt_deg=32),
            )
        ]
    )
    result = face_quality.evaluate_frame(
        frame, detector=detector, settings=face_quality.FaceQualitySettings()
    )
    assert result.ok is False
    assert result.reason == "FACE_TILTED"
    assert abs(result.metrics["eye_angle"]) > 20


def test_yawed_landmarks_fail_pose_invalid():
    frame = _gray_frame(value=80)
    _paint_textured_face(frame, 70, 80, 100, 140)
    detector = FakeDetector(
        [
            _face(
                70,
                80,
                100,
                140,
                landmarks=_landmarks(70, 80, 100, 140, yaw_ratio=0.55),
            )
        ]
    )
    result = face_quality.evaluate_frame(
        frame, detector=detector, settings=face_quality.FaceQualitySettings()
    )
    assert result.ok is False
    assert result.reason == "FACE_POSE_INVALID"
    assert abs(result.metrics["yaw_ratio"]) > 0.38


def test_pose_check_skipped_without_landmarks():
    frame = _gray_frame(value=80)
    _paint_textured_face(frame, 70, 80, 100, 140)
    detector = FakeDetector([_face(70, 80, 100, 140)])
    result = face_quality.evaluate_frame(
        frame, detector=detector, settings=face_quality.FaceQualitySettings()
    )
    assert result.ok is True
    assert result.metrics["eye_angle"] is None
    assert result.metrics["yaw_ratio"] is None


def test_pose_disabled_allows_tilt():
    frame = _gray_frame(value=80)
    _paint_textured_face(frame, 70, 80, 100, 140)
    detector = FakeDetector(
        [
            _face(
                70,
                80,
                100,
                140,
                landmarks=_landmarks(70, 80, 100, 140, tilt_deg=32),
            )
        ]
    )
    result = face_quality.evaluate_frame(
        frame,
        detector=detector,
        settings=face_quality.FaceQualitySettings(pose_check_enabled=False),
    )
    assert result.ok is True
    assert abs(result.metrics["eye_angle"]) > 20


def test_yunet_row_keeps_landmarks_for_pose():
    row = [
        10,
        20,
        80,
        100,
        20,
        40,
        70,
        40,
        45,
        60,
        30,
        90,
        60,
        90,
        0.91,
    ]
    faces = face_quality._faces_from_yunet_mat([row])
    assert len(faces) == 1
    assert faces[0].score == 0.91
    assert faces[0].landmarks["right_eye"] == (20.0, 40.0)
    assert faces[0].landmarks["left_eye"] == (70.0, 40.0)
    pose = face_quality.pose_from_landmarks(faces[0].landmarks)
    assert pose is not None
    assert abs(pose["eye_angle"]) < 1.0
    assert abs(pose["yaw_ratio"]) < 0.05


def test_quality_fail_reasons_include_pose():
    assert "FACE_TILTED" in face_quality.QUALITY_FAIL_REASONS
    assert "FACE_POSE_INVALID" in face_quality.QUALITY_FAIL_REASONS
    assert "BAD_IMAGE" not in face_quality.QUALITY_FAIL_REASONS


def test_settings_from_config_honors_monkeypatched_env_values(monkeypatch):
    monkeypatch.setattr(face_quality.config, "FACE_DETECTION_SCORE_THRESHOLD", 0.81)
    monkeypatch.setattr(face_quality.config, "FACE_MIN_AREA_RATIO", 0.09)
    monkeypatch.setattr(face_quality.config, "FACE_CENTER_TOLERANCE", 0.19)
    monkeypatch.setattr(face_quality.config, "FACE_MIN_BRIGHTNESS", 60)
    monkeypatch.setattr(face_quality.config, "FACE_MAX_BRIGHTNESS", 200)
    monkeypatch.setattr(face_quality.config, "FACE_MIN_SHARPNESS", 33)
    monkeypatch.setattr(face_quality.config, "FACE_POSE_CHECK_ENABLED", False)
    monkeypatch.setattr(face_quality.config, "FACE_MAX_EYE_TILT_DEGREES", 18)
    monkeypatch.setattr(face_quality.config, "FACE_MAX_YAW_RATIO", 0.3)
    settings = face_quality.settings_from_config()
    assert settings.detection_score_threshold == 0.81
    assert settings.min_area_ratio == 0.09
    assert settings.center_tolerance == 0.19
    assert settings.min_brightness == 60
    assert settings.max_brightness == 200
    assert settings.min_sharpness == 33
    assert settings.pose_check_enabled is False
    assert settings.max_eye_tilt_degrees == 18
    assert settings.max_yaw_ratio == 0.3
