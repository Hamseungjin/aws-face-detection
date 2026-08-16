"""Rekognition Collection helpers for kiosk FaceId references.

New STORE transactions index a verified FRAME-A into a Collection and persist
only the returned FaceId. New RETRIEVE transactions search that Collection and
accept a match only when the transaction's expected FaceId is present at the
business similarity threshold.

PRIVACY: never log image bytes, base64, or full FaceIds unless a short mask is
required for cleanup. CompareFaces remains in reference_face.py for STORE ID
checks and legacy S3 retrieve.
"""
from __future__ import annotations

import logging

from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

import config
from reference_face import ReferenceFaceError

logger = logging.getLogger("webui.face_collection")

_REKOGNITION_API_SIMILARITY_THRESHOLD = 0.0
_REKOGNITION_QUALITY_FILTER = "NONE"


def mask_face_id(face_id):
    if not isinstance(face_id, str) or not face_id:
        return "-"
    if len(face_id) <= 8:
        return "********"
    return face_id[:8] + "…"


def collection_id():
    return (config.REKOGNITION_FACE_COLLECTION_ID or "").strip()


def require_collection_id():
    value = collection_id()
    if not value:
        raise ReferenceFaceError("FACE_COLLECTION_NOT_CONFIGURED")
    return value


def _rekognition_collection_error(err):
    code = (err.response or {}).get("Error", {}).get("Code", "") or ""
    if code == "ResourceNotFoundException":
        return ReferenceFaceError("FACE_COLLECTION_NOT_CONFIGURED")
    if code == "InvalidParameterException":
        return ReferenceFaceError("NO_FACE_IN_SOURCE_OR_TARGET")
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


def index_verified_face(rekognition_client, image_bytes, external_image_id):
    """Index one verified FRAME-A. Returns FaceId. Does not store JPEG bytes."""
    if not image_bytes:
        raise ReferenceFaceError("INVALID_IMAGE")
    if not external_image_id:
        raise ReferenceFaceError("FACE_INDEX_FAILED")
    target_collection = require_collection_id()
    try:
        response = rekognition_client.index_faces(
            CollectionId=target_collection,
            Image={"Bytes": bytes(image_bytes)},
            ExternalImageId=str(external_image_id)[:255],
            MaxFaces=1,
            QualityFilter=_REKOGNITION_QUALITY_FILTER,
            DetectionAttributes=[],
        )
    except NoCredentialsError:
        raise ReferenceFaceError("ACCESS_DENIED")
    except ClientError as err:
        raise _rekognition_collection_error(err)
    except BotoCoreError:
        raise ReferenceFaceError("FACE_INDEX_FAILED")

    records = response.get("FaceRecords") or []
    if len(records) != 1:
        logger.warning(
            "face_index_failed reason=FACE_INDEX_FAILED record_count=%s",
            len(records),
        )
        raise ReferenceFaceError("FACE_INDEX_FAILED")
    face = (records[0] or {}).get("Face") or {}
    face_id = face.get("FaceId")
    if not face_id:
        raise ReferenceFaceError("FACE_ID_MISSING")
    logger.info(
        "event=face_index_ok collection_configured=true face_id=%s",
        mask_face_id(face_id),
    )
    return face_id


def search_faces_by_image(rekognition_client, image_bytes, max_faces=None):
    """Return [{face_id, similarity}, ...] for one FRAME-B. One API call."""
    if not image_bytes:
        raise ReferenceFaceError("INVALID_IMAGE")
    target_collection = require_collection_id()
    limit = int(config.REKOGNITION_SEARCH_MAX_FACES if max_faces is None else max_faces)
    if limit < 1:
        limit = 1
    try:
        response = rekognition_client.search_faces_by_image(
            CollectionId=target_collection,
            Image={"Bytes": bytes(image_bytes)},
            MaxFaces=limit,
            FaceMatchThreshold=_REKOGNITION_API_SIMILARITY_THRESHOLD,
            QualityFilter=_REKOGNITION_QUALITY_FILTER,
        )
    except NoCredentialsError:
        raise ReferenceFaceError("ACCESS_DENIED")
    except ClientError as err:
        mapped = _rekognition_collection_error(err)
        if mapped.reason == "NO_FACE_IN_SOURCE_OR_TARGET":
            raise ReferenceFaceError("NO_FACE_IN_TARGET")
        if mapped.reason == "AWS_API_ERROR":
            raise ReferenceFaceError("FACE_SEARCH_FAILED", detail=mapped.detail)
        raise mapped
    except BotoCoreError:
        raise ReferenceFaceError("FACE_SEARCH_FAILED")

    matches = []
    for item in response.get("FaceMatches") or []:
        face = item.get("Face") or {}
        face_id = face.get("FaceId")
        if not face_id:
            continue
        try:
            similarity = float(item.get("Similarity"))
        except (TypeError, ValueError):
            continue
        matches.append({"face_id": face_id, "similarity": similarity})
    return matches


def match_expected_face(matches, expected_face_id, threshold=None):
    """Find expected FaceId among candidates. Do not assume matches[0]."""
    if not expected_face_id:
        return None
    cutoff = float(
        config.FACE_SIMILARITY_THRESHOLD if threshold is None else threshold
    )
    found = None
    for item in matches or []:
        if item.get("face_id") != expected_face_id:
            continue
        found = item
        break
    if found is None:
        return None
    if float(found.get("similarity") or 0.0) < cutoff:
        return {
            "face_id": expected_face_id,
            "similarity": round(float(found["similarity"]), 4),
            "matched": False,
            "reason": "SIMILARITY_BELOW_THRESHOLD",
        }
    return {
        "face_id": expected_face_id,
        "similarity": round(float(found["similarity"]), 4),
        "matched": True,
        "reason": "SIMILARITY_ABOVE_THRESHOLD",
    }


def delete_indexed_faces(rekognition_client, face_ids, target_collection=None):
    """Best-effort DeleteFaces. Missing faces are treated as success."""
    ids = [face_id for face_id in (face_ids or []) if face_id]
    if not ids:
        return False
    collection = (target_collection or collection_id() or "").strip()
    if not collection:
        raise ReferenceFaceError("FACE_COLLECTION_NOT_CONFIGURED")
    try:
        rekognition_client.delete_faces(CollectionId=collection, FaceIds=ids)
    except NoCredentialsError:
        raise ReferenceFaceError("ACCESS_DENIED")
    except ClientError as err:
        code = (err.response or {}).get("Error", {}).get("Code", "") or ""
        if code == "ResourceNotFoundException":
            return False
        raise _rekognition_collection_error(err)
    except BotoCoreError:
        raise ReferenceFaceError("FACE_DELETE_FAILED")
    logger.info(
        "event=face_delete_ok count=%s face_id=%s",
        len(ids),
        mask_face_id(ids[0]),
    )
    return True
