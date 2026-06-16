"""Compare a face in a local ID-card image with the face in the latest camera
frame stored in S3/DynamoDB, using AWS Rekognition CompareFaces.

This is a local CLI MVP for kiosk ID verification. It does NOT modify any
existing Lambda / Web UI / CloudFormation. It only reads from DynamoDB and S3
and calls Rekognition.

Usage:
    python scripts/compare_latest_frame_with_id.py \
        --id-image ./sample-id.jpg \
        --config ./config/facecompare-params.json \
        --profile video-analyzer \
        --threshold 90 \
        --pretty

The result is printed to stdout as a JSON object. The process exit code
reflects the outcome (see EXIT_CODES below).
"""

from __future__ import print_function

import argparse
import datetime
import json
import os
import sys
import time

import boto3
import botocore.exceptions
from boto3.dynamodb.conditions import Key
from zoneinfo import ZoneInfo


# Config keys that must be present in facecompare-params.json.
REQUIRED_CONFIG_KEYS = [
    "region",
    "ddb_table",
    "ddb_gsi_name",
    "timezone",
    "similarity_threshold",
    "rekognition_api_similarity_threshold",
    "quality_filter",
    "latest_frame_horizon_minutes",
    "max_source_image_bytes",
    "allowed_source_extensions",
]

# Map each reason to a process exit code.
#   0 = matched (similarity >= business threshold)
#   2 = ran fine but similarity below threshold
#   3 = no comparable face (source and/or target)
#   4 = input / config error
#   5 = no usable latest frame
#   6 = AWS-side error
#   1 = any other / unexpected (DEFAULT_EXIT)
EXIT_CODES = {
    "SIMILARITY_ABOVE_THRESHOLD": 0,
    "SIMILARITY_BELOW_THRESHOLD": 2,
    "NO_FACE_IN_SOURCE_OR_TARGET": 3,
    "CONFIG_ERROR": 4,
    "ID_IMAGE_NOT_FOUND": 4,
    "UNSUPPORTED_IMAGE_FORMAT": 4,
    "IMAGE_TOO_LARGE": 4,
    "NO_LATEST_FRAME": 5,
    "NO_RECENT_FRAME": 5,
    "FRAME_METADATA_INVALID": 5,
    "INVALID_S3_OBJECT": 6,
    "ACCESS_DENIED": 6,
    "THROTTLED": 6,
    "AWS_API_ERROR": 6,
}
DEFAULT_EXIT = 1


class CompareError(Exception):
    """Carries a stable reason code (plus optional detail / AWS error code)
    that maps to a result JSON and an exit code."""

    def __init__(self, reason, detail=None, aws_code=None):
        super(CompareError, self).__init__(reason)
        self.reason = reason
        self.detail = detail
        self.aws_code = aws_code


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Compare a face in a local ID image with the latest camera "
                    "frame (S3) using Rekognition CompareFaces."
    )
    parser.add_argument("--id-image", required=True,
                        help="Path to the local ID-card image (.jpg/.jpeg/.png).")
    parser.add_argument("--config", required=True,
                        help="Path to facecompare-params.json.")
    parser.add_argument("--profile", default=None,
                        help="AWS named profile to use (optional).")
    parser.add_argument("--threshold", type=float, default=None,
                        help="Business similarity threshold override, 0-100 "
                             "(defaults to similarity_threshold in config).")
    parser.add_argument("--pretty", action="store_true",
                        help="Pretty-print the JSON output.")
    return parser.parse_args(argv)


def load_config(path):
    try:
        with open(path, "r", encoding="utf-8") as conf_file:
            config = json.loads(conf_file.read())
    except FileNotFoundError:
        raise CompareError("CONFIG_ERROR", detail="config file not found: {}".format(path))
    except (ValueError, OSError) as err:
        raise CompareError("CONFIG_ERROR", detail="cannot read config: {}".format(err))

    missing = [key for key in REQUIRED_CONFIG_KEYS if key not in config]
    if missing:
        raise CompareError("CONFIG_ERROR",
                           detail="missing config keys: {}".format(", ".join(missing)))
    return config


def load_id_image(path, config):
    """Validate the local ID image and return its raw bytes.

    NOTE: the returned bytes must never be logged or printed."""
    if not os.path.isfile(path):
        raise CompareError("ID_IMAGE_NOT_FOUND", detail=path)

    ext = os.path.splitext(path)[1].lower()
    allowed = [str(e).lower() for e in config["allowed_source_extensions"]]
    if ext not in allowed:
        raise CompareError("UNSUPPORTED_IMAGE_FORMAT",
                           detail="extension '{}' not in {}".format(ext, allowed))

    size = os.path.getsize(path)
    max_bytes = int(config["max_source_image_bytes"])
    if size > max_bytes:
        raise CompareError("IMAGE_TOO_LARGE",
                           detail="{} bytes exceeds max {} bytes".format(size, max_bytes))

    with open(path, "rb") as img_file:
        return img_file.read()


def build_session(profile, region):
    if profile:
        return boto3.Session(profile_name=profile, region_name=region)
    return boto3.Session(region_name=region)


def _to_float(value):
    if value is None:
        return None
    return float(value)


def _query_month(table, gsi_name, year_month):
    resp = table.query(
        IndexName=gsi_name,
        KeyConditionExpression=Key("processed_year_month").eq(year_month),
        ScanIndexForward=False,  # most recent processed_timestamp first
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def query_latest_frame(session, config):
    """Return the most recent EnrichedFrame item, or None.

    Queries the current "YYYYMM" partition first; if empty (e.g. just after a
    month rollover) it falls back to the previous month. The current month's
    timestamps are always newer than the previous month's, so current-month
    first is correct."""
    table = session.resource("dynamodb").Table(config["ddb_table"])
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


def compare_faces(session, id_image_bytes, target, config):
    """Call Rekognition CompareFaces and return the best similarity (0-100).

    The API is called with SimilarityThreshold=0.0 on purpose so that nothing is
    filtered out and we can read the real similarity. The business threshold is
    applied by the caller, not the API."""
    rekog = session.client("rekognition")
    try:
        resp = rekog.compare_faces(
            SourceImage={"Bytes": id_image_bytes},
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
            # frame has none -> treat as no comparable face.
            raise CompareError("NO_FACE_IN_SOURCE_OR_TARGET", detail="likely_target")
        # A face exists in the target but did not match the source at all.
        # (Practically unreachable at API threshold 0.0; treated as 0 similarity.)
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
        return CompareError("IMAGE_TOO_LARGE", detail="target image too large: " + message,
                            aws_code=code)
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
                 age_seconds, error):
    return {
        "success": success,
        "matched": matched,
        "similarity": similarity,
        "threshold": threshold,
        "reason": reason,
        "source": source,
        "target": target,
        "age_seconds": age_seconds,
        "error": error,
    }


def run(args):
    # These accumulate as we progress so the error path can report whatever
    # context we already have.
    source = {"id_image_path": args.id_image, "id_image_bytes": None}
    target = None
    age_seconds = None
    threshold = None

    try:
        config = load_config(args.config)
        threshold = float(args.threshold) if args.threshold is not None \
            else float(config["similarity_threshold"])

        id_image_bytes = load_id_image(args.id_image, config)
        # Record the byte length ONLY -- never the bytes themselves.
        source["id_image_bytes"] = len(id_image_bytes)

        try:
            session = build_session(args.profile, config["region"])
        except botocore.exceptions.ProfileNotFound as err:
            raise CompareError("CONFIG_ERROR", detail=str(err))

        now_epoch = time.time()
        item = query_latest_frame(session, config)
        if item is None:
            raise CompareError("NO_LATEST_FRAME",
                               detail="no frame found in current or previous month")

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
                "NO_RECENT_FRAME",
                detail="latest frame age {}s exceeds horizon {}s".format(
                    age_seconds, int(horizon_seconds)))

        similarity = compare_faces(session, id_image_bytes, target, config)
        matched = similarity >= threshold
        reason = "SIMILARITY_ABOVE_THRESHOLD" if matched else "SIMILARITY_BELOW_THRESHOLD"
        return build_result(True, matched, round(similarity, 4), threshold, reason,
                            source, target, age_seconds, None)

    except CompareError as err:
        return build_result(False, False, None, threshold, err.reason, source, target,
                            age_seconds, {"detail": err.detail, "aws_code": err.aws_code})
    except botocore.exceptions.NoCredentialsError:
        return build_result(False, False, None, threshold, "ACCESS_DENIED", source, target,
                            age_seconds, {"detail": "no AWS credentials found", "aws_code": None})
    except botocore.exceptions.BotoCoreError as err:
        return build_result(False, False, None, threshold, "AWS_API_ERROR", source, target,
                            age_seconds, {"detail": str(err), "aws_code": None})


def main(argv=None):
    args = parse_args(argv)
    result = run(args)
    indent = 2 if args.pretty else None
    print(json.dumps(result, indent=indent, ensure_ascii=False))
    sys.exit(EXIT_CODES.get(result["reason"], DEFAULT_EXIT))


if __name__ == "__main__":
    main()
