# Structured JSON-Lines logging helper for the ImageProcessor Lambda.
#
# NOTE: This module is intentionally named "log_util" (NOT "logging") so it never
# shadows the Python standard library "logging" module.
#
# Logging strategy:
#   - CloudWatch (always): each event is printed as one JSON line. AWS Lambda
#     forwards stdout to the /aws/lambda/imageprocessor log group automatically.
#   - S3 (optional): when ENABLE_S3_LOGGING is truthy, all of an invocation's log
#     lines are buffered and written ONCE, at the end of the invocation, to a
#     per-invocation object so nothing is ever overwritten:
#         logging/imageprocessor/YYYY/MM/DD/HH/<request_id>.log   (Asia/Seoul)
#
# Privacy: callers must never pass raw image bytes, base64 payloads, presigned
# URLs, or face data. Only S3 keys, frame ids, byte sizes and label summaries.
#
# All logging is best-effort: any failure here is swallowed (and noted on
# CloudWatch) so it can never break frame processing.

from __future__ import print_function
import os
import json
import datetime

# Asia/Seoul timezone: prefer stdlib zoneinfo (Python 3.9+, available on the
# python3.12 Lambda runtime), fall back to the bundled pytz, then a fixed offset.
_KST = None
try:
    from zoneinfo import ZoneInfo
    _KST = ZoneInfo("Asia/Seoul")
except Exception:
    try:
        import pytz
        _KST = pytz.timezone("Asia/Seoul")
    except Exception:
        _KST = None

_FIXED_KST = datetime.timezone(datetime.timedelta(hours=9))


def now_kst():
    '''Return a timezone-aware datetime in Asia/Seoul.'''
    tz = _KST if _KST is not None else _FIXED_KST
    return datetime.datetime.now(tz)


def iso_ts(dt=None):
    '''ISO-8601 timestamp with milliseconds, e.g. 2026-06-17T14:05:10.456+09:00.'''
    if dt is None:
        dt = now_kst()
    return dt.isoformat(timespec="milliseconds")


def s3_logging_enabled():
    '''True if S3 logging is switched on via the ENABLE_S3_LOGGING env var.'''
    return os.environ.get("ENABLE_S3_LOGGING", "false").strip().lower() in ("1", "true", "yes", "on")


def s3_log_key(request_id, dt=None):
    '''Per-invocation S3 key: logging/imageprocessor/YYYY/MM/DD/HH/<request_id>.log (KST).'''
    if dt is None:
        dt = now_kst()
    return "logging/imageprocessor/{}/{}/{}/{}/{}.log".format(
        dt.strftime("%Y"), dt.strftime("%m"), dt.strftime("%d"), dt.strftime("%H"),
        request_id or "unknown-request")


def log_event(component, event, buffer=None, **fields):
    '''Emit one JSON line to CloudWatch (via print) and, if a buffer list is
    given, append the same line to it for later S3 flush. Best-effort.'''
    try:
        record = {"timestamp": iso_ts(), "component": component, "event": event}
        record.update(fields)
        line = json.dumps(record, default=str, ensure_ascii=False)
    except Exception as e:
        print("log_util: failed to serialize log event '{}': {}".format(event, e))
        return None

    print(line)
    if buffer is not None:
        try:
            buffer.append(line)
        except Exception:
            pass
    return line


def flush_to_s3(s3_client, buffer, request_id, config=None):
    '''Write the buffered log lines to S3 as a single per-invocation object.

    Only runs when ENABLE_S3_LOGGING is truthy. The target bucket is
    LOG_BUCKET_NAME, falling back to the image bucket (config["s3_bucket"]) so
    the existing frames bucket can be reused with no new IAM permission.
    Best-effort: failures are logged to CloudWatch but never raised.
    '''
    if not s3_logging_enabled():
        return
    if not buffer:
        return

    bucket = os.environ.get("LOG_BUCKET_NAME", "").strip()
    if not bucket and config:
        bucket = config.get("s3_bucket", "")

    if not bucket:
        print("log_util: S3 logging enabled but no LOG_BUCKET_NAME (and no config s3_bucket); skipping.")
        return

    key = s3_log_key(request_id)
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=("\n".join(buffer) + "\n").encode("utf-8")
        )
        print("log_util: flushed {} log lines to s3://{}/{}".format(len(buffer), bucket, key))
    except Exception as e:
        print("log_util: failed to flush logs to s3://{}/{}: {}".format(bucket, key, e))
