# Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# Licensed under the Amazon Software License (the "License"). You may not use this file except in compliance with the License. A copy of the License is located at
#     http://aws.amazon.com/asl/
# or in the "license" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and limitations under the License.

"""facecompare Lambda.

Compares a face in an uploaded image (sent as base64 in the POST body) against
one camera frame stored in S3/DynamoDB, using Amazon Rekognition CompareFaces.
``targetFrameId`` selects that exact frame for citizen-kiosk requests. Omitting
it retains the global-latest lookup only for the legacy operator workflow.
Exposed via API Gateway: POST /face-compare (AWS_PROXY integration).

The core logic is ported from scripts/compare_latest_frame_with_id.py (the
local CLI), adapted for Lambda: config is read from the bundled params file,
the image arrives as base64 in the request (used as Bytes, never persisted),
and the result is returned as an API Gateway proxy response.

PRIVACY: the uploaded image base64 / decoded bytes are NEVER logged. Only
frame_id, similarity and reason are logged.
"""

from __future__ import print_function

import base64
import binascii
import datetime
import json
import time
import uuid

import boto3
import botocore.exceptions
from boto3.dynamodb.conditions import Key
from zoneinfo import ZoneInfo

ALLOWED_CONTENT_TYPES = ("image/jpeg", "image/png")

# Config keys the handler relies on (bundled facecompare-params.json).
REQUIRED_CONFIG_KEYS = [
    "ddb_table",
    "ddb_gsi_name",
    "timezone",
    "similarity_threshold",
    "rekognition_api_similarity_threshold",
    "quality_filter",
    "latest_frame_horizon_minutes",
    "max_source_image_bytes",
]

# Map each reason to the HTTP status of the API Gateway proxy response.
#   2xx  -> request was understood and produced a comparison/business outcome
#   400  -> bad client input (image/payload problem)
#   5xx  -> server/config/AWS-side problem
HTTP_STATUS = {
    "SIMILARITY_ABOVE_THRESHOLD": 200,
    "SIMILARITY_BELOW_THRESHOLD": 200,
    "NO_FACE_IN_SOURCE_OR_TARGET": 200,
    "NO_LATEST_FRAME": 200,
    "NO_RECENT_FRAME": 200,
    "FRAME_METADATA_INVALID": 200,
    "TARGET_FRAME_NOT_READY": 200,
    "TARGET_FRAME_TOO_OLD": 200,
    "BAD_REQUEST": 400,
    "INVALID_TARGET_FRAME_ID": 400,
    "UNSUPPORTED_IMAGE_FORMAT": 400,
    "IMAGE_TOO_LARGE": 400,
    "INVALID_IMAGE": 400,
    "CONFIG_ERROR": 500,
    "INVALID_S3_OBJECT": 502,
    "ACCESS_DENIED": 500,
    "THROTTLED": 503,
    "AWS_API_ERROR": 502,
    "SERVICE_ERROR": 500,
}
DEFAULT_STATUS = 500

# boto3 clients reuse the Lambda execution role + region from the environment.
dynamodb = boto3.resource("dynamodb")
rekog_client = boto3.client("rekognition")


class CompareError(Exception):
    """Carries a stable reason code (plus optional detail / AWS error code)
    that maps to a result JSON and an HTTP status."""

    def __init__(self, reason, detail=None, aws_code=None):
        super(CompareError, self).__init__(reason)
        self.reason = reason
        self.detail = detail
        self.aws_code = aws_code


def load_config():
    try:
        with open("facecompare-params.json", "r") as conf_file:
            config = json.loads(conf_file.read())
    except (ValueError, OSError) as err:
        raise CompareError("CONFIG_ERROR", detail="cannot read config: {}".format(err))

    missing = [key for key in REQUIRED_CONFIG_KEYS if key not in config]
    if missing:
        raise CompareError("CONFIG_ERROR",
                           detail="missing config keys: {}".format(", ".join(missing)))
    return config


def _to_float(value):
    if value is None:
        return None
    return float(value)


def parse_body(event):
    """Parse the API Gateway proxy request body into a dict."""
    raw = event.get("body")
    if raw is None:
        raise CompareError("BAD_REQUEST", detail="empty request body")
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception:
            raise CompareError("BAD_REQUEST", detail="cannot decode request body")
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        raise CompareError("BAD_REQUEST", detail="request body is not valid JSON")
    if not isinstance(body, dict):
        raise CompareError("BAD_REQUEST", detail="request body must be a JSON object")
    return body


def decode_image(body, config):
    """Validate and decode the uploaded image. Returns (bytes, content_type).

    NOTE: the returned bytes must never be logged or printed."""
    image_b64 = body.get("imageBase64")
    if not image_b64:
        raise CompareError("BAD_REQUEST", detail="imageBase64 is required")

    content_type = (body.get("contentType") or "").lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise CompareError("UNSUPPORTED_IMAGE_FORMAT",
                           detail="contentType must be one of {}".format(list(ALLOWED_CONTENT_TYPES)))

    # Defensively strip a data-URL prefix ("data:image/jpeg;base64,...").
    if image_b64.strip().startswith("data:") and "," in image_b64:
        image_b64 = image_b64.split(",", 1)[1]

    try:
        image_bytes = base64.b64decode(image_b64, validate=True)
    except (binascii.Error, ValueError):
        raise CompareError("INVALID_IMAGE", detail="imageBase64 is not valid base64")

    if not image_bytes:
        raise CompareError("INVALID_IMAGE", detail="decoded image is empty")

    max_bytes = int(config["max_source_image_bytes"])
    if len(image_bytes) > max_bytes:
        raise CompareError("IMAGE_TOO_LARGE",
                           detail="image {} bytes exceeds max {} bytes".format(len(image_bytes), max_bytes))
    return image_bytes, content_type


def validate_target_frame_id(value):
    """Return a canonical UUIDv4 target id or reject exact-frame mode."""
    if not isinstance(value, str) or len(value) != 36:
        raise CompareError("INVALID_TARGET_FRAME_ID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise CompareError("INVALID_TARGET_FRAME_ID")
    if parsed.version != 4 or str(parsed) != value:
        raise CompareError("INVALID_TARGET_FRAME_ID")
    return value


def _query_month(table, gsi_name, year_month):
    resp = table.query(
        IndexName=gsi_name,
        KeyConditionExpression=Key("processed_year_month").eq(year_month),
        ScanIndexForward=False,  # most recent processed_timestamp first
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def query_latest_frame(config):
    """Return the most recent EnrichedFrame item, or None.

    Queries the current "YYYYMM" partition first; if empty (e.g. just after a
    month rollover) it falls back to the previous month."""
    table = dynamodb.Table(config["ddb_table"])
    gsi_name = config["ddb_gsi_name"]
    now_local = datetime.datetime.now(ZoneInfo(config["timezone"]))
    try:
        item = _query_month(table, gsi_name, now_local.strftime("%Y%m"))
        if item is None:
            prev_month = (now_local.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y%m")
            item = _query_month(table, gsi_name, prev_month)
    except botocore.exceptions.ClientError as err:
        raise _aws_client_error(err)
    return item


def get_frame_by_id(config, frame_id):
    """Strongly read the EnrichedFrame table's frame_id primary key."""
    table = dynamodb.Table(config["ddb_table"])
    try:
        response = table.get_item(
            Key={"frame_id": frame_id},
            ConsistentRead=True,
        )
    except botocore.exceptions.ClientError as err:
        raise _aws_client_error(err)
    return response.get("Item")


def compare_faces(image_bytes, target, config):
    """Call Rekognition CompareFaces and return the best similarity (0-100).

    The uploaded image is the SourceImage (Bytes, never persisted); the selected
    frame is the TargetImage (read directly from S3 by Rekognition). The API is
    called with SimilarityThreshold=0.0 so nothing is filtered; the business
    threshold is applied by the caller."""
    try:
        resp = rekog_client.compare_faces(
            SourceImage={"Bytes": image_bytes},
            TargetImage={"S3Object": {"Bucket": target["frame_s3_bucket"],
                                      "Name": target["frame_s3_key"]}},
            SimilarityThreshold=float(config["rekognition_api_similarity_threshold"]),
            QualityFilter=config["quality_filter"],
        )
    except botocore.exceptions.ClientError as err:
        raise _rekognition_error(err)

    face_matches = resp.get("FaceMatches", [])
    unmatched_faces = resp.get("UnmatchedFaces", [])
    if not face_matches:
        if not unmatched_faces:
            # Source had a face (else InvalidParameterException), but the target
            # frame has none -> no comparable face.
            raise CompareError("NO_FACE_IN_SOURCE_OR_TARGET", detail="likely_target")
        # A face exists in the target but did not match at all.
        return 0.0
    return max(float(fm["Similarity"]) for fm in face_matches)


def _rekognition_error(err):
    code = err.response.get("Error", {}).get("Code", "")
    message = err.response.get("Error", {}).get("Message", "")
    if code == "InvalidParameterException":
        # Most commonly raised when no face is detectable in the SOURCE image.
        return CompareError("NO_FACE_IN_SOURCE_OR_TARGET", detail="likely_source", aws_code=code)
    if code == "InvalidS3ObjectException":
        return CompareError("INVALID_S3_OBJECT", detail=message, aws_code=code)
    if code == "ImageTooLargeException":
        return CompareError("IMAGE_TOO_LARGE", detail="target image too large", aws_code=code)
    return _aws_client_error(err)


def _aws_client_error(err):
    code = err.response.get("Error", {}).get("Code", "")
    message = err.response.get("Error", {}).get("Message", "")
    if code in ("AccessDeniedException", "AccessDenied"):
        return CompareError("ACCESS_DENIED", detail=message, aws_code=code)
    if code in ("ProvisionedThroughputExceededException", "ThrottlingException",
                "ThrottledException", "Throttling", "RequestLimitExceeded"):
        return CompareError("THROTTLED", detail=message, aws_code=code)
    return CompareError("AWS_API_ERROR", detail=message, aws_code=code)


def build_result(success, matched, similarity, threshold, reason, source, target,
                 age_seconds, error, target_frame_id=None):
    return {
        "success": success,
        "matched": matched,
        "similarity": similarity,
        "threshold": threshold,
        "reason": reason,
        "source": source,
        "target": target,
        "targetFrameId": target_frame_id,
        "age_seconds": age_seconds,
        "error": error,
    }


def respond(result):
    status = HTTP_STATUS.get(result.get("reason"), DEFAULT_STATUS)
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(result),
    }


def run(event, context=None):
    # Accumulate context so the error path can report whatever we already have.
    # source carries only NON-sensitive metadata (filename, type, byte length).
    source = {"filename": None, "contentType": None, "image_bytes": None}
    target = None
    age_seconds = None
    threshold = None
    exact_mode = False
    requested_target_frame_id = None

    # KPI timers (ms); stay None until the corresponding step runs.
    t_start = time.perf_counter()
    source_image_prepare_ms = None
    recent_frame_lookup_ms = None
    rekognition_compare_faces_ms = None

    def log_kpi(reason):
        # Structured KPI log (CloudWatch). PRIVACY: only sizes/ids/timings -- never image bytes.
        print(json.dumps({
            "component": "facecompare",
            "event": "compare_complete",
            "request_id": getattr(context, "aws_request_id", None),
            "reason": reason,
            "frame_id": (target or {}).get("frame_id"),
            "source_image_bytes": source.get("image_bytes"),
            "recent_frame_lookup_ms": recent_frame_lookup_ms,
            "source_image_prepare_ms": source_image_prepare_ms,
            "rekognition_compare_faces_ms": rekognition_compare_faces_ms,
            "facecompare_total_lambda_ms": round((time.perf_counter() - t_start) * 1000.0, 1),
            "age_seconds": age_seconds,
        }, default=str))

    try:
        config = load_config()

        body = parse_body(event)
        source["filename"] = body.get("filename")

        # Presence of the field opts into exact mode. Empty/null/malformed values
        # are errors and must never fall through to the legacy global-latest path.
        exact_mode = "targetFrameId" in body
        if exact_mode:
            requested_target_frame_id = validate_target_frame_id(
                body.get("targetFrameId")
            )

        _prep_start = time.perf_counter()
        image_bytes, content_type = decode_image(body, config)
        source_image_prepare_ms = round((time.perf_counter() - _prep_start) * 1000.0, 1)
        source["contentType"] = content_type
        source["image_bytes"] = len(image_bytes)  # length ONLY -- never the bytes

        try:
            if body.get("similarityThreshold") is not None:
                threshold = float(body["similarityThreshold"])
            else:
                threshold = float(config["similarity_threshold"])
        except (TypeError, ValueError):
            raise CompareError("BAD_REQUEST")
        if threshold < 0 or threshold > 100:
            raise CompareError("BAD_REQUEST")

        now_epoch = time.time()
        _lookup_start = time.perf_counter()
        if exact_mode:
            item = get_frame_by_id(config, requested_target_frame_id)
        else:
            # LEGACY OPERATOR COMPATIBILITY ONLY. The citizen kiosk must always
            # send targetFrameId and must never select a globally latest frame.
            item = query_latest_frame(config)
        recent_frame_lookup_ms = round((time.perf_counter() - _lookup_start) * 1000.0, 1)
        if item is None:
            if exact_mode:
                raise CompareError("TARGET_FRAME_NOT_READY")
            raise CompareError("NO_LATEST_FRAME")

        missing = [key for key in ("frame_id", "s3_bucket", "s3_key") if not item.get(key)]
        if missing:
            raise CompareError("FRAME_METADATA_INVALID",
                               detail="missing fields: {}".format(", ".join(missing)))

        target = {
            "frame_id": item.get("frame_id"),
            "frame_s3_bucket": item.get("s3_bucket"),
            "frame_s3_key": item.get("s3_key"),
            "processed_timestamp": _to_float(item.get("processed_timestamp")),
            "approx_capture_timestamp": _to_float(item.get("approx_capture_timestamp")),
        }

        capture_ts = target["approx_capture_timestamp"]
        if capture_ts is None:
            capture_ts = target["processed_timestamp"]
        if capture_ts is None:
            raise CompareError("FRAME_METADATA_INVALID", detail="no usable timestamp on frame")

        age_seconds = round(now_epoch - capture_ts, 3)
        horizon_seconds = float(config["latest_frame_horizon_minutes"]) * 60
        if age_seconds > horizon_seconds:
            raise CompareError(
                "TARGET_FRAME_TOO_OLD" if exact_mode else "NO_RECENT_FRAME")

        _compare_start = time.perf_counter()
        similarity = compare_faces(image_bytes, target, config)
        rekognition_compare_faces_ms = round((time.perf_counter() - _compare_start) * 1000.0, 1)
        matched = similarity >= threshold
        reason = "SIMILARITY_ABOVE_THRESHOLD" if matched else "SIMILARITY_BELOW_THRESHOLD"
        response_target = target
        if exact_mode:
            # The kiosk needs correlation confirmation, not internal AWS locations.
            response_target = {
                "frame_id": target["frame_id"],
                "processed_timestamp": target["processed_timestamp"],
                "approx_capture_timestamp": target["approx_capture_timestamp"],
            }
        result = build_result(
            True, matched, round(similarity, 4), threshold, reason, source,
            response_target, age_seconds, None,
            requested_target_frame_id if exact_mode else target.get("frame_id"),
        )
        print("facecompare ok: frame_id={} similarity={} matched={}".format(
            target.get("frame_id"), result["similarity"], matched))
        log_kpi(reason)
        return result

    except CompareError as err:
        print("facecompare fail: reason={} aws_code={}".format(err.reason, err.aws_code))
        log_kpi(err.reason)
        response_target = target
        if exact_mode and target:
            response_target = {
                "frame_id": target.get("frame_id"),
                "processed_timestamp": target.get("processed_timestamp"),
                "approx_capture_timestamp": target.get("approx_capture_timestamp"),
            }
        return build_result(
            False, False, None, threshold, err.reason, source, response_target,
            age_seconds, {"code": err.reason}, requested_target_frame_id,
        )
    except botocore.exceptions.NoCredentialsError:
        log_kpi("ACCESS_DENIED")
        return build_result(
            False, False, None, threshold, "ACCESS_DENIED", source, None,
            age_seconds, {"code": "ACCESS_DENIED"}, requested_target_frame_id,
        )
    except botocore.exceptions.BotoCoreError as err:
        log_kpi("AWS_API_ERROR")
        return build_result(
            False, False, None, threshold, "AWS_API_ERROR", source, None,
            age_seconds, {"code": "AWS_API_ERROR"}, requested_target_frame_id,
        )
    except Exception:
        # Never return raw service messages, request ids, or stack traces.
        log_kpi("SERVICE_ERROR")
        return build_result(
            False, False, None, threshold, "SERVICE_ERROR", source, None,
            age_seconds, {"code": "SERVICE_ERROR"}, requested_target_frame_id,
        )


def handler(event, context):
    return respond(run(event, context))
