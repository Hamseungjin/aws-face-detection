# Structured JSON-Lines logging helper for the video capture client.
#
# NOTE: This module is intentionally named "log_util" (NOT "logging") so it never
# shadows the Python standard library "logging" module. The "logging/" directory at
# the project root is used ONLY for log files and must contain no Python files.
#
# Local capture logs are appended to:
#     <project_root>/logging/capture/YYYY/MM/DD/HH.log   (Asia/Seoul time)
#
# All logging is best-effort: any failure here is swallowed so it can never crash
# the capture loop.

import os
import sys
import json
import datetime

# Asia/Seoul timezone: prefer stdlib zoneinfo (Python 3.9+), fall back to pytz.
_KST = None
try:
    from zoneinfo import ZoneInfo
    _KST = ZoneInfo("Asia/Seoul")
except Exception:
    try:
        import pytz
        _KST = pytz.timezone("Asia/Seoul")
    except Exception:
        _KST = None  # last resort: fixed +09:00 offset (see now_kst)

_FIXED_KST = datetime.timezone(datetime.timedelta(hours=9))

# Project root is the parent of the directory containing this file (client/).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CAPTURE_LOG_ROOT = os.path.join(_PROJECT_ROOT, "logging", "capture")


def now_kst():
    '''Return a timezone-aware datetime in Asia/Seoul.'''
    tz = _KST if _KST is not None else _FIXED_KST
    return datetime.datetime.now(tz)


def iso_ts(dt=None):
    '''ISO-8601 timestamp with milliseconds, e.g. 2026-06-17T14:03:22.123+09:00.'''
    if dt is None:
        dt = now_kst()
    # isoformat with milliseconds precision
    return dt.isoformat(timespec="milliseconds")


def _capture_log_path(dt=None):
    '''Build logging/capture/YYYY/MM/DD/HH.log under the project root (Asia/Seoul).
    Creates parent directories. Returns the file path, or None on failure.'''
    if dt is None:
        dt = now_kst()
    try:
        directory = os.path.join(
            _CAPTURE_LOG_ROOT,
            dt.strftime("%Y"),
            dt.strftime("%m"),
            dt.strftime("%d"),
        )
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, dt.strftime("%H") + ".log")
    except Exception as e:
        # Directory creation failed: warn on stderr only, never raise.
        sys.stderr.write("log_util: failed to create capture log dir: {}\n".format(e))
        return None


def log_event(component, event, **fields):
    '''Append one JSON line describing an event to the current hour's capture log.

    Best-effort: serialization or I/O errors are swallowed (logging must never
    break capture). Each call computes the path fresh so hour rollover and
    multiprocessing workers are handled naturally, and appends a single line.
    '''
    try:
        dt = now_kst()
        record = {"timestamp": iso_ts(dt), "component": component, "event": event}
        record.update(fields)
        line = json.dumps(record, default=str, ensure_ascii=False)
    except Exception as e:
        sys.stderr.write("log_util: failed to serialize log event '{}': {}\n".format(event, e))
        return

    path = _capture_log_path(dt)
    if path is None:
        # Could not establish a file; emit to stderr so the data isn't fully lost.
        sys.stderr.write(line + "\n")
        return

    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        sys.stderr.write("log_util: failed to write capture log: {}\n".format(e))
