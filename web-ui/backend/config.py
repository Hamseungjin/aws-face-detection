"""Web UI backend configuration (12-factor: everything via environment).

Mirrors kpi-dashboard/backend/config.py: values come from os.environ, which on EC2
is populated from /etc/webui.env (written by webui-bootstrap.sh from SSM Parameter
Store), and locally from web-ui/backend/.env (loaded below). NO secrets are
hardcoded here.
"""
import os

# Local dev convenience: load web-ui/backend/.env into the environment if present.
#   override=False  -> the EC2 systemd EnvironmentFile (/etc/webui.env) ALWAYS wins
#                      (and publishapps excludes .env, so EC2 has none -> no-op there).
#   interpolate=False -> a '$' inside WEBUI_AUTH_PASSWORD_HASH (pbkdf2_sha256$...$...$...)
#                      is never mangled by dotenv variable expansion.
try:
    from dotenv import load_dotenv
    load_dotenv(override=False, interpolate=False)
except Exception:
    pass


def _bool(name, default="false"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# AWS / Kinesis ------------------------------------------------------------
REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-northeast-2"
WEBUI_PORT = int(os.getenv("WEBUI_PORT", "8080"))

# Kinesis target -- MUST match the data stack stream (same as client/video_cap.py).
KINESIS_STREAM = os.getenv("KINESIS_STREAM", "FrameStream")
KINESIS_PARTITION_KEY = os.getenv("KINESIS_PARTITION_KEY", "partitionkey")

# Auth (injected, never hardcoded) -----------------------------------------
#   AUTH_USERNAME       : plain username
#   AUTH_PASSWORD_HASH  : PBKDF2 string produced by auth.hash_password (NOT the password)
#   SESSION_SECRET      : random secret used to sign the session cookie
AUTH_USERNAME = os.getenv("WEBUI_AUTH_USERNAME", "")
AUTH_PASSWORD_HASH = os.getenv("WEBUI_AUTH_PASSWORD_HASH", "")
SESSION_SECRET = os.getenv("WEBUI_SESSION_SECRET", "")

# Session cookie -----------------------------------------------------------
SESSION_COOKIE = os.getenv("WEBUI_SESSION_COOKIE", "webui_session")
SESSION_MAX_AGE = int(os.getenv("WEBUI_SESSION_MAX_AGE", str(8 * 3600)))  # 8h
# Secure cookie requires HTTPS; on EC2 we serve self-signed HTTPS so default true,
# but for local http://localhost dev set WEBUI_SESSION_HTTPS_ONLY=false.
SESSION_HTTPS_ONLY = _bool("WEBUI_SESSION_HTTPS_ONLY", "false")

# Data stack lookup for the frame viewer (/api/config -> apiBaseUrl + apiKey) ----
DATA_STACK = os.getenv("DATA_STACK", "video-analyzer-stack")
DATA_API_STAGE = os.getenv("DATA_API_STAGE", "development")
DATA_REST_API_LOGICAL_ID = os.getenv("DATA_REST_API_LOGICAL_ID", "VidAnalyzerRestApi")
DATA_API_KEY_LOGICAL_ID = os.getenv("DATA_API_KEY_LOGICAL_ID", "VidAnalyzerApiKey")

# imageprocessor Lambda whose ENABLE_DETECT_LABELS env var the web UI toggles at
# runtime. FunctionName is hardcoded in the data stack (aws-infra-cfn.yaml), so the
# physical name is deterministic -- no CloudFormation lookup needed.
IMAGEPROCESSOR_FUNCTION_NAME = os.getenv("IMAGEPROCESSOR_FUNCTION_NAME", "imageprocessor")
