"""Kiosk FastAPI: login + STORE/RETRIEVE. Rekognition Collection only.

Browser cameras stay in the page. FastAPI calls Rekognition CompareFaces /
IndexFaces / SearchFacesByImage / DeleteFaces. There is no S3 face-reference path.
"""
import datetime
import logging
import secrets
import time
from pathlib import Path
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, StrictStr
from starlette.middleware.sessions import SessionMiddleware

import auth
import config
import face_quality
import reference_face
from session_cookie_compat import CodeServerSessionCookieCompatMiddleware
import face_collection
from kiosk_store import (
    InvalidKioskInput,
    KioskStore,
    LockerNotAvailable,
    RetrievalAttemptLimiter,
    RetrievalUnavailable,
    StoreTransactionUnavailable,
    validate_retrieval_code,
    validate_transaction_id,
)

logger = logging.getLogger("webui")


def _configure_webui_logging():
    """Keep webui.* INFO logs visible under uvicorn.

    Child loggers (webui.face_quality, webui.reference_face) start at NOTSET.
    If the root logger stays at WARNING, those INFO records are dropped before
    they reach a handler. Force the webui tree to INFO. Add a stderr handler
    only when neither this logger nor root already has one (uvicorn CLI).
    """
    webui = logging.getLogger("webui")
    webui.setLevel(logging.INFO)
    root = logging.getLogger()
    if not webui.handlers and not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        webui.addHandler(handler)
        webui.propagate = False
    else:
        webui.propagate = True


_configure_webui_logging()

app = FastAPI(title="Kiosk Face Locker")

# Signed, HttpOnly session cookie.
# - same_site from WEBUI_SESSION_SAME_SITE (default lax) for same-origin SPA
# - https_only / Secure from WEBUI_SESSION_HTTPS_ONLY (false for local HTTP;
#   true for deployed HTTPS). Secure cookies are NOT stored by browsers on
#   plain http://localhost → login appears to succeed then protected APIs 401.
#
# Middleware execution order (Starlette add_middleware: last-added is outermost):
#   request → CodeServerSessionCookieCompatMiddleware → SessionMiddleware → routes
# Compat must run BEFORE SessionMiddleware parses the Cookie header so that
# code-server /proxy encodeURIComponent-style mutation of the session cookie
# value (e.g. '=' → '%3D') is reversed for the configured session cookie only.
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET or "dev-insecure-secret-change-me",
    session_cookie=config.SESSION_COOKIE,
    max_age=config.SESSION_MAX_AGE,
    same_site=config.SESSION_SAME_SITE,
    https_only=config.SESSION_HTTPS_ONLY,
)
app.add_middleware(
    CodeServerSessionCookieCompatMiddleware,
    session_cookie_name=config.SESSION_COOKIE,
)

# Non-secret startup diagnostics (never log SESSION_SECRET or cookie values).
logger.info(
    "session_cookie_config name=%s max_age=%s same_site=%s https_only=%s "
    "secret_configured=%s auth_username_configured=%s",
    config.SESSION_COOKIE,
    config.SESSION_MAX_AGE,
    config.SESSION_SAME_SITE,
    config.SESSION_HTTPS_ONLY,
    bool(config.SESSION_SECRET),
    bool(config.AUTH_USERNAME),
)
logger.info(
    "event=face_collection_config collection_configured=%s",
    bool(face_collection.collection_id()),
)
if not face_collection.collection_id():
    logger.warning(
        "event=face_collection_config result=MISSING "
        "hint=set_REKOGNITION_FACE_COLLECTION_ID_or_store_complete_returns_502"
    )
if config.SESSION_HTTPS_ONLY:
    logger.warning(
        "WEBUI_SESSION_HTTPS_ONLY=true: session cookie will have the Secure flag. "
        "Browsers will not send it on plain HTTP (http://localhost / http://127.0.0.1). "
        "For local HTTP development set WEBUI_SESSION_HTTPS_ONLY=false and restart."
    )
if config.SESSION_SAME_SITE == "none" and not config.SESSION_HTTPS_ONLY:
    logger.warning(
        "WEBUI_SESSION_SAME_SITE=none requires Secure cookies; "
        "set WEBUI_SESSION_HTTPS_ONLY=true when serving over HTTPS."
    )

ROOT = Path(__file__).resolve().parent.parent  # web-ui/ -- the static SPA lives here

# One database file is shared by every browser connected to this FastAPI process.
# KioskStore opens short-lived sqlite3 connections for thread-safe request handling.
_kiosk_store = KioskStore(
    config.KIOSK_DB_PATH,
    reservation_seconds=config.KIOSK_RESERVATION_SECONDS,
    retrieval_seconds=config.KIOSK_RETRIEVAL_SECONDS,
)
_retrieval_limiter = RetrievalAttemptLimiter(
    max_failures=config.KIOSK_RETRIEVAL_MAX_FAILURES,
    window_seconds=config.KIOSK_RETRIEVAL_RATE_WINDOW_SECONDS,
)

_REKOGNITION_CONFIG = BotoConfig(
    connect_timeout=3, read_timeout=15,
    retries={"max_attempts": 3, "mode": "standard"},
)
_rekognition = boto3.client(
    "rekognition", region_name=config.REGION, config=_REKOGNITION_CONFIG
)

# Session keys for trusted STORE face verification (no image bytes stored).
_SESSION_VERIFIED_FACE_HASH = "verified_store_face_hash"
_SESSION_VERIFIED_AT = "verified_store_at"
_SESSION_VERIFIED_SIMILARITY = "verified_store_similarity"
_SESSION_VERIFIED_FRAME_ID = "verified_store_frame_id"  # legacy; no longer written
_SESSION_HOLD_TX = "store_hold_transaction_id"
_SESSION_HOLD_LOCKER = "store_hold_locker_id"


# --- auth helpers ----------------------------------------------------------
def _current_user(request):
    return request.session.get("user")


def require_user(request):
    if not _current_user(request):
        raise HTTPException(status_code=401, detail="authentication required")


def _validated_transaction_id(transaction_id):
    try:
        return validate_transaction_id(transaction_id)
    except InvalidKioskInput as error:
        raise HTTPException(status_code=400, detail=error.code)


def _validated_retrieval_code(retrieval_code):
    try:
        return validate_retrieval_code(retrieval_code)
    except InvalidKioskInput as error:
        raise HTTPException(status_code=400, detail=error.code)


def _retrieval_rate_scope(request):
    scope = request.session.get("kiosk_rate_scope")
    if not scope:
        scope = secrets.token_urlsafe(18)
        request.session["kiosk_rate_scope"] = scope
    return "%s:%s" % (_current_user(request), scope)


def _run_retrieval_code_lookup(request, operation):
    scope = _retrieval_rate_scope(request)
    retry_after = _retrieval_limiter.retry_after(scope)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail="RETRIEVAL_RATE_LIMITED",
            headers={"Retry-After": str(retry_after)},
        )
    try:
        result = operation()
    except RetrievalUnavailable:
        _retrieval_limiter.record_failure(scope)
        raise HTTPException(status_code=409, detail="RETRIEVAL_UNAVAILABLE")
    _retrieval_limiter.record_success(scope)
    return result


# --- request models --------------------------------------------------------
class LoginIn(BaseModel):
    username: str
    password: str


class LockerReserveIn(BaseModel):
    lockerId: str


class TransactionIn(BaseModel):
    transactionId: StrictStr


class StoreCompleteIn(BaseModel):
    transactionId: StrictStr
    verifiedFaceImageBase64: Optional[StrictStr] = None


class RetrievalStartIn(BaseModel):
    retrievalCode: StrictStr
    faceImageBase64: Optional[StrictStr] = None


class RetrievalLookupIn(BaseModel):
    retrievalCode: StrictStr


class StoreFaceVerifyIn(BaseModel):
    imageBase64: StrictStr
    contentType: StrictStr
    filename: str = "id.jpg"
    faceImageBase64: StrictStr
    transactionId: Optional[StrictStr] = None


def _safe_face_result(
    *,
    success,
    matched,
    similarity,
    threshold,
    reason,
    transaction_id=None,
    locker_id=None,
    additional_fee=None,
    status=None,
    source=None,
):
    """Browser-safe face/retrieval result. Never includes S3 keys or images."""
    result = {
        "success": bool(success),
        "matched": bool(matched),
        "similarity": similarity,
        "threshold": threshold,
        "reason": reason,
    }
    if transaction_id is not None:
        result["transactionId"] = transaction_id
    if locker_id is not None:
        result["lockerId"] = locker_id
    if additional_fee is not None:
        result["additionalFee"] = additional_fee
    if status is not None:
        result["status"] = status
    if source is not None:
        result["source"] = source
    return result


def _clear_store_verification(request):
    request.session.pop(_SESSION_VERIFIED_FACE_HASH, None)
    request.session.pop(_SESSION_VERIFIED_AT, None)
    request.session.pop(_SESSION_VERIFIED_SIMILARITY, None)
    request.session.pop(_SESSION_VERIFIED_FRAME_ID, None)


def _bind_store_hold(request, transaction_id, locker_id):
    request.session[_SESSION_HOLD_TX] = transaction_id
    request.session[_SESSION_HOLD_LOCKER] = locker_id


def _clear_store_hold(request):
    request.session.pop(_SESSION_HOLD_TX, None)
    request.session.pop(_SESSION_HOLD_LOCKER, None)


def _active_store_hold(request, transaction_id=None):
    """Return (transaction_id, locker_id) or (None, reason). No AWS."""
    session_tx = request.session.get(_SESSION_HOLD_TX)
    if not isinstance(session_tx, str) or not session_tx:
        return None, "LOCKER_HOLD_NOT_OWNED"
    if transaction_id and transaction_id != session_tx:
        return None, "LOCKER_HOLD_NOT_OWNED"
    try:
        txn = _kiosk_store.get_transaction(session_tx)
    except InvalidKioskInput:
        _clear_store_hold(request)
        return None, "LOCKER_HOLD_NOT_OWNED"
    if not txn or txn.get("status") != "RESERVED":
        _clear_store_hold(request)
        return None, "LOCKER_HOLD_EXPIRED"
    locker_id = txn.get("locker_id")
    session_locker = request.session.get(_SESSION_HOLD_LOCKER)
    if session_locker and locker_id != session_locker:
        _clear_store_hold(request)
        return None, "LOCKER_HOLD_NOT_OWNED"
    return session_tx, locker_id


def _set_store_verification(request, face_hash, similarity):
    request.session[_SESSION_VERIFIED_FACE_HASH] = face_hash
    request.session[_SESSION_VERIFIED_AT] = datetime.datetime.now(
        datetime.timezone.utc
    ).isoformat(timespec="seconds")
    if similarity is not None:
        request.session[_SESSION_VERIFIED_SIMILARITY] = float(similarity)


def _parse_verified_at(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _store_verification_state(request):
    """Return ('ok', hash), ('expired', hash), or ('missing', None)."""
    face_hash = request.session.get(_SESSION_VERIFIED_FACE_HASH)
    if not isinstance(face_hash, str) or len(face_hash) != 64:
        return "missing", None
    verified_at = _parse_verified_at(request.session.get(_SESSION_VERIFIED_AT))
    if verified_at is None:
        return "missing", None
    age = (
        datetime.datetime.now(datetime.timezone.utc) - verified_at
    ).total_seconds()
    if age < 0 or age > float(config.KIOSK_STORE_VERIFICATION_SECONDS):
        return "expired", face_hash
    return "ok", face_hash


# --- auth routes -----------------------------------------------------------
@app.post("/api/login")
def login(body: LoginIn, request: Request):
    ok = (
        bool(config.AUTH_USERNAME)
        and bool(config.AUTH_PASSWORD_HASH)
        and body.username == config.AUTH_USERNAME
        and auth.verify_password(body.password, config.AUTH_PASSWORD_HASH)
    )
    if not ok:
        raise HTTPException(status_code=401, detail="invalid credentials")
    request.session["user"] = body.username
    request.session["kiosk_rate_scope"] = secrets.token_urlsafe(18)
    return {"ok": True, "user": body.username}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    user = _current_user(request)
    return {"authenticated": bool(user), "user": user}


# --- kiosk locker transactions (auth required) -----------------------------
@app.get("/api/kiosk/lockers")
def kiosk_lockers(request: Request):
    require_user(request)
    return {"lockers": _kiosk_store.list_lockers()}


@app.post("/api/kiosk/store/reserve")
def kiosk_store_reserve(body: LockerReserveIn, request: Request):
    require_user(request)
    existing = request.session.get(_SESSION_HOLD_TX)
    if existing:
        try:
            _kiosk_store.cancel_store(existing)
        except Exception:
            logger.warning("previous_hold_cancel_failed")
        _clear_store_hold(request)
        _clear_store_verification(request)
    try:
        result = _kiosk_store.reserve_locker(body.lockerId)
    except LockerNotAvailable as error:
        raise HTTPException(status_code=409, detail=error.code)
    _bind_store_hold(request, result["transactionId"], result["lockerId"])
    return result


@app.post("/api/kiosk/store/cancel")
def kiosk_store_cancel(body: TransactionIn, request: Request):
    require_user(request)
    transaction_id = _validated_transaction_id(body.transactionId)
    cancelled = _kiosk_store.cancel_store(transaction_id)
    if request.session.get(_SESSION_HOLD_TX) == transaction_id:
        _clear_store_hold(request)
        _clear_store_verification(request)
    return {
        "transactionId": transaction_id,
        "cancelled": cancelled,
    }


def _face_input_error(reason, source="input"):
    return _safe_face_result(
        success=False,
        matched=False,
        similarity=None,
        threshold=config.FACE_SIMILARITY_THRESHOLD,
        reason=reason,
        source=source,
    )


def _evaluate_camera_frame(face_bytes, request_id=None):
    """Local OpenCV gate. Callers must reuse the same face_bytes on PASS."""
    if request_id:
        return face_quality.evaluate(face_bytes, request_id=request_id)
    return face_quality.evaluate(face_bytes)


def _new_store_verify_request_id():
    return "store-face-verify-%s" % secrets.token_hex(4)


def _log_face_verify_stage(request_id, stage, result, **fields):
    """One-line stage log. Never includes image bytes, base64, or hashes."""
    parts = [
        "request_id=%s" % request_id,
        "stage=%s" % stage,
        "result=%s" % result,
    ]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append("%s=%s" % (key, value))
    logger.info("event=face_verify_stage %s", " ".join(parts))


def _compensate_indexed_face(face_id):
    """Best-effort DeleteFaces after IndexFaces succeeded but STORE did not."""
    try:
        face_collection.delete_indexed_faces(_rekognition, [face_id])
    except Exception:
        logger.warning(
            "event=face_delete_compensation_failed face_id=%s",
            face_collection.mask_face_id(face_id),
        )


def _require_stored_face_id(stored):
    """STORED rows must have a live FaceId. No S3 fallback."""
    if (
        stored
        and stored.get("rekognition_face_id")
        and not stored.get("face_deleted_at")
    ):
        return stored["rekognition_face_id"]
    logger.error(
        "event=data_consistency_error reason=STORED_FACE_ID_MISSING "
        "transaction_id=%s aws=0",
        (stored or {}).get("transaction_id"),
    )
    raise HTTPException(status_code=500, detail="DATA_CONSISTENCY_ERROR")


def _retrieve_compare_frame(stored, face_bytes):
    """Collection SearchFacesByImage against the expected FaceId."""
    expected = _require_stored_face_id(stored)
    matches = face_collection.search_faces_by_image(_rekognition, face_bytes)
    outcome = face_collection.match_expected_face(matches, expected)
    threshold = float(config.FACE_SIMILARITY_THRESHOLD)
    if outcome is None:
        return {
            "success": True,
            "matched": False,
            "similarity": None,
            "threshold": threshold,
            "reason": "EXPECTED_FACE_NOT_FOUND",
        }
    return {
        "success": True,
        "matched": bool(outcome.get("matched")),
        "similarity": outcome.get("similarity"),
        "threshold": threshold,
        "reason": outcome.get("reason") or "EXPECTED_FACE_NOT_FOUND",
    }


@app.post("/api/kiosk/store/face-verify")
def kiosk_store_face_verify(body: StoreFaceVerifyIn, request: Request):
    """Server-side ID bytes vs FRAME-A bytes for STORE.

    On match, binds verified_store_face_hash into the signed session so
    store/complete can persist that exact FRAME-A as the transaction
    reference. ID and face bytes are never persisted at this step.
    """
    require_user(request)
    request_id = _new_store_verify_request_id()
    requested_tx = None
    if body.transactionId:
        requested_tx = _validated_transaction_id(body.transactionId)
    hold_tx, hold_detail = _active_store_hold(request, requested_tx)
    if not hold_tx:
        _log_face_verify_stage(
            request_id, "hold", "FAIL", reason=hold_detail, source="hold"
        )
        _log_face_verify_stage(request_id, "id_decode", "NOT_REACHED")
        _log_face_verify_stage(request_id, "frame_a_decode", "NOT_REACHED")
        _log_face_verify_stage(request_id, "quality_gate", "NOT_REACHED")
        _log_face_verify_stage(request_id, "rekognition", "NOT_CALLED")
        _clear_store_verification(request)
        return _face_input_error(hold_detail, source="hold")
    _log_face_verify_stage(request_id, "hold", "PASS")

    id_diag = reference_face.describe_image_payload(
        body.imageBase64,
        content_type=body.contentType,
        filename=body.filename,
    )
    _log_face_verify_stage(
        request_id,
        "id_inspect",
        "INFO",
        declared_content_type=id_diag.get("declared_content_type"),
        detected_magic=id_diag.get("magic"),
        decoded_bytes=id_diag.get("decoded_bytes"),
        image_width=id_diag.get("image_width"),
        image_height=id_diag.get("image_height"),
        filename_extension=id_diag.get("filename_extension"),
    )
    try:
        id_bytes, _id_type = reference_face.decode_face_image(
            body.imageBase64,
            content_type=body.contentType,
            max_bytes=config.KIOSK_MAX_ID_IMAGE_BYTES,
        )
    except reference_face.ReferenceFaceError as error:
        _log_face_verify_stage(
            request_id,
            "id_decode",
            "FAIL",
            reason=error.reason,
            declared_content_type=id_diag.get("declared_content_type"),
            detected_magic=id_diag.get("magic"),
            decoded_bytes=id_diag.get("decoded_bytes"),
            filename_extension=id_diag.get("filename_extension"),
            source="id_image",
        )
        _log_face_verify_stage(request_id, "frame_a_decode", "NOT_REACHED")
        _log_face_verify_stage(request_id, "quality_gate", "NOT_REACHED")
        _log_face_verify_stage(request_id, "rekognition", "NOT_CALLED")
        _clear_store_verification(request)
        return _face_input_error(error.reason, source="id_image")
    _log_face_verify_stage(
        request_id,
        "id_decode",
        "PASS",
        detected_magic=id_diag.get("magic"),
        decoded_bytes=id_diag.get("decoded_bytes"),
        image_width=id_diag.get("image_width"),
        image_height=id_diag.get("image_height"),
        filename_extension=id_diag.get("filename_extension"),
    )

    face_diag = reference_face.describe_image_payload(body.faceImageBase64)
    _log_face_verify_stage(
        request_id,
        "frame_a_inspect",
        "INFO",
        detected_magic=face_diag.get("magic"),
        decoded_bytes=face_diag.get("decoded_bytes"),
        image_width=face_diag.get("image_width"),
        image_height=face_diag.get("image_height"),
    )
    try:
        face_bytes, _face_type = reference_face.decode_face_image(
            body.faceImageBase64,
            max_bytes=config.KIOSK_MAX_ID_IMAGE_BYTES,
        )
    except reference_face.ReferenceFaceError as error:
        _log_face_verify_stage(
            request_id,
            "frame_a_decode",
            "FAIL",
            reason=error.reason,
            detected_magic=face_diag.get("magic"),
            decoded_bytes=face_diag.get("decoded_bytes"),
            source="frame_a",
        )
        _log_face_verify_stage(request_id, "quality_gate", "NOT_REACHED")
        _log_face_verify_stage(request_id, "rekognition", "NOT_CALLED")
        _clear_store_verification(request)
        return _face_input_error(error.reason, source="frame_a")
    _log_face_verify_stage(
        request_id,
        "frame_a_decode",
        "PASS",
        detected_magic=face_diag.get("magic"),
        decoded_bytes=face_diag.get("decoded_bytes"),
        image_width=face_diag.get("image_width"),
        image_height=face_diag.get("image_height"),
    )

    # Re-check hold after local decode so an expiry during upload skips AWS.
    hold_tx, hold_detail = _active_store_hold(request, requested_tx)
    if not hold_tx:
        _log_face_verify_stage(
            request_id, "hold", "FAIL", reason=hold_detail, source="hold"
        )
        _log_face_verify_stage(request_id, "quality_gate", "NOT_REACHED")
        _log_face_verify_stage(request_id, "rekognition", "NOT_CALLED")
        _clear_store_verification(request)
        return _face_input_error(hold_detail, source="hold")

    # FRAME-A quality gate. The same face_bytes object is reused below.
    quality = _evaluate_camera_frame(face_bytes, request_id=request_id)
    if not quality.ok:
        _log_face_verify_stage(
            request_id,
            "quality_gate",
            "FAIL",
            reason=quality.reason or face_quality.REASON_QUALITY_CHECK_FAILED,
            detector=quality.detector_name,
            source="quality_gate",
        )
        _log_face_verify_stage(request_id, "rekognition", "NOT_CALLED")
        _clear_store_verification(request)
        return _face_input_error(
            quality.reason or face_quality.REASON_QUALITY_CHECK_FAILED,
            source="quality_gate",
        )
    _log_face_verify_stage(
        request_id,
        "quality_gate",
        "PASS",
        detector=quality.detector_name,
    )

    hold_tx, hold_detail = _active_store_hold(request, requested_tx)
    if not hold_tx:
        _log_face_verify_stage(
            request_id, "hold", "FAIL", reason=hold_detail, source="hold"
        )
        _log_face_verify_stage(request_id, "rekognition", "NOT_CALLED")
        _clear_store_verification(request)
        return _face_input_error(hold_detail, source="hold")

    logger.info(
        "event=rekognition_call_after_quality_gate request_id=%s operation=store_compare",
        request_id,
    )
    try:
        result = reference_face.compare_id_to_face_bytes(
            _rekognition, id_bytes, face_bytes
        )
    except reference_face.ReferenceFaceError as error:
        _log_face_verify_stage(
            request_id,
            "rekognition",
            "FAIL",
            reason=error.reason,
            source="rekognition",
        )
        logger.warning(
            "store_face_verify_failed request_id=%s reason=%s",
            request_id,
            error.reason,
        )
        _clear_store_verification(request)
        return _face_input_error(error.reason, source="rekognition")

    reason = result.get("reason") or "SERVICE_ERROR"
    matched = bool(result.get("success") and result.get("matched"))
    similarity = result.get("similarity")
    threshold = result.get("threshold", config.FACE_SIMILARITY_THRESHOLD)
    if matched and similarity is not None and float(similarity) >= float(threshold):
        _set_store_verification(
            request, reference_face.hash_face_image(face_bytes), similarity
        )
        _log_face_verify_stage(
            request_id, "rekognition", "PASS", reason=reason, source="rekognition"
        )
        logger.info(
            "store_face_verify_ok request_id=%s matched=true",
            request_id,
        )
    else:
        _clear_store_verification(request)
        _log_face_verify_stage(
            request_id, "rekognition", "FAIL", reason=reason, source="rekognition"
        )
        logger.info(
            "store_face_verify_result request_id=%s matched=false reason=%s",
            request_id,
            reason,
        )
    return _safe_face_result(
        success=bool(result.get("success")),
        matched=matched and reason == "SIMILARITY_ABOVE_THRESHOLD",
        similarity=similarity,
        threshold=threshold,
        reason=reason,
        source="rekognition",
    )


@app.post("/api/kiosk/store/complete")
def kiosk_store_complete(body: StoreCompleteIn, request: Request):
    require_user(request)
    transaction_id = _validated_transaction_id(body.transactionId)

    # Idempotent path: already STORED returns the same safe result without
    # requiring a new verification or creating another reference object.
    existing = _kiosk_store.get_transaction(transaction_id)
    if existing and existing.get("status") == "STORED":
        try:
            result = _kiosk_store.complete_store(transaction_id, amount=2000)
        except StoreTransactionUnavailable as error:
            raise HTTPException(status_code=409, detail=error.code)
        return _store_complete_response(result)

    hold_tx, hold_detail = _active_store_hold(request, transaction_id)
    if not hold_tx:
        raise HTTPException(status_code=403, detail=hold_detail)

    state, expected_hash = _store_verification_state(request)
    if state == "expired":
        _clear_store_verification(request)
        raise HTTPException(status_code=403, detail="STORE_FACE_VERIFICATION_EXPIRED")
    if state != "ok" or not expected_hash:
        raise HTTPException(status_code=403, detail="STORE_FACE_NOT_VERIFIED")
    if not body.verifiedFaceImageBase64:
        raise HTTPException(status_code=403, detail="STORE_FACE_NOT_VERIFIED")

    try:
        face_bytes, _face_type = reference_face.decode_face_image(
            body.verifiedFaceImageBase64,
            max_bytes=config.KIOSK_MAX_ID_IMAGE_BYTES,
        )
    except reference_face.ReferenceFaceError as error:
        raise HTTPException(status_code=400, detail=error.reason)

    if reference_face.hash_face_image(face_bytes) != expected_hash:
        raise HTTPException(status_code=403, detail="STORE_FACE_MISMATCH")

    if not face_collection.collection_id():
        logger.warning(
            "event=store_complete_stage stage=index result=FAIL "
            "reason=FACE_COLLECTION_NOT_CONFIGURED collection_configured=false"
        )
        raise HTTPException(status_code=502, detail="FACE_COLLECTION_NOT_CONFIGURED")

    hold_tx, hold_detail = _active_store_hold(request, transaction_id)
    if not hold_tx:
        raise HTTPException(status_code=403, detail=hold_detail)

    face_id = None
    try:
        logger.info(
            "event=store_complete_stage stage=index result=CALL "
            "collection_configured=true"
        )
        face_id = face_collection.index_verified_face(
            _rekognition, face_bytes, transaction_id
        )
        result = _kiosk_store.complete_store(
            transaction_id,
            amount=2000,
            rekognition_collection_id=face_collection.collection_id(),
            rekognition_face_id=face_id,
        )
    except reference_face.ReferenceFaceError as error:
        if face_id:
            _compensate_indexed_face(face_id)
        logger.warning(
            "store_complete_index_failed transaction_id=%s reason=%s",
            transaction_id,
            error.reason,
        )
        raise HTTPException(status_code=502, detail=error.reason)
    except (StoreTransactionUnavailable, InvalidKioskInput) as error:
        if face_id:
            _compensate_indexed_face(face_id)
        status = 409 if isinstance(error, StoreTransactionUnavailable) else 400
        raise HTTPException(status_code=status, detail=error.code)
    except Exception:
        if face_id:
            _compensate_indexed_face(face_id)
        raise

    _clear_store_verification(request)
    _clear_store_hold(request)
    logger.info(
        "store_complete_ok transaction_id=%s reference_mode=collection",
        transaction_id,
    )
    return _store_complete_response(result)


def _store_complete_response(result):
    # Never expose reference_face_s3_key to the browser.
    return {
        "transactionId": result["transactionId"],
        "lockerId": result["lockerId"],
        "retrievalCode": result["retrievalCode"],
        "mockPaymentId": result["mockPaymentId"],
        "amount": result["amount"],
        "status": result["status"],
        "referencePresent": bool(result.get("referencePresent")),
    }


@app.post("/api/kiosk/retrieve/lookup")
def kiosk_retrieve_lookup(body: RetrievalLookupIn, request: Request):
    """SQLite-only retrieval gate before the camera starts.

    Invalid or non-STORED codes are rejected here. No OpenCV, no Rekognition,
    no S3. FaceId and locker id stay on the server.
    """
    require_user(request)
    retrieval_code = _validated_retrieval_code(body.retrievalCode)
    scope = _retrieval_rate_scope(request)
    retry_after = _retrieval_limiter.retry_after(scope)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail="RETRIEVAL_RATE_LIMITED",
            headers={"Retry-After": str(retry_after)},
        )

    stored = _kiosk_store.get_stored_by_retrieval_code(retrieval_code)
    if not stored:
        _retrieval_limiter.record_failure(scope)
        logger.info(
            "event=retrieve_lookup result=FAIL reason=RETRIEVAL_UNAVAILABLE aws=0"
        )
        raise HTTPException(status_code=409, detail="RETRIEVAL_UNAVAILABLE")
    _require_stored_face_id(stored)

    _retrieval_limiter.record_success(scope)
    logger.info("event=retrieve_lookup result=PASS status=STORED auth_mode=collection aws=0")
    return {"ready": True, "status": "STORED"}


@app.post("/api/kiosk/retrieve/start")
def kiosk_retrieve_start(body: RetrievalStartIn, request: Request):
    """Start retrieval only after retrieval_code + face verification succeed.

    Requires faceImageBase64 (current FRAME-B). Code-only open is rejected.
    FRAME-B is compared in memory and is not written to S3/Kinesis/DynamoDB.
    """
    require_user(request)
    retrieval_code = _validated_retrieval_code(body.retrievalCode)
    if not body.faceImageBase64:
        raise HTTPException(status_code=400, detail="FACE_IMAGE_REQUIRED")

    scope = _retrieval_rate_scope(request)
    retry_after = _retrieval_limiter.retry_after(scope)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail="RETRIEVAL_RATE_LIMITED",
            headers={"Retry-After": str(retry_after)},
        )

    stored = _kiosk_store.get_stored_by_retrieval_code(retrieval_code)
    if not stored:
        _retrieval_limiter.record_failure(scope)
        # Do not reveal whether the code exists beyond the standard error.
        raise HTTPException(status_code=409, detail="RETRIEVAL_UNAVAILABLE")

    transaction_id = stored.get("transaction_id")
    _require_stored_face_id(stored)

    try:
        face_bytes, _face_type = reference_face.decode_face_image(
            body.faceImageBase64,
            max_bytes=config.KIOSK_MAX_ID_IMAGE_BYTES,
        )
    except reference_face.ReferenceFaceError as error:
        return _safe_face_result(
            success=False,
            matched=False,
            similarity=None,
            threshold=config.FACE_SIMILARITY_THRESHOLD,
            reason=error.reason,
            status="STORED",
            source="frame_b",
        )

    # FRAME-B quality gate. The same face_bytes object is reused below.
    # Local quality failures must not call S3 or Rekognition.
    quality = _evaluate_camera_frame(face_bytes)
    if not quality.ok:
        return _safe_face_result(
            success=False,
            matched=False,
            similarity=None,
            threshold=config.FACE_SIMILARITY_THRESHOLD,
            reason=quality.reason or face_quality.REASON_QUALITY_CHECK_FAILED,
            status="STORED",
            source="quality_gate",
        )

    logger.info(
        "event=rekognition_call_after_quality_gate operation=retrieve_compare"
    )
    try:
        compare_result = _retrieve_compare_frame(stored, face_bytes)
    except reference_face.ReferenceFaceError as error:
        logger.warning(
            "retrieve_face_compare_failed transaction_id=%s reason=%s",
            transaction_id,
            error.reason,
        )
        return _safe_face_result(
            success=False,
            matched=False,
            similarity=None,
            threshold=config.FACE_SIMILARITY_THRESHOLD,
            reason=error.reason,
            status="STORED",
            source="rekognition",
        )

    reason = compare_result.get("reason") or "SERVICE_ERROR"
    similarity = compare_result.get("similarity")
    threshold = compare_result.get("threshold", config.FACE_SIMILARITY_THRESHOLD)
    matched = bool(
        compare_result.get("success")
        and compare_result.get("matched")
        and reason == "SIMILARITY_ABOVE_THRESHOLD"
    )

    if not matched:
        logger.info(
            "retrieve_face_mismatch transaction_id=%s reason=%s "
            "similarity=%s reference_present=true",
            transaction_id,
            reason,
            similarity,
        )
        return _safe_face_result(
            success=bool(compare_result.get("success")),
            matched=False,
            similarity=similarity,
            threshold=threshold,
            reason=reason,
            status="STORED",
            source="rekognition",
        )

    try:
        started = _kiosk_store.start_retrieval(retrieval_code)
    except RetrievalUnavailable:
        _retrieval_limiter.record_failure(scope)
        raise HTTPException(status_code=409, detail="RETRIEVAL_UNAVAILABLE")

    _retrieval_limiter.record_success(scope)
    logger.info(
        "retrieve_authorized transaction_id=%s locker_id=%s "
        "matched=true similarity=%s",
        started["transactionId"],
        started["lockerId"],
        similarity,
    )
    return _safe_face_result(
        success=True,
        matched=True,
        similarity=similarity,
        threshold=threshold,
        reason="SIMILARITY_ABOVE_THRESHOLD",
        transaction_id=started["transactionId"],
        locker_id=started["lockerId"],
        additional_fee=started.get("additionalFee", 0),
        status="RETRIEVING",
        source="rekognition",
    )


@app.post("/api/kiosk/retrieve/recover")
def kiosk_retrieve_recover(body: RetrievalStartIn, request: Request):
    require_user(request)
    retrieval_code = _validated_retrieval_code(body.retrievalCode)
    return _run_retrieval_code_lookup(
        request, lambda: _kiosk_store.recover_retrieval(retrieval_code)
    )


@app.post("/api/kiosk/retrieve/cancel")
def kiosk_retrieve_cancel(body: TransactionIn, request: Request):
    require_user(request)
    transaction_id = _validated_transaction_id(body.transactionId)
    return {
        "transactionId": transaction_id,
        "cancelled": _kiosk_store.cancel_retrieval(transaction_id),
    }


@app.post("/api/kiosk/retrieve/complete")
def kiosk_retrieve_complete(body: TransactionIn, request: Request):
    require_user(request)
    transaction_id = _validated_transaction_id(body.transactionId)
    try:
        result = _kiosk_store.complete_retrieval(transaction_id)
    except RetrievalUnavailable:
        raise HTTPException(status_code=409, detail="RETRIEVAL_UNAVAILABLE")

    # Biometric cleanup after RETRIEVED. Failure must not reopen the locker.
    _cleanup_retrieved_biometrics(transaction_id)
    return result


def _cleanup_retrieved_biometrics(transaction_id):
    txn = _kiosk_store.get_transaction(transaction_id)
    if not txn:
        return
    face_id = txn.get("rekognition_face_id")
    if face_id and not txn.get("face_deleted_at"):
        try:
            face_collection.delete_indexed_faces(
                _rekognition,
                [face_id],
                target_collection=txn.get("rekognition_collection_id"),
            )
            _kiosk_store.mark_face_deleted(transaction_id)
            logger.info(
                "event=face_delete_ok transaction_id=%s cleanup=retrieved",
                transaction_id,
            )
        except reference_face.ReferenceFaceError as error:
            logger.warning(
                "event=face_delete_pending transaction_id=%s reason=%s face_id=%s",
                transaction_id,
                error.reason,
                face_collection.mask_face_id(face_id),
            )


@app.get("/api/kiosk/transactions/{transaction_id}")
def kiosk_transaction_status(transaction_id: str, request: Request):
    require_user(request)
    transaction_id = _validated_transaction_id(transaction_id)
    result = _kiosk_store.get_transaction_status(transaction_id)
    if not result:
        raise HTTPException(status_code=404, detail="TRANSACTION_NOT_FOUND")
    return result



@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# --- static SPA (served WITHOUT exposing this backend/ directory) ----------
# We deliberately do NOT mount "/" over web-ui/ (that would serve backend/*.py and
# a local .env). Only /src and a whitelist of root assets are exposed.
app.mount("/src", StaticFiles(directory=str(ROOT / "src")), name="src")

_ROOT_ASSETS = {
    "logo.png", "favicon.ico", "favicon-16x16.png", "favicon-32x32.png",
    "favicon-96x96.png", "ms-icon-144x144.png",
}


@app.get("/")
def index():
    return RedirectResponse(url="/kiosk", status_code=307)


@app.get("/kiosk")
def kiosk():
    return FileResponse(str(ROOT / "kiosk.html"))


@app.get("/{asset}")
def root_asset(asset: str):
    if asset in _ROOT_ASSETS and (ROOT / asset).is_file():
        return FileResponse(str(ROOT / asset))
    raise HTTPException(status_code=404, detail="not found")
