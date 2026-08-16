"""Web UI backend configuration (12-factor: everything via environment).

Mirrors kpi-dashboard/backend/config.py: values come from os.environ, which on EC2
is populated from /etc/webui.env (written by webui-bootstrap.sh from SSM Parameter
Store), and locally from web-ui/backend/.env (loaded below). NO secrets are
hardcoded here.
"""
import os
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent
# Local dev convenience: always load web-ui/backend/.env by absolute path when present.
# Do NOT rely on process CWD — uvicorn may be started from the repo root or elsewhere.
#   override=False  -> the EC2 systemd EnvironmentFile (/etc/webui.env) ALWAYS wins
#                      (and publishapps excludes .env, so EC2 has none -> no-op there).
#   interpolate=False -> a '$' inside WEBUI_AUTH_PASSWORD_HASH (pbkdf2_sha256$...$...$...)
#                      is never mangled by dotenv variable expansion.
try:
    from dotenv import load_dotenv
    load_dotenv(_BACKEND_DIR / ".env", override=False, interpolate=False)
except Exception:
    pass


def _bool(name, default="false"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# AWS ----------------------------------------------------------------------
REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-northeast-2"
WEBUI_PORT = int(os.getenv("WEBUI_PORT", "8080"))

# Auth (injected, never hardcoded) -----------------------------------------
#   AUTH_USERNAME       : plain username
#   AUTH_PASSWORD_HASH  : PBKDF2 string produced by auth.hash_password (NOT the password)
#   SESSION_SECRET      : random secret used to sign the session cookie
AUTH_USERNAME = os.getenv("WEBUI_AUTH_USERNAME", "")
AUTH_PASSWORD_HASH = os.getenv("WEBUI_AUTH_PASSWORD_HASH", "")
SESSION_SECRET = os.getenv("WEBUI_SESSION_SECRET", "")

# Session cookie -----------------------------------------------------------
SESSION_COOKIE = os.getenv("WEBUI_SESSION_COOKIE", "webui_session")
SESSION_MAX_AGE = int(os.getenv("WEBUI_SESSION_MAX_AGE", str(8 * 3600)))  # 8h
# Secure cookie flag (SessionMiddleware https_only).
# - Local plain HTTP (http://localhost:8080): MUST be false, or browsers will refuse
#   to store/send the session cookie → login 200 then protected routes 401.
# - Deployed HTTPS (EC2 webui-bootstrap writes WEBUI_SESSION_HTTPS_ONLY=true): true.
# Default false so a missing env var does not break local HTTP development.
SESSION_HTTPS_ONLY = _bool("WEBUI_SESSION_HTTPS_ONLY", "false")
# SameSite policy for the session cookie (lax|strict|none). "none" requires Secure
# (HTTPS) and is only needed for cross-site embeds; keep "lax" for same-origin SPA.
SESSION_SAME_SITE = (os.getenv("WEBUI_SESSION_SAME_SITE", "lax") or "lax").strip().lower()
if SESSION_SAME_SITE not in ("lax", "strict", "none"):
    SESSION_SAME_SITE = "lax"

# Single-host kiosk transaction storage. The default is intentionally local to
# this application; deployments can point KIOSK_DB_PATH at a persistent volume.
KIOSK_DB_PATH = os.getenv(
    "KIOSK_DB_PATH", str(_BACKEND_DIR / "data" / "kiosk.sqlite3")
)
# Locker hold TTL. STORE now reserves the locker before ID/face/payment.
# LOCKER_HOLD_TTL_SECONDS is the preferred name; KIOSK_RESERVATION_SECONDS is
# kept as a backward-compatible alias. Default 180s covers ID + capture + pay.
LOCKER_HOLD_TTL_SECONDS = int(
    os.getenv("LOCKER_HOLD_TTL_SECONDS")
    or os.getenv("KIOSK_RESERVATION_SECONDS", "180")
)
KIOSK_RESERVATION_SECONDS = LOCKER_HOLD_TTL_SECONDS
KIOSK_RETRIEVAL_SECONDS = int(os.getenv("KIOSK_RETRIEVAL_SECONDS", "120"))
KIOSK_RETRIEVAL_MAX_FAILURES = int(
    os.getenv("KIOSK_RETRIEVAL_MAX_FAILURES", "5")
)
KIOSK_RETRIEVAL_RATE_WINDOW_SECONDS = int(
    os.getenv("KIOSK_RETRIEVAL_RATE_WINDOW_SECONDS", "60")
)

FACE_SIMILARITY_THRESHOLD = float(os.getenv("FACE_SIMILARITY_THRESHOLD", "90"))
# Rekognition Collection for kiosk FaceId references. Empty disables
# STORE complete (no S3 fallback).
REKOGNITION_FACE_COLLECTION_ID = os.getenv(
    "REKOGNITION_FACE_COLLECTION_ID", ""
).strip()
# How many SearchFacesByImage candidates to inspect for the expected FaceId.
# Raise if the same person may have several active FaceIds. One API call.
REKOGNITION_SEARCH_MAX_FACES = int(os.getenv("REKOGNITION_SEARCH_MAX_FACES", "5"))
# How long a STORE face verification remains valid for store/complete.
KIOSK_STORE_VERIFICATION_SECONDS = int(
    os.getenv(
        "KIOSK_STORE_VERIFICATION_SECONDS",
        str(KIOSK_RESERVATION_SECONDS),
    )
)
# Max source image size accepted by store face-verify (bytes).
KIOSK_MAX_ID_IMAGE_BYTES = int(
    os.getenv("KIOSK_MAX_ID_IMAGE_BYTES", str(5 * 1024 * 1024))
)

# Local OpenCV face quality gate (STORE/RETRIEVE camera frames only) ---------
# Strict Kiosk Profile 1st calibration defaults for a kiosk camera. They are
# not claimed industry standards. Calibrate on the real device and lighting.
# See opencv_strict_quality_gate.md. All values are env-overridable.
#
# FACE_DETECTION_SCORE_THRESHOLD  unit: detector confidence 0-1
#   meaning: minimum YuNet/Haar score kept as a face
#   raise: fewer detections, more NO_FACE
#   lower: more detections, more false faces
FACE_DETECTION_SCORE_THRESHOLD = float(
    os.getenv("FACE_DETECTION_SCORE_THRESHOLD", "0.75")
)
# FACE_MIN_AREA_RATIO  unit: face_bbox_area / frame_area (0-1)
#   meaning: reject faces that occupy too little of the frame
#   raise: user must stand closer
#   lower: allow smaller / farther faces
FACE_MIN_AREA_RATIO = float(os.getenv("FACE_MIN_AREA_RATIO", "0.08"))
# FACE_CENTER_TOLERANCE  unit: |face_center - 0.5| in normalized frame coords
#   meaning: how far from the optical center a face may sit
#   raise: more off-center faces pass (looser UX)
#   lower: face must be nearer the guide oval (stricter)
FACE_CENTER_TOLERANCE = float(os.getenv("FACE_CENTER_TOLERANCE", "0.20"))
# FACE_MIN_BRIGHTNESS / FACE_MAX_BRIGHTNESS  unit: mean grayscale 0-255 of face ROI
#   meaning: reject underexposed or overexposed face crops (not the whole frame)
#   raise min / lower max: stricter lighting
#   lower min / raise max: more lighting conditions pass
FACE_MIN_BRIGHTNESS = float(os.getenv("FACE_MIN_BRIGHTNESS", "55"))
FACE_MAX_BRIGHTNESS = float(os.getenv("FACE_MAX_BRIGHTNESS", "205"))
# FACE_MIN_SHARPNESS  unit: variance of Laplacian (cv2.CV_64F) on face ROI
#   meaning: reject motion-blurred or out-of-focus face crops
#   raise: require a sharper still
#   lower: allow more blur
#   This number is not a universal blur standard; it depends on ROI size/light.
FACE_MIN_SHARPNESS = float(os.getenv("FACE_MIN_SHARPNESS", "30"))
# YuNet 2D landmark pose heuristics. Missing landmarks skip the check.
# These are not 3D head pose and are not Face Liveness.
FACE_POSE_CHECK_ENABLED = _bool("FACE_POSE_CHECK_ENABLED", "true")
# FACE_MAX_EYE_TILT_DEGREES  unit: degrees from image horizontal
#   raise: allow more roll. lower: require a more upright face
FACE_MAX_EYE_TILT_DEGREES = float(os.getenv("FACE_MAX_EYE_TILT_DEGREES", "20"))
# FACE_MAX_YAW_RATIO  unit: |nose_x - eye_mid_x| / inter-ocular distance
#   raise: allow more left/right turn. lower: require facing the camera
FACE_MAX_YAW_RATIO = float(os.getenv("FACE_MAX_YAW_RATIO", "0.38"))
# Minimum decoded frame size before detection. Smaller images are INVALID_IMAGE.
FACE_MIN_IMAGE_WIDTH = int(os.getenv("FACE_MIN_IMAGE_WIDTH", "80"))
FACE_MIN_IMAGE_HEIGHT = int(os.getenv("FACE_MIN_IMAGE_HEIGHT", "80"))
# Longest side used for detection. Boxes are mapped back to the original frame.
FACE_DETECT_MAX_SIDE = int(os.getenv("FACE_DETECT_MAX_SIDE", "320"))
# Optional YuNet ONNX. Empty → search web-ui/backend/models/, then Haar, then
# the file-free contour fallback. Do not commit the model binary.
FACE_DETECTOR_MODEL_PATH = os.getenv("FACE_DETECTOR_MODEL_PATH", "").strip()
# When false (default), contour/skin fallback is not trusted for production
# kiosk auth. Missing YuNet/Haar → QUALITY_DETECTOR_UNAVAILABLE, AWS = 0.
# Set true only for local demo without the YuNet ONNX.
FACE_QUALITY_ALLOW_UNTRUSTED_DETECTOR = _bool(
    "FACE_QUALITY_ALLOW_UNTRUSTED_DETECTOR", "false"
)
