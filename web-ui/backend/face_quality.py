"""Local OpenCV face quality gate for kiosk STORE/RETRIEVE camera frames.

Runs on the FastAPI host before any Rekognition CompareFaces call so that
empty, multi-person, tiny, off-center, dark, bright, blurry, or severely
non-frontal captures are rejected locally. The caller must pass the same
decoded JPEG/PNG bytes that will later be sent to AWS when this gate
returns ok=True. This is a capture-quality gate, not Face Liveness.

Detector selection (first match):
  1. cv2.FaceDetectorYN (YuNet) when the official ONNX is next to this file
     at models/face_detection_yunet_2023mar.onnx, or FACE_DETECTOR_MODEL_PATH
  2. cv2.CascadeClassifier Haar XML when OpenCV still bundles it (4.x)
  3. Contour/skin-blob fallback only when FACE_QUALITY_ALLOW_UNTRUSTED_DETECTOR

Production (allow_untrusted=false): missing or unloadable YuNet →
QUALITY_DETECTOR_UNAVAILABLE and no AWS Rekognition. Paths are resolved from
__file__, not process CWD.

PRIVACY: never log image bytes, base64, or face crops. Metrics only.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Protocol, Sequence

import config

logger = logging.getLogger("webui.face_quality")

# Internal reasons. Kept distinct so the kiosk can show a specific recapture
# prompt. These are quality problems, not biometric mismatches.
REASON_NO_FACE = "NO_FACE"
REASON_MULTIPLE_FACES = "MULTIPLE_FACES"
REASON_FACE_TOO_SMALL = "FACE_TOO_SMALL"
REASON_FACE_OFF_CENTER = "FACE_OFF_CENTER"
REASON_TOO_DARK = "TOO_DARK"
REASON_TOO_BRIGHT = "TOO_BRIGHT"
REASON_TOO_BLURRY = "TOO_BLURRY"
REASON_FACE_TILTED = "FACE_TILTED"
REASON_FACE_POSE_INVALID = "FACE_POSE_INVALID"
REASON_INVALID_IMAGE = "INVALID_IMAGE"
REASON_QUALITY_CHECK_FAILED = "QUALITY_CHECK_FAILED"
REASON_DETECTOR_UNAVAILABLE = "QUALITY_DETECTOR_UNAVAILABLE"

QUALITY_FAIL_REASONS = frozenset(
    {
        REASON_NO_FACE,
        REASON_MULTIPLE_FACES,
        REASON_FACE_TOO_SMALL,
        REASON_FACE_OFF_CENTER,
        REASON_TOO_DARK,
        REASON_TOO_BRIGHT,
        REASON_TOO_BLURRY,
        REASON_FACE_TILTED,
        REASON_FACE_POSE_INVALID,
        REASON_INVALID_IMAGE,
        REASON_QUALITY_CHECK_FAILED,
        REASON_DETECTOR_UNAVAILABLE,
    }
)

_YUNET_FILENAMES = (
    "face_detection_yunet_2023mar.onnx",
    "face_detection_yunet.onnx",
    "yunet.onnx",
)

_cv2 = None
_cv2_import_error = None
_gate_lock = threading.Lock()
_gate = None


def _cv2_module():
    """Lazy import so the app can still start if OpenCV is missing."""
    global _cv2, _cv2_import_error
    if _cv2 is not None:
        return _cv2
    if _cv2_import_error is not None:
        raise _cv2_import_error
    try:
        import cv2  # type: ignore
        import numpy as np  # noqa: F401
    except Exception as exc:  # pragma: no cover - import environment
        _cv2_import_error = exc
        raise
    _cv2 = cv2
    return _cv2


@dataclass(frozen=True)
class FaceQualitySettings:
    """Thresholds for the local gate. Calibrate on the real kiosk camera.

    All defaults are conservative starting points, not claimed standards.
    """

    # YuNet/Haar score in [0, 1]. Raise → fewer detections (more NO_FACE).
    # Lower → more detections (more false faces). Unit: detector confidence.
    # Strict kiosk 1st calibration default. Not a claimed industry standard.
    detection_score_threshold: float = 0.75
    # Minimum face bbox area / frame area. Raise → require a closer face.
    # Lower → allow smaller / farther faces. Unit: ratio 0-1.
    min_area_ratio: float = 0.08
    # Max |center - 0.5| on each axis, in frame-normalized units.
    # Raise → allow faces farther from the center. Lower → stricter centering.
    center_tolerance: float = 0.20
    # Mean grayscale of the face ROI. Raise → reject more dim faces.
    # Lower → allow darker captures. Unit: 0-255.
    min_brightness: float = 55.0
    # Mean grayscale of the face ROI. Lower → reject more bright faces.
    # Raise → allow brighter captures. Unit: 0-255.
    max_brightness: float = 205.0
    # Laplacian variance of the face ROI. Raise → require sharper images.
    # Lower → allow more motion blur. Unit: variance of Laplacian (CV_64F).
    # This unit is ROI-size and lighting dependent, not a universal blur score.
    min_sharpness: float = 30.0
    min_image_width: int = 80
    min_image_height: int = 80
    # Longest side used for detection (boxes are scaled back). Smaller is
    # cheaper; too small misses distant faces.
    detect_max_side: int = 320
    model_path: str = ""
    allow_untrusted_detector: bool = False
    # YuNet 2D landmark heuristics. Disabled or missing landmarks → skip.
    # Not a 3D head-pose solver and not Face Liveness.
    pose_check_enabled: bool = True
    # Max |inter-ocular line vs image horizontal| in degrees.
    # Raise → allow more roll. Lower → stricter upright face.
    max_eye_tilt_degrees: float = 20.0
    # Max |nose_x - eye_mid_x| / inter-ocular distance.
    # Raise → allow more left/right turn. Lower → stricter facing-camera.
    max_yaw_ratio: float = 0.38


@dataclass
class DetectedFace:
    x: float
    y: float
    width: float
    height: float
    score: float = 1.0
    landmarks: Optional[dict] = None

    @property
    def area(self) -> float:
        return max(0.0, float(self.width)) * max(0.0, float(self.height))

    @property
    def center_x(self) -> float:
        return float(self.x) + float(self.width) / 2.0

    @property
    def center_y(self) -> float:
        return float(self.y) + float(self.height) / 2.0


@dataclass
class FaceQualityResult:
    ok: bool
    reason: Optional[str] = None
    metrics: dict = field(default_factory=dict)
    detector_name: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": bool(self.ok),
            "reason": self.reason,
            "metrics": dict(self.metrics),
            "detector_name": self.detector_name,
        }


class FaceDetector(Protocol):
    name: str

    def detect(self, frame_bgr) -> Sequence[DetectedFace]:
        ...


def settings_from_config() -> FaceQualitySettings:
    return FaceQualitySettings(
        detection_score_threshold=float(config.FACE_DETECTION_SCORE_THRESHOLD),
        min_area_ratio=float(config.FACE_MIN_AREA_RATIO),
        center_tolerance=float(config.FACE_CENTER_TOLERANCE),
        min_brightness=float(config.FACE_MIN_BRIGHTNESS),
        max_brightness=float(config.FACE_MAX_BRIGHTNESS),
        min_sharpness=float(config.FACE_MIN_SHARPNESS),
        min_image_width=int(config.FACE_MIN_IMAGE_WIDTH),
        min_image_height=int(config.FACE_MIN_IMAGE_HEIGHT),
        detect_max_side=int(config.FACE_DETECT_MAX_SIDE),
        model_path=str(config.FACE_DETECTOR_MODEL_PATH or ""),
        allow_untrusted_detector=bool(config.FACE_QUALITY_ALLOW_UNTRUSTED_DETECTOR),
        pose_check_enabled=bool(config.FACE_POSE_CHECK_ENABLED),
        max_eye_tilt_degrees=float(config.FACE_MAX_EYE_TILT_DEGREES),
        max_yaw_ratio=float(config.FACE_MAX_YAW_RATIO),
    )


def default_yunet_search_paths() -> List[str]:
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    models_dir = os.path.join(backend_dir, "models")
    return [os.path.join(models_dir, name) for name in _YUNET_FILENAMES]


def resolve_yunet_model_path(configured_path=""):
    candidates = []
    if configured_path:
        candidates.append(configured_path)
    candidates.extend(default_yunet_search_paths())
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return ""


class YuNetFaceDetector:
    """OpenCV FaceDetectorYN (YuNet). Requires a local ONNX file."""

    name = "yunet"
    trusted = True

    def __init__(self, model_path, score_threshold=0.75):
        self.model_path = model_path
        self.score_threshold = float(score_threshold)
        self._detector = None
        self._input_size = None
        self._lock = threading.Lock()

    def _create(self, width, height):
        cv2 = _cv2_module()
        create = getattr(cv2, "FaceDetectorYN_create", None)
        if create is None and hasattr(cv2, "FaceDetectorYN"):
            create = cv2.FaceDetectorYN.create
        if create is None:
            raise RuntimeError("cv2.FaceDetectorYN is not available")
        return create(
            self.model_path,
            "",
            (int(width), int(height)),
            float(self.score_threshold),
            0.3,
            5000,
        )

    def ensure_loaded(self, width=320, height=320):
        """Create the OpenCV detector once. Raises if the ONNX cannot be loaded."""
        with self._lock:
            if self._detector is None:
                self._detector = self._create(width, height)
                self._input_size = (width, height)
        return self

    def detect(self, frame_bgr) -> List[DetectedFace]:
        height, width = frame_bgr.shape[:2]
        with self._lock:
            if self._detector is None:
                self._detector = self._create(width, height)
                self._input_size = (width, height)
            elif self._input_size != (width, height):
                self._detector.setInputSize((int(width), int(height)))
                self._input_size = (width, height)
            raw = self._detector.detect(frame_bgr)
        faces_mat = raw[-1] if isinstance(raw, tuple) else raw
        return _faces_from_yunet_mat(faces_mat)


class HaarCascadeDetector:
    """OpenCV Haar cascade. Available on OpenCV 4.x wheels that bundle XML."""

    name = "haar_cascade"
    trusted = True

    def __init__(self, classifier):
        self._classifier = classifier

    def detect(self, frame_bgr) -> List[DetectedFace]:
        cv2 = _cv2_module()
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        rects = self._classifier.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24)
        )
        faces = []
        for item in rects:
            x, y, width, height = [float(v) for v in item[:4]]
            faces.append(
                DetectedFace(x=x, y=y, width=width, height=height, score=1.0)
            )
        return faces


class UnavailableDetector:
    """Used when no trusted detector can be loaded in production."""

    name = "unavailable"
    trusted = False

    def detect(self, frame_bgr) -> List[DetectedFace]:
        return []


class ContourFaceDetector:
    """File-free fallback: YCrCb skin blobs, then grayscale contrast blobs.

    This is not as accurate as YuNet. Install the YuNet ONNX for production
    kiosk cameras. Used so the gate still runs when no model file is present.
    """

    name = "contour_fallback"
    trusted = False

    def detect(self, frame_bgr) -> List[DetectedFace]:
        faces = _skin_blobs(frame_bgr)
        if not faces:
            faces = _contrast_blobs(frame_bgr)
        return _nms_faces(faces, iou_threshold=0.3)


def _try_haar_detector():
    cv2 = _cv2_module()
    if not hasattr(cv2, "CascadeClassifier"):
        return None
    data = getattr(cv2, "data", None)
    base = getattr(data, "haarcascades", None) if data is not None else None
    if not base:
        return None
    xml_path = os.path.join(base, "haarcascade_frontalface_default.xml")
    if not os.path.isfile(xml_path):
        # Some wheels point haarcascades at the data dir itself.
        xml_path = os.path.join(base, "haarcascades", "haarcascade_frontalface_default.xml")
    if not os.path.isfile(xml_path):
        return None
    classifier = cv2.CascadeClassifier(xml_path)
    if classifier is None or getattr(classifier, "empty", lambda: False)():
        return None
    return HaarCascadeDetector(classifier)


def build_detector(settings: Optional[FaceQualitySettings] = None) -> FaceDetector:
    settings = settings or settings_from_config()
    model_path = resolve_yunet_model_path(settings.model_path)
    if model_path:
        try:
            detector = YuNetFaceDetector(
                model_path, score_threshold=settings.detection_score_threshold
            )
            detector.ensure_loaded(320, 320)
            logger.info("face_detector=yunet model_present=true")
            return detector
        except Exception:
            logger.warning(
                "face_detector=yunet_load_failed path_configured=true"
            )
            if not settings.allow_untrusted_detector:
                return UnavailableDetector()
    haar = _try_haar_detector()
    if haar is not None:
        logger.info("face_detector=haar_cascade model_present=false")
        return haar
    if not settings.allow_untrusted_detector:
        logger.info(
            "face_detector=unavailable model_present=false "
            "reason=QUALITY_DETECTOR_UNAVAILABLE"
        )
        return UnavailableDetector()
    logger.info(
        "face_detector=contour_fallback model_present=false "
        "hint=install_yunet_onnx_for_better_detection"
    )
    return ContourFaceDetector()


def _faces_from_yunet_mat(faces_mat) -> List[DetectedFace]:
    if faces_mat is None:
        return []
    try:
        rows = list(faces_mat)
    except TypeError:
        return []
    faces = []
    for row in rows:
        try:
            values = [float(v) for v in row]
        except (TypeError, ValueError):
            continue
        if len(values) < 4:
            continue
        score = values[14] if len(values) > 14 else 1.0
        landmarks = None
        if len(values) >= 14:
            landmarks = {
                "right_eye": (values[4], values[5]),
                "left_eye": (values[6], values[7]),
                "nose": (values[8], values[9]),
                "mouth_right": (values[10], values[11]),
                "mouth_left": (values[12], values[13]),
            }
        faces.append(
            DetectedFace(
                x=values[0],
                y=values[1],
                width=values[2],
                height=values[3],
                score=score,
                landmarks=landmarks,
            )
        )
    return faces


def _skin_blobs(frame_bgr) -> List[DetectedFace]:
    cv2 = _cv2_module()
    import numpy as np

    ycrcb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2YCrCb)
    lower = np.array([0, 133, 77], dtype=np.uint8)
    upper = np.array([255, 173, 127], dtype=np.uint8)
    mask = cv2.inRange(ycrcb, lower, upper)
    return _boxes_from_mask(mask, frame_bgr.shape[0], frame_bgr.shape[1])


def _contrast_blobs(frame_bgr) -> List[DetectedFace]:
    cv2 = _cv2_module()
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    _, mask = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return _boxes_from_mask(mask, frame_bgr.shape[0], frame_bgr.shape[1])


def _boxes_from_mask(mask, frame_h, frame_w) -> List[DetectedFace]:
    cv2 = _cv2_module()
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    found = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = found[0] if len(found) == 2 else found[1]
    min_area = float(frame_h * frame_w) * 0.004
    max_area = float(frame_h * frame_w) * 0.85
    faces = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        area = float(width * height)
        if area < min_area or area > max_area:
            continue
        if height <= 0:
            continue
        aspect = width / float(height)
        if aspect < 0.45 or aspect > 1.85:
            continue
        faces.append(
            DetectedFace(
                x=float(x),
                y=float(y),
                width=float(width),
                height=float(height),
                score=1.0,
            )
        )
    return faces


def _iou(left: DetectedFace, right: DetectedFace) -> float:
    ax2 = left.x + left.width
    ay2 = left.y + left.height
    bx2 = right.x + right.width
    by2 = right.y + right.height
    ix1 = max(left.x, right.x)
    iy1 = max(left.y, right.y)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = left.area + right.area - inter
    if union <= 0:
        return 0.0
    return inter / union


def _nms_faces(faces: Sequence[DetectedFace], iou_threshold=0.3) -> List[DetectedFace]:
    ordered = sorted(faces, key=lambda face: face.area, reverse=True)
    kept: List[DetectedFace] = []
    for face in ordered:
        if any(_iou(face, existing) > iou_threshold for existing in kept):
            continue
        kept.append(face)
    return kept


def decode_bgr_image(image_bytes):
    """Decode JPEG/PNG bytes to a BGR ndarray. Returns None on failure."""
    if not image_bytes:
        return None
    try:
        cv2 = _cv2_module()
        import numpy as np
    except Exception:
        return None
    try:
        buffer = np.frombuffer(bytes(image_bytes), dtype=np.uint8)
        if buffer.size == 0:
            return None
        frame = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    except Exception:
        return None
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    return frame


def _downscale_for_detection(frame_bgr, max_side):
    cv2 = _cv2_module()
    height, width = frame_bgr.shape[:2]
    longest = max(height, width)
    if longest <= max_side or max_side <= 0:
        return frame_bgr, 1.0
    scale = float(max_side) / float(longest)
    resized = cv2.resize(
        frame_bgr,
        (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
        interpolation=cv2.INTER_AREA,
    )
    return resized, scale


def _scale_faces(faces: Sequence[DetectedFace], scale: float) -> List[DetectedFace]:
    if scale == 1.0:
        return list(faces)
    if scale <= 0:
        return list(faces)
    inv = 1.0 / scale
    scaled = []
    for face in faces:
        landmarks = None
        if face.landmarks:
            landmarks = {
                key: (point[0] * inv, point[1] * inv)
                for key, point in face.landmarks.items()
            }
        scaled.append(
            DetectedFace(
                x=face.x * inv,
                y=face.y * inv,
                width=face.width * inv,
                height=face.height * inv,
                score=face.score,
                landmarks=landmarks,
            )
        )
    return scaled


def _clamp_box(face: DetectedFace, frame_w: int, frame_h: int) -> Optional[tuple]:
    x1 = int(max(0, min(frame_w - 1, round(face.x))))
    y1 = int(max(0, min(frame_h - 1, round(face.y))))
    x2 = int(max(0, min(frame_w, round(face.x + face.width))))
    y2 = int(max(0, min(frame_h, round(face.y + face.height))))
    if x2 - x1 < 3 or y2 - y1 < 3:
        return None
    return x1, y1, x2, y2


def _roi_metrics(frame_bgr, box):
    cv2 = _cv2_module()
    import numpy as np

    x1, y1, x2, y2 = box
    roi = frame_bgr[y1:y2, x1:x2]
    if roi is None or roi.size == 0:
        return None, None
    if len(roi.shape) == 3:
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    else:
        gray = roi
    mean, _stddev = cv2.meanStdDev(gray)
    brightness = float(mean[0][0]) if mean is not None else float(np.mean(gray))
    laplacian = cv2.Laplacian(gray, cv2.CV_64F)
    sharpness = float(laplacian.var())
    return brightness, sharpness


def _empty_metrics(**extra):
    metrics = {
        "face_count": 0,
        "face_ratio": None,
        "offset_x": None,
        "offset_y": None,
        "brightness": None,
        "sharpness": None,
        "detection_score": None,
        "eye_angle": None,
        "yaw_ratio": None,
        "frame_width": None,
        "frame_height": None,
    }
    metrics.update(extra)
    return metrics


def _tilt_from_horizontal_deg(angle_deg):
    """Smallest signed angle from the image horizontal, in [-90, 90]."""
    folded = (float(angle_deg) + 180.0) % 360.0 - 180.0
    if folded > 90.0:
        folded -= 180.0
    elif folded < -90.0:
        folded += 180.0
    return folded


def pose_from_landmarks(landmarks):
    """Estimate roll/yaw from YuNet 2D landmarks.

    This is a 2D heuristic, not a 3D head-pose solver. Returns None when
    landmarks are missing or degenerate so Haar/contour/tests without
    landmarks skip the check instead of failing closed.
    """
    if not landmarks:
        return None
    right_eye = landmarks.get("right_eye")
    left_eye = landmarks.get("left_eye")
    if (
        not right_eye
        or not left_eye
        or len(right_eye) < 2
        or len(left_eye) < 2
    ):
        return None
    right_x, right_y = float(right_eye[0]), float(right_eye[1])
    left_x, left_y = float(left_eye[0]), float(left_eye[1])
    # User-facing definition: atan2(right_y - left_y, right_x - left_x).
    # On a frontal YuNet face that vector points left (~180°); fold it
    # so only deviation from horizontal remains.
    raw_angle = math.degrees(
        math.atan2(right_y - left_y, right_x - left_x)
    )
    eye_angle = _tilt_from_horizontal_deg(raw_angle)
    eye_dist = math.hypot(left_x - right_x, left_y - right_y)
    if eye_dist < 3.0:
        return None
    yaw_ratio = None
    nose = landmarks.get("nose")
    if nose and len(nose) >= 2:
        mid_x = (left_x + right_x) / 2.0
        yaw_ratio = (float(nose[0]) - mid_x) / eye_dist
    return {
        "eye_angle": round(eye_angle, 3),
        "yaw_ratio": None if yaw_ratio is None else round(float(yaw_ratio), 4),
        "eye_distance": round(eye_dist, 3),
    }


def evaluate_frame(
    frame_bgr,
    *,
    detector: Optional[FaceDetector] = None,
    settings: Optional[FaceQualitySettings] = None,
) -> FaceQualityResult:
    """Run the ordered local checks on an already-decoded BGR frame."""
    settings = settings or settings_from_config()
    active_detector = detector or get_gate().detector
    detector_name = getattr(active_detector, "name", "unknown")

    if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
        return FaceQualityResult(
            False,
            REASON_INVALID_IMAGE,
            _empty_metrics(),
            detector_name,
        )

    try:
        height, width = frame_bgr.shape[:2]
    except Exception:
        return FaceQualityResult(
            False,
            REASON_INVALID_IMAGE,
            _empty_metrics(),
            detector_name,
        )

    metrics = _empty_metrics(frame_width=int(width), frame_height=int(height))
    metrics["trusted"] = bool(getattr(active_detector, "trusted", True))
    if (
        not getattr(active_detector, "trusted", True)
        and not settings.allow_untrusted_detector
    ):
        return FaceQualityResult(
            False, REASON_DETECTOR_UNAVAILABLE, metrics, detector_name
        )
    if width < settings.min_image_width or height < settings.min_image_height:
        return FaceQualityResult(
            False, REASON_INVALID_IMAGE, metrics, detector_name
        )

    try:
        small, scale = _downscale_for_detection(frame_bgr, settings.detect_max_side)
        raw_faces = list(active_detector.detect(small) or [])
        faces = _scale_faces(raw_faces, scale)
    except Exception:
        logger.warning(
            "quality_gate_failed reason=%s detector=%s",
            REASON_QUALITY_CHECK_FAILED,
            detector_name,
        )
        return FaceQualityResult(
            False, REASON_QUALITY_CHECK_FAILED, metrics, detector_name
        )

    if faces:
        metrics["detection_score"] = round(
            max(float(face.score) for face in faces), 4
        )
    faces = [
        face
        for face in faces
        if face.score >= settings.detection_score_threshold
    ]
    metrics["face_count"] = len(faces)
    if not faces:
        return FaceQualityResult(False, REASON_NO_FACE, metrics, detector_name)
    if len(faces) > 1:
        return FaceQualityResult(
            False, REASON_MULTIPLE_FACES, metrics, detector_name
        )

    face = faces[0]
    frame_area = float(width * height)
    face_ratio = (face.area / frame_area) if frame_area else 0.0
    offset_x = abs((face.center_x / float(width)) - 0.5) if width else 1.0
    offset_y = abs((face.center_y / float(height)) - 0.5) if height else 1.0
    metrics["face_ratio"] = round(face_ratio, 6)
    metrics["offset_x"] = round(offset_x, 6)
    metrics["offset_y"] = round(offset_y, 6)
    metrics["detection_score"] = round(float(face.score), 4)

    if face_ratio < settings.min_area_ratio:
        return FaceQualityResult(
            False, REASON_FACE_TOO_SMALL, metrics, detector_name
        )
    if offset_x > settings.center_tolerance or offset_y > settings.center_tolerance:
        return FaceQualityResult(
            False, REASON_FACE_OFF_CENTER, metrics, detector_name
        )

    pose = pose_from_landmarks(face.landmarks)
    if pose:
        metrics["eye_angle"] = pose["eye_angle"]
        metrics["yaw_ratio"] = pose["yaw_ratio"]
        if settings.pose_check_enabled:
            if abs(float(pose["eye_angle"])) > float(settings.max_eye_tilt_degrees):
                return FaceQualityResult(
                    False, REASON_FACE_TILTED, metrics, detector_name
                )
            yaw_ratio = pose.get("yaw_ratio")
            if (
                yaw_ratio is not None
                and abs(float(yaw_ratio)) > float(settings.max_yaw_ratio)
            ):
                return FaceQualityResult(
                    False, REASON_FACE_POSE_INVALID, metrics, detector_name
                )

    box = _clamp_box(face, width, height)
    if box is None:
        return FaceQualityResult(
            False, REASON_FACE_TOO_SMALL, metrics, detector_name
        )

    try:
        brightness, sharpness = _roi_metrics(frame_bgr, box)
    except Exception:
        return FaceQualityResult(
            False, REASON_QUALITY_CHECK_FAILED, metrics, detector_name
        )
    if brightness is None or sharpness is None:
        return FaceQualityResult(
            False, REASON_QUALITY_CHECK_FAILED, metrics, detector_name
        )

    metrics["brightness"] = round(float(brightness), 3)
    metrics["sharpness"] = round(float(sharpness), 3)

    if brightness < settings.min_brightness:
        return FaceQualityResult(False, REASON_TOO_DARK, metrics, detector_name)
    if brightness > settings.max_brightness:
        return FaceQualityResult(False, REASON_TOO_BRIGHT, metrics, detector_name)
    if sharpness < settings.min_sharpness:
        return FaceQualityResult(False, REASON_TOO_BLURRY, metrics, detector_name)

    return FaceQualityResult(True, None, metrics, detector_name)


def evaluate(
    image_bytes,
    *,
    detector: Optional[FaceDetector] = None,
    settings: Optional[FaceQualitySettings] = None,
    request_id: Optional[str] = None,
) -> FaceQualityResult:
    """Evaluate a camera frame from JPEG/PNG bytes. Never logs the bytes."""
    started = time.perf_counter()
    logger.info(
        "event=quality_gate_total request_id=%s",
        request_id or "-",
    )
    try:
        if not image_bytes:
            result = FaceQualityResult(
                False, REASON_INVALID_IMAGE, _empty_metrics(), "none"
            )
        else:
            try:
                _cv2_module()
            except Exception:
                logger.warning(
                    "event=quality_gate_failed reason=%s detector=none",
                    REASON_DETECTOR_UNAVAILABLE,
                )
                result = FaceQualityResult(
                    False,
                    REASON_DETECTOR_UNAVAILABLE,
                    _empty_metrics(trusted=False),
                    "none",
                )
            else:
                frame = decode_bgr_image(image_bytes)
                if frame is None:
                    result = FaceQualityResult(
                        False, REASON_INVALID_IMAGE, _empty_metrics(), "none"
                    )
                else:
                    result = evaluate_frame(
                        frame, detector=detector, settings=settings
                    )
    except Exception:
        logger.warning(
            "quality_gate_failed reason=%s", REASON_QUALITY_CHECK_FAILED
        )
        result = FaceQualityResult(
            False, REASON_QUALITY_CHECK_FAILED, _empty_metrics(), "none"
        )

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    result.metrics["elapsed_ms"] = round(elapsed_ms, 2)
    if request_id:
        result.metrics["request_id"] = request_id
    _log_quality_result(result)
    return result


def _log_quality_result(result: FaceQualityResult):
    metrics = result.metrics or {}
    result_label = "PASS" if result.ok else "FAIL"
    reason = "PASS" if result.ok else (result.reason or REASON_QUALITY_CHECK_FAILED)
    # One line so a real-camera session can be calibrated from logs alone.
    # Never include image bytes, base64, or face crops.
    logger.info(
        "event=quality_gate_debug request_id=%s result=%s reason=%s detector=%s trusted=%s "
        "detection_score=%s face_count=%s face_ratio=%s brightness=%s "
        "sharpness=%s offset_x=%s offset_y=%s eye_angle=%s yaw_ratio=%s "
        "elapsed_ms=%s image_width=%s image_height=%s",
        metrics.get("request_id") or "-",
        result_label,
        reason,
        result.detector_name,
        metrics.get("trusted"),
        metrics.get("detection_score"),
        metrics.get("face_count"),
        metrics.get("face_ratio"),
        metrics.get("brightness"),
        metrics.get("sharpness"),
        metrics.get("offset_x"),
        metrics.get("offset_y"),
        metrics.get("eye_angle"),
        metrics.get("yaw_ratio"),
        metrics.get("elapsed_ms"),
        metrics.get("frame_width"),
        metrics.get("frame_height"),
    )
    if result.ok:
        logger.info(
            "event=quality_gate_pass detector=%s face_count=%s face_ratio=%s "
            "brightness=%s sharpness=%s detection_score=%s elapsed_ms=%s",
            result.detector_name,
            metrics.get("face_count"),
            metrics.get("face_ratio"),
            metrics.get("brightness"),
            metrics.get("sharpness"),
            metrics.get("detection_score"),
            metrics.get("elapsed_ms"),
        )
        return

    fail_event = {
        REASON_NO_FACE: "quality_gate_fail_no_face",
        REASON_MULTIPLE_FACES: "quality_gate_fail_multiple_faces",
        REASON_FACE_TOO_SMALL: "quality_gate_fail_too_small",
        REASON_FACE_OFF_CENTER: "quality_gate_fail_off_center",
        REASON_TOO_DARK: "quality_gate_fail_dark",
        REASON_TOO_BRIGHT: "quality_gate_fail_bright",
        REASON_TOO_BLURRY: "quality_gate_fail_blur",
        REASON_FACE_TILTED: "quality_gate_fail_tilted",
        REASON_FACE_POSE_INVALID: "quality_gate_fail_pose",
        REASON_INVALID_IMAGE: "quality_gate_fail_invalid_image",
        REASON_QUALITY_CHECK_FAILED: "quality_gate_fail_check_failed",
        REASON_DETECTOR_UNAVAILABLE: "quality_gate_fail_detector_unavailable",
    }.get(reason, "quality_gate_fail")
    logger.info(
        "event=%s quality_gate_failed reason=%s detector=%s face_count=%s "
        "face_ratio=%s brightness=%s sharpness=%s detection_score=%s elapsed_ms=%s",
        fail_event,
        reason,
        result.detector_name,
        metrics.get("face_count"),
        metrics.get("face_ratio"),
        metrics.get("brightness"),
        metrics.get("sharpness"),
        metrics.get("detection_score"),
        metrics.get("elapsed_ms"),
    )


class FaceQualityGate:
    """Process-wide gate. Detector is created once, not per request."""

    def __init__(
        self,
        settings: Optional[FaceQualitySettings] = None,
        detector: Optional[FaceDetector] = None,
    ):
        self.settings = settings or settings_from_config()
        self.detector = detector or build_detector(self.settings)

    def evaluate(self, image_bytes) -> FaceQualityResult:
        return evaluate(
            image_bytes, detector=self.detector, settings=self.settings
        )


def get_gate() -> FaceQualityGate:
    """Application-startup singleton. Safe to call from request handlers."""
    global _gate
    if _gate is not None:
        return _gate
    with _gate_lock:
        if _gate is None:
            _gate = FaceQualityGate()
        return _gate


def reset_gate_for_tests():
    """Test helper. Do not use in request handlers."""
    global _gate
    with _gate_lock:
        _gate = None
