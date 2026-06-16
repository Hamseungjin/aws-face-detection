from __future__ import print_function

import base64
import binascii
import datetime
import decimal
import json
import time
import traceback
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError, BotoCoreError

CONFIG_FILE = "faceverifier-params.json"
REQUIRED_FRAME_FIELDS = [
    "frame_id",
    "s3_bucket",
    "s3_key",
    "processed_timestamp",
    "approx_capture_timestamp",
]
DEFAULT_CONFIG = {
    "ddb_table": "EnrichedFrame",
    "ddb_gsi_name": "processed_year_month-processed_timestamp-index",
    "timezone": "Asia/Seoul",
    "similarity_threshold": 90.0,
    "rekognition_api_similarity_threshold": 0.0,
    "quality_filter": "NONE",
    "latest_frame_horizon_minutes": 5,
    "max_source_image_bytes": 5242880,
    "allowed_source_content_types": ["image/jpeg", "image/png"],
    "allow_multiple_faces_in_id_image": False,
}


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, decimal.Decimal):
            return float(o) if o % 1 else int(o)
        return super(DecimalEncoder, self).default(o)


def load_config(path=CONFIG_FILE):
    config = DEFAULT_CONFIG.copy()
    with open(path, "r") as conf_file:
        config.update(json.loads(conf_file.read()))
    return config


def cors_headers():
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token",
    }


def respond(status_code, payload):
    return {"statusCode": status_code, "headers": cors_headers(), "body": json.dumps(payload, cls=DecimalEncoder)}


def empty_analysis():
    return {"detected": False, "face_count": 0, "selected_face": None}


def build_result(success, matched, similarity, threshold, reason, id_image_analysis=None, latest_frame=None, error=None):
    return {
        "success": success,
        "matched": matched,
        "similarity": similarity,
        "threshold": threshold,
        "reason": reason,
        "id_image_analysis": id_image_analysis if id_image_analysis is not None else empty_analysis(),
        "latest_frame": latest_frame,
        "error": error,
    }


def error_response(reason, threshold, message, service=None, code=None, status_code=200, analysis=None, latest_frame=None):
    return respond(status_code, build_result(False, False, None, threshold, reason, analysis, latest_frame, {
        "service": service,
        "code": code,
        "message": message,
    }))


def parse_body(event):
    try:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        return json.loads(body), None
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None, "Request body must be valid JSON."


def decode_image(payload, config):
    content_type = payload.get("id_image_content_type")
    allowed = config.get("allowed_source_content_types", DEFAULT_CONFIG["allowed_source_content_types"])
    if content_type not in allowed:
        return None, "UNSUPPORTED_IMAGE_TYPE", "Only image/jpeg and image/png are supported."
    encoded = payload.get("id_image_base64")
    if not encoded:
        return None, "INVALID_REQUEST_BODY", "id_image_base64 is required."
    if "," in encoded and encoded.strip().startswith("data:"):
        encoded = encoded.split(",", 1)[1]
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None, "INVALID_IMAGE_BASE64", "id_image_base64 must be valid base64."
    if len(image_bytes) > int(config.get("max_source_image_bytes", DEFAULT_CONFIG["max_source_image_bytes"])):
        return None, "IMAGE_TOO_LARGE", "Uploaded image exceeds the configured size limit."
    return image_bytes, None, None


def normalize_float(value):
    if value is None:
        return None
    return float(value)


def normalize_rekognition_face(face):
    box = face.get("BoundingBox") or {}
    quality = face.get("Quality") or {}
    pose = face.get("Pose") or {}
    return {
        "bounding_box": {
            "left": normalize_float(box.get("Left")),
            "top": normalize_float(box.get("Top")),
            "width": normalize_float(box.get("Width")),
            "height": normalize_float(box.get("Height")),
        },
        "confidence": normalize_float(face.get("Confidence")),
        "quality": {
            "brightness": normalize_float(quality.get("Brightness")),
            "sharpness": normalize_float(quality.get("Sharpness")),
        },
        "pose": {
            "roll": normalize_float(pose.get("Roll")),
            "yaw": normalize_float(pose.get("Yaw")),
            "pitch": normalize_float(pose.get("Pitch")),
        },
    }


def analyze_id_image(rekognition, image_bytes, config):
    response = rekognition.detect_faces(Image={"Bytes": image_bytes}, Attributes=["ALL"])
    details = response.get("FaceDetails", [])
    face_count = len(details)
    if face_count == 0:
        return {"detected": False, "face_count": 0, "selected_face": None}, "NO_FACE_IN_ID_IMAGE"
    selected = max(details, key=lambda f: f.get("Confidence", 0))
    analysis = {"detected": True, "face_count": face_count, "selected_face": normalize_rekognition_face(selected)}
    if face_count > 1 and not config.get("allow_multiple_faces_in_id_image", False):
        return analysis, "MULTIPLE_FACES_IN_ID_IMAGE"
    return analysis, None


def get_current_and_previous_month_keys(now):
    current = now.strftime("%Y%m")
    first = now.replace(day=1)
    previous = (first - datetime.timedelta(days=1)).strftime("%Y%m")
    return [current, previous]


def query_latest_frame_from_ddb(ddb_table, gsi_name, timezone_name):
    now = datetime.datetime.now(ZoneInfo(timezone_name))
    candidates = []
    for month_key in get_current_and_previous_month_keys(now):
        response = ddb_table.query(
            IndexName=gsi_name,
            KeyConditionExpression=Key("processed_year_month").eq(month_key),
            Limit=1,
            ScanIndexForward=False,
        )
        candidates.extend(response.get("Items", []))
    if not candidates:
        return None
    return max(candidates, key=lambda item: float(item.get("processed_timestamp", 0)))


def extract_required_frame_fields(item):
    missing = [field for field in REQUIRED_FRAME_FIELDS if field not in item or item[field] in (None, "")]
    if missing:
        raise KeyError(",".join(missing))
    processed_timestamp = float(item["processed_timestamp"])
    return {
        "frame_id": item["frame_id"],
        "s3_bucket": item["s3_bucket"],
        "s3_key": item["s3_key"],
        "processed_timestamp": processed_timestamp,
        "approx_capture_timestamp": float(item["approx_capture_timestamp"]),
        "age_seconds": round(time.time() - processed_timestamp, 3),
    }


def validate_latest_frame_age(latest_frame, horizon_minutes):
    return latest_frame["age_seconds"] <= float(horizon_minutes) * 60


def select_best_similarity(face_matches):
    if not face_matches:
        return None
    return max(float(match.get("Similarity", 0)) for match in face_matches)


def compare_faces(rekognition, image_bytes, frame, config):
    kwargs = {
        "SourceImage": {"Bytes": image_bytes},
        "TargetImage": {"S3Object": {"Bucket": frame["s3_bucket"], "Name": frame["s3_key"]}},
        "SimilarityThreshold": float(config.get("rekognition_api_similarity_threshold", 0.0)),
    }
    quality_filter = config.get("quality_filter")
    if quality_filter:
        kwargs["QualityFilter"] = quality_filter
    return rekognition.compare_faces(**kwargs)


def face_verify(event, context, config=None, dynamodb_resource=None, rekognition_client=None):
    config = config or load_config()
    threshold = float(config.get("similarity_threshold", DEFAULT_CONFIG["similarity_threshold"]))
    request_id = getattr(context, "aws_request_id", None) if context else None

    if event.get("httpMethod") == "OPTIONS":
        return respond(200, {})

    payload, parse_error = parse_body(event)
    if parse_error:
        print("request_id=%s reason=INVALID_REQUEST_BODY" % request_id)
        return error_response("INVALID_REQUEST_BODY", threshold, parse_error, status_code=400)
    try:
        if payload.get("threshold") is not None:
            threshold = float(payload.get("threshold"))
    except (TypeError, ValueError):
        return error_response("INVALID_REQUEST_BODY", threshold, "threshold must be a number.", status_code=400)

    image_bytes, reason, message = decode_image(payload, config)
    if reason:
        print("request_id=%s reason=%s" % (request_id, reason))
        return error_response(reason, threshold, message, status_code=400)

    rekognition = rekognition_client or boto3.client("rekognition", region_name=config.get("region"))
    dynamodb = dynamodb_resource or boto3.resource("dynamodb", region_name=config.get("region"))

    try:
        analysis, face_error = analyze_id_image(rekognition, image_bytes, config)
        if face_error == "NO_FACE_IN_ID_IMAGE":
            return error_response(face_error, threshold, "No face was detected in the uploaded ID image.", "rekognition", analysis=analysis)
        if face_error == "MULTIPLE_FACES_IN_ID_IMAGE":
            return error_response(face_error, threshold, "Multiple faces were detected in the uploaded ID image.", "rekognition", analysis=analysis)

        table = dynamodb.Table(config["ddb_table"])
        item = query_latest_frame_from_ddb(table, config["ddb_gsi_name"], config.get("timezone", "UTC"))
        if not item:
            return error_response("NO_LATEST_FRAME", threshold, "No frame metadata was found in DynamoDB.", "dynamodb", analysis=analysis)
        try:
            latest_frame = extract_required_frame_fields(item)
        except KeyError as exc:
            return error_response("MISSING_DDB_FIELD", threshold, "DynamoDB item is missing required field(s): %s" % exc, "dynamodb", analysis=analysis)
        if not validate_latest_frame_age(latest_frame, config.get("latest_frame_horizon_minutes", 5)):
            return error_response("NO_RECENT_FRAME", threshold, "Latest frame is older than the configured horizon.", "dynamodb", analysis=analysis, latest_frame=latest_frame)

        try:
            compare_response = compare_faces(rekognition, image_bytes, latest_frame, config)
        except (ClientError, BotoCoreError) as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
            return error_response("REKOGNITION_COMPARE_FAILED", threshold, "Rekognition CompareFaces failed.", "rekognition", code, analysis=analysis, latest_frame=latest_frame)

        similarity = select_best_similarity(compare_response.get("FaceMatches", []))
        matched = similarity is not None and similarity >= threshold
        result_reason = "SIMILARITY_ABOVE_THRESHOLD" if matched else "SIMILARITY_BELOW_THRESHOLD"
        return respond(200, build_result(True, matched, similarity, threshold, result_reason, analysis, latest_frame, None))
    except Exception:
        print("request_id=%s reason=INTERNAL_ERROR" % request_id)
        traceback.print_exc()
        return error_response("INTERNAL_ERROR", threshold, "An internal error occurred.", status_code=500)


def handler(event, context):
    return face_verify(event, context)
