"""Configuration for the KPI dashboard backend.

PR 3 only reads KPI_PORT / REFRESH interval. The AWS-related values (region, log
groups, DynamoDB table/GSI) are defined here now so PR 4 can wire real queries
without touching the service layout. All overridable via environment variables
(set by /etc/kpi-dashboard.env on the instance)."""
import os

REGION = (os.getenv("AWS_REGION")
          or os.getenv("AWS_DEFAULT_REGION")
          or "ap-northeast-2")

DATA_STACK = os.getenv("DATA_STACK", "video-analyzer-stack")

KPI_PORT = int(os.getenv("KPI_PORT", "8000"))
REFRESH_INTERVAL_SECONDS = int(os.getenv("KPI_REFRESH_SECONDS", "60"))
LOOKBACK_HOURS = float(os.getenv("KPI_LOOKBACK_HOURS", "1"))

# Forward-looking (used in PR 4):
LOG_GROUPS = {
    "imageprocessor": os.getenv("LOG_GROUP_IMAGEPROCESSOR", "/aws/lambda/imageprocessor"),
    "framefetcher":   os.getenv("LOG_GROUP_FRAMEFETCHER",   "/aws/lambda/framefetcher"),
    "facecompare":    os.getenv("LOG_GROUP_FACECOMPARE",    "/aws/lambda/facecompare"),
}
DDB_TABLE = os.getenv("DDB_TABLE", "EnrichedFrame")
DDB_GSI = os.getenv("DDB_GSI", "processed_year_month-processed_timestamp-index")
KINESIS_STREAM = os.getenv("KINESIS_STREAM", "FrameStream")
PIPELINE_TZ = os.getenv("PIPELINE_TZ", "Asia/Seoul")  # processed_year_month is written in this tz
