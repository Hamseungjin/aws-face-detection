"""Live KPI aggregation from AWS for the dashboard backend.

Sources:
  - CloudWatch Logs Insights  -> imageprocessor / framefetcher / facecompare counts + recent errors
  - CloudWatch GetMetricData  -> Kinesis IteratorAge / IncomingRecords / PutRecord latency
  - DynamoDB Query (GSI)      -> recent frame count + latest_frame_age_seconds

Design:
  - collect() NEVER raises. Each section is wrapped so one failure becomes
    {"error": "..."} while the rest of the payload is returned (partial failure).
  - COST: CloudWatch Logs Insights bills per GB scanned. The app-layer TTL cache
    (config.REFRESH_INTERVAL_SECONDS, 60s) and the lookback window
    (config.LOOKBACK_HOURS, default 1h) are the guardrails. Do NOT widen the
    lookback or remove the cache without weighing Logs Insights cost."""
import datetime
import decimal
import time
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Key

import config

_FACECOMPARE_OK = {"SIMILARITY_ABOVE_THRESHOLD", "SIMILARITY_BELOW_THRESHOLD"}
_FACECOMPARE_ERR = [
    "BAD_REQUEST", "UNSUPPORTED_IMAGE_FORMAT", "IMAGE_TOO_LARGE", "INVALID_IMAGE",
    "CONFIG_ERROR", "INVALID_S3_OBJECT", "ACCESS_DENIED", "THROTTLED", "AWS_API_ERROR",
]
_DDB_QUERY_LIMIT = 300  # recent_count capped at this for the demo (flagged in result)


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def collect():
    """Aggregate all KPIs. Never raises; per-section failures become {"error":...}."""
    now = time.time()
    out = {
        "generated_at": _iso(now),
        "source": "live",
        "window_hours": config.LOOKBACK_HOURS,
        "refresh_interval_seconds": config.REFRESH_INTERVAL_SECONDS,
        "kpis": {},
        "recent_errors": [],
    }

    region = config.REGION
    logs = boto3.client("logs", region_name=region)
    cw = boto3.client("cloudwatch", region_name=region)
    ddb = boto3.resource("dynamodb", region_name=region)

    end_s = now
    start_s = now - config.LOOKBACK_HOURS * 3600.0

    ip = config.LOG_GROUPS["imageprocessor"]
    ff = config.LOG_GROUPS["framefetcher"]
    fc = config.LOG_GROUPS["facecompare"]

    err_reasons = '","'.join(_FACECOMPARE_ERR)
    specs = {
        "imageprocessor": ([ip],
            'filter component = "imageprocessor" '
            '| stats count(*) as events, sum(succeeded) as succeeded, sum(failed) as failed by event'),
        "framefetcher": ([ff],
            'filter component = "framefetcher" and event = "fetch_complete" '
            '| stats count(*) as count'),
        "facecompare": ([fc],
            'filter component = "facecompare" and event = "compare_complete" '
            '| stats count(*) as count by reason'),
        "errors": ([ip, ff, fc],
            'fields @timestamp, component, event, error, reason '
            '| filter (ispresent(error) and error != "") '
            'or (component = "facecompare" and event = "compare_complete" and reason in ["' + err_reasons + '"]) '
            '| sort @timestamp desc | limit 20'),
    }

    raw = _run_insights(logs, specs, start_s, end_s)

    out["kpis"]["imageprocessor"] = _guard(raw["imageprocessor"], _parse_ip_success)
    out["kpis"]["rekognition_detect_labels"] = _guard(raw["imageprocessor"], _parse_rekog)
    out["kpis"]["s3_put"] = _guard(raw["imageprocessor"], _parse_s3)
    out["kpis"]["dynamodb_put"] = _guard(raw["imageprocessor"], _parse_ddb_put)
    out["kpis"]["enrichedframe"] = _guard(raw["framefetcher"], _parse_ff)
    out["kpis"]["facecompare"] = _guard(raw["facecompare"], _parse_fc)

    errs = _guard(raw["errors"], _parse_errors)
    if isinstance(errs, dict):  # {"error": ...}
        out["recent_errors"] = []
        out["recent_errors_error"] = errs["error"]
    else:
        out["recent_errors"] = errs

    out["kpis"]["kinesis"] = _safe(lambda: _kinesis(cw, start_s, end_s))
    out["kpis"]["frames"] = _safe(lambda: _frames(ddb, now))

    return out


# ---- partial-failure helpers ----------------------------------------------
def _guard(raw, fn):
    if isinstance(raw, Exception):
        return {"error": str(raw)}
    try:
        return fn(raw)
    except Exception as e:
        return {"error": str(e)}


def _safe(fn):
    try:
        return fn()
    except Exception as e:
        return {"error": str(e)}


# ---- Logs Insights orchestration (start all, then poll all) ----------------
def _run_insights(logs, specs, start_s, end_s):
    started, raw = {}, {}
    for key, (groups, query) in specs.items():
        try:
            r = logs.start_query(logGroupNames=groups, startTime=int(start_s),
                                 endTime=int(end_s), queryString=query, limit=1000)
            started[key] = r["queryId"]
        except Exception as e:
            raw[key] = e
    for key, qid in started.items():
        try:
            raw[key] = _poll(logs, qid)
        except Exception as e:
            raw[key] = e
    return raw


def _poll(logs, query_id, timeout_s=20):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = logs.get_query_results(queryId=query_id)
        st = r["status"]
        if st == "Complete":
            return [{c["field"]: c["value"] for c in row} for row in r["results"]]
        if st in ("Failed", "Cancelled", "Timeout"):
            raise RuntimeError("Logs Insights status=%s" % st)
        time.sleep(0.5)
    try:
        logs.stop_query(queryId=query_id)
    except Exception:
        pass
    raise TimeoutError("Logs Insights query timed out")


# ---- parsers ---------------------------------------------------------------
def _count_events(rows, names):
    return sum(_num(r.get("events")) for r in rows if r.get("event") in names)


def _parse_ip_success(rows):
    s = f = 0
    for r in rows:
        if r.get("event") == "handler_complete":
            s += _num(r.get("succeeded"))
            f += _num(r.get("failed"))
    return {"succeeded": s, "failed": f}


def _parse_rekog(rows):
    return {"count": _count_events(rows, {"rekognition_detect_labels_success",
                                          "rekognition_detect_labels_failed"})}


def _parse_s3(rows):
    return {"success": _count_events(rows, {"s3_put_success"}),
            "failed": _count_events(rows, {"s3_put_failed"})}


def _parse_ddb_put(rows):
    return {"success": _count_events(rows, {"dynamodb_put_success"}),
            "failed": _count_events(rows, {"dynamodb_put_failed"})}


def _parse_ff(rows):
    return {"count": sum(_num(r.get("count")) for r in rows)}


def _parse_fc(rows):
    total = ok = bad = 0
    for r in rows:
        c = _num(r.get("count"))
        total += c
        if r.get("reason") in _FACECOMPARE_OK:
            ok += c
        else:
            bad += c
    return {"count": total, "success": ok, "failed": bad}


def _parse_errors(rows):
    return [{"timestamp": r.get("@timestamp"), "component": r.get("component"),
             "event": r.get("event"), "error": r.get("error") or r.get("reason")}
            for r in rows]


# ---- CloudWatch metrics (Kinesis) -----------------------------------------
def _kinesis(cw, start_s, end_s):
    start_dt = datetime.datetime.fromtimestamp(start_s, datetime.timezone.utc)
    end_dt = datetime.datetime.fromtimestamp(end_s, datetime.timezone.utc)
    dim = [{"Name": "StreamName", "Value": config.KINESIS_STREAM}]

    def mq(mid, name, stat):
        return {"Id": mid, "MetricStat": {
            "Metric": {"Namespace": "AWS/Kinesis", "MetricName": name, "Dimensions": dim},
            "Period": 300, "Stat": stat}}

    r = cw.get_metric_data(MetricDataQueries=[
        mq("iterage", "GetRecords.IteratorAgeMilliseconds", "Maximum"),
        mq("incoming", "IncomingRecords", "Sum"),
        mq("putlat", "PutRecord.Latency", "Average"),
    ], StartTime=start_dt, EndTime=end_dt)

    vals = {m["Id"]: m.get("Values", []) for m in r["MetricDataResults"]}
    iterage, incoming, putlat = vals.get("iterage") or [], vals.get("incoming") or [], vals.get("putlat") or []
    return {
        "iterator_age_ms_max": round(max(iterage), 1) if iterage else None,
        "incoming_records": int(sum(incoming)) if incoming else None,
        "put_latency_ms_avg": round(sum(putlat) / len(putlat), 1) if putlat else None,
    }


# ---- DynamoDB (latest frame age + recent count) ---------------------------
def _year_months(now_ts):
    tz = ZoneInfo(config.PIPELINE_TZ)  # processed_year_month is written in Asia/Seoul
    dt = datetime.datetime.fromtimestamp(now_ts, tz)
    cur = dt.strftime("%Y%m")
    prev = (dt.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y%m")
    return [cur] if cur == prev else [cur, prev]


def _frames(ddb, now_ts):
    table = ddb.Table(config.DDB_TABLE)
    horizon = decimal.Decimal(str(now_ts - config.LOOKBACK_HOURS * 3600.0))
    recent_count, latest, capped = 0, None, False
    for ym in _year_months(now_ts):
        resp = table.query(
            IndexName=config.DDB_GSI,
            KeyConditionExpression=Key("processed_year_month").eq(ym)
            & Key("processed_timestamp").gt(horizon),
            ScanIndexForward=False,
            Limit=_DDB_QUERY_LIMIT,
        )
        items = resp.get("Items", [])
        recent_count += len(items)
        if len(items) >= _DDB_QUERY_LIMIT:
            capped = True
        for it in items:
            cap = it.get("approx_capture_timestamp")
            if cap is None:
                cap = it.get("processed_timestamp")
            if cap is not None:
                capf = float(cap)
                if latest is None or capf > latest:
                    latest = capf
    res = {"recent_count": recent_count,
           "latest_frame_age_seconds": round(now_ts - latest, 1) if latest is not None else None}
    if capped:
        res["recent_count_capped_at"] = _DDB_QUERY_LIMIT
    return res
