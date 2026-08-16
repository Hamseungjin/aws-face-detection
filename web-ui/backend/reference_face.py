"""Kiosk image helpers and STORE CompareFaces (bytes vs bytes).

STORE/RETRIEVE persist FaceIds in Rekognition Collections, not JPEGs:

* STORE verify: ID bytes vs FRAME-A bytes (nothing persisted)
* STORE complete: IndexFaces(FRAME-A)
* RETRIEVE: SearchFacesByImage(FRAME-B)

There is no S3 locker-references path.

PRIVACY: never log image bytes, base64, signed URLs, or credentials. Safe fields
only (transaction_id, matched, similarity, reason, reference_present).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
import struct
import uuid

from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

import config

logger = logging.getLogger("webui.reference_face")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

_JPEG_MAGIC = b"\xff\xd8"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_ALLOWED_CONTENT_TYPES = frozenset({"image/jpeg", "image/png"})

# Rekognition API threshold is 0 so the business threshold is applied here.
_REKOGNITION_API_SIMILARITY_THRESHOLD = 0.0
_REKOGNITION_QUALITY_FILTER = "NONE"


class ReferenceFaceError(Exception):
    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def hash_face_image(face_bytes):
    """SHA-256 hex digest of raw image bytes. Never log the bytes themselves."""
    if not isinstance(face_bytes, (bytes, bytearray)):
        raise ReferenceFaceError("INVALID_IMAGE")
    return hashlib.sha256(bytes(face_bytes)).hexdigest()


def _detect_image_format(image_bytes):
    if image_bytes.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    if image_bytes.startswith(_PNG_MAGIC):
        return "image/png"
    return None


def _sniff_magic_name(image_bytes):
    """Name the container for logs. Unsupported types are not accepted."""
    detected = _detect_image_format(image_bytes)
    if detected == "image/jpeg":
        return "JPEG"
    if detected == "image/png":
        return "PNG"
    if len(image_bytes) >= 12 and image_bytes[4:8] == b"ftyp":
        brand = image_bytes[8:12].lower()
        if brand in (b"heic", b"heif", b"mif1", b"msf1"):
            return "HEIC"
        if brand == b"avif":
            return "AVIF"
    if (
        len(image_bytes) >= 12
        and image_bytes.startswith(b"RIFF")
        and image_bytes[8:12] == b"WEBP"
    ):
        return "WEBP"
    if image_bytes.startswith(b"\x00\x00\x00\x0cjP  "):
        return "JPEG2000"
    if not image_bytes:
        return "EMPTY"
    return "UNKNOWN"


def _png_dimensions(image_bytes):
    if len(image_bytes) < 24 or not image_bytes.startswith(_PNG_MAGIC):
        return None, None
    width, height = struct.unpack(">II", image_bytes[16:24])
    return int(width), int(height)


def _jpeg_dimensions(image_bytes):
    if not image_bytes.startswith(_JPEG_MAGIC) or len(image_bytes) < 4:
        return None, None
    index = 2
    length = len(image_bytes)
    while index < length - 8:
        if image_bytes[index] != 0xFF:
            index += 1
            continue
        marker = image_bytes[index + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            height = int.from_bytes(image_bytes[index + 5 : index + 7], "big")
            width = int.from_bytes(image_bytes[index + 7 : index + 9], "big")
            return width, height
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if index + 3 >= length:
            break
        segment = int.from_bytes(image_bytes[index + 2 : index + 4], "big")
        if segment < 2:
            break
        index += 2 + segment
    return None, None


def image_dimensions(image_bytes):
    """Width/height from JPEG SOF or PNG IHDR. No pixel decode, no EXIF transform."""
    if not image_bytes:
        return None, None
    if image_bytes.startswith(_PNG_MAGIC):
        return _png_dimensions(image_bytes)
    if image_bytes.startswith(_JPEG_MAGIC):
        return _jpeg_dimensions(image_bytes)
    return None, None


def filename_extension(filename):
    if not filename or not isinstance(filename, str):
        return None
    if "." not in filename:
        return None
    ext = filename.rsplit(".", 1)[-1].strip().lower()
    return ext or None


def _magic_name(detected):
    if detected == "image/jpeg":
        return "JPEG"
    if detected == "image/png":
        return "PNG"
    return "UNKNOWN"


def describe_image_payload(image_b64, content_type=None, filename=None):
    """Safe metadata for logs. Never includes bytes or base64."""
    info = {
        "declared_content_type": (content_type or "").strip().lower() or None,
        "b64_chars": 0,
        "has_data_url_prefix": False,
        "decode_ok": False,
        "decoded_bytes": None,
        "magic": None,
        "detected_content_type": None,
        "image_width": None,
        "image_height": None,
        "filename_extension": filename_extension(filename),
    }
    if not isinstance(image_b64, str) or not image_b64.strip():
        return info
    raw = image_b64.strip()
    info["b64_chars"] = len(raw)
    if raw.startswith("data:") and "," in raw:
        info["has_data_url_prefix"] = True
        raw = raw.split(",", 1)[1]
        info["b64_chars"] = len(raw)
    try:
        image_bytes = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return info
    info["decode_ok"] = True
    info["decoded_bytes"] = len(image_bytes)
    if not image_bytes:
        info["magic"] = "EMPTY"
        return info
    detected = _detect_image_format(image_bytes)
    info["detected_content_type"] = detected
    info["magic"] = _sniff_magic_name(image_bytes)
    width, height = image_dimensions(image_bytes)
    info["image_width"] = width
    info["image_height"] = height
    return info


def decode_face_image(image_b64, content_type=None, max_bytes=None):
    """Validate and decode a kiosk image. Returns (bytes, detected_content_type).

    The returned bytes must never be logged.
    """
    if not isinstance(image_b64, str) or not image_b64.strip():
        raise ReferenceFaceError("INVALID_IMAGE")

    declared = (content_type or "").strip().lower()
    if declared and declared not in _ALLOWED_CONTENT_TYPES:
        raise ReferenceFaceError("UNSUPPORTED_IMAGE_FORMAT")

    raw = image_b64.strip()
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]

    try:
        image_bytes = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise ReferenceFaceError("INVALID_IMAGE")

    if not image_bytes:
        raise ReferenceFaceError("INVALID_IMAGE")

    limit = int(max_bytes if max_bytes is not None else config.KIOSK_MAX_ID_IMAGE_BYTES)
    if len(image_bytes) > limit:
        raise ReferenceFaceError("IMAGE_TOO_LARGE")

    detected = _detect_image_format(image_bytes)
    if detected is None:
        raise ReferenceFaceError("UNSUPPORTED_IMAGE_FORMAT")
    if declared and declared != detected:
        # Trust magic when both types are allowed. Browsers often send
        # image/jpeg for a PNG screenshot whose filename ends in .jpg.
        logger.info(
            "event=image_content_type_mismatch declared=%s detected=%s used=%s",
            declared,
            detected,
            detected,
        )
    return image_bytes, detected


def decode_image_base64(image_b64, content_type=None, max_bytes=None):
    """Alias used by callers that only need the decoded bytes."""
    image_bytes, _detected = decode_face_image(
        image_b64, content_type=content_type, max_bytes=max_bytes
    )
    return image_bytes


def _compare_result(success, matched, similarity, reason, threshold=None):
    return {
        "success": bool(success),
        "matched": bool(matched),
        "similarity": similarity,
        "threshold": float(
            config.FACE_SIMILARITY_THRESHOLD if threshold is None else threshold
        ),
        "reason": reason,
    }


def _rekognition_error(err, source_mode="bytes"):
    code = (err.response or {}).get("Error", {}).get("Code", "") or ""
    if code == "InvalidParameterException":
        return ReferenceFaceError("NO_FACE_IN_SOURCE_OR_TARGET")
    if code == "InvalidS3ObjectException":
        return ReferenceFaceError("INVALID_S3_OBJECT")
    if code == "ImageTooLargeException":
        return ReferenceFaceError("IMAGE_TOO_LARGE")
    if code in ("InvalidImageFormatException", "InvalidImageException"):
        return ReferenceFaceError("INVALID_IMAGE")
    if code in ("AccessDenied", "AccessDeniedException"):
        return ReferenceFaceError("ACCESS_DENIED")
    if code in (
        "ProvisionedThroughputExceededException",
        "ThrottlingException",
        "ThrottledException",
        "Throttling",
        "RequestLimitExceeded",
    ):
        return ReferenceFaceError("THROTTLED")
    return ReferenceFaceError("AWS_API_ERROR", detail=code)


def _compare_faces(rekognition_client, source_image, target_image, source_mode="bytes"):
    try:
        response = rekognition_client.compare_faces(
            SourceImage=source_image,
            TargetImage=target_image,
            SimilarityThreshold=_REKOGNITION_API_SIMILARITY_THRESHOLD,
            QualityFilter=_REKOGNITION_QUALITY_FILTER,
        )
    except NoCredentialsError:
        raise ReferenceFaceError("ACCESS_DENIED")
    except ClientError as err:
        raise _rekognition_error(err, source_mode=source_mode)
    except BotoCoreError:
        raise ReferenceFaceError("AWS_API_ERROR")

    face_matches = response.get("FaceMatches") or []
    unmatched_faces = response.get("UnmatchedFaces") or []
    if not face_matches:
        if not unmatched_faces:
            raise ReferenceFaceError("NO_FACE_IN_SOURCE_OR_TARGET")
        return 0.0
    return max(float(match["Similarity"]) for match in face_matches)


def _threshold_outcome(similarity, threshold=None):
    cutoff = float(
        config.FACE_SIMILARITY_THRESHOLD if threshold is None else threshold
    )
    matched = float(similarity) >= cutoff
    return _compare_result(
        True,
        matched,
        round(float(similarity), 4),
        "SIMILARITY_ABOVE_THRESHOLD" if matched else "SIMILARITY_BELOW_THRESHOLD",
        threshold=cutoff,
    )


def compare_id_to_face_bytes(
    rekognition_client, id_bytes, face_bytes, threshold=None
):
    """STORE: ID image bytes vs live FRAME-A bytes. Nothing is persisted."""
    if not id_bytes or not face_bytes:
        raise ReferenceFaceError("INVALID_IMAGE")
    similarity = _compare_faces(
        rekognition_client,
        {"Bytes": bytes(id_bytes)},
        {"Bytes": bytes(face_bytes)},
        source_mode="bytes",
    )
    return _threshold_outcome(similarity, threshold=threshold)


def validate_frame_id(frame_id):
    """Legacy helper retained for operator/frame-id tooling."""
    if not isinstance(frame_id, str) or not _UUID_RE.match(frame_id):
        raise ReferenceFaceError("INVALID_TARGET_FRAME_ID")
    return frame_id


def is_uuid4(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return parsed.version == 4 and str(parsed) == value
