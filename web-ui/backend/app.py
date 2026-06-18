"""Web UI backend (FastAPI/uvicorn) -- login gate + browser-webcam capture.

WHY a backend (the SPA used to be served by a static http.server):
  * EC2 is headless (no webcam), so the camera must be opened in the user's BROWSER
    via getUserMedia. The browser POSTs JPEG frames here; this backend forwards each
    frame to the Kinesis FrameStream using the EC2 INSTANCE ROLE -- AWS credentials
    never reach the browser.
  * The Kinesis record is byte-compatible with client/video_cap.py
    (pickle.dumps({'ApproximateCaptureTime', 'FrameCount', 'ImageBytes'})), so the
    existing imageprocessor Lambda consumes browser frames unchanged.
  * Login gate: unauthenticated users get only the login screen; the capture API and
    the API-Gateway config (apiBaseUrl + apiKey) require a valid session.

Run locally (localhost is a secure context, so getUserMedia works over http):
    cd web-ui/backend && cp .env.example .env   # fill WEBUI_AUTH_* / WEBUI_SESSION_SECRET
    pip install -r requirements.txt
    AWS_PROFILE=video-analyzer uvicorn app:app --port 8080
    open http://localhost:8080
"""
import base64
import datetime
import pickle
import threading
import time
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

import auth
import config

app = FastAPI(title="Rekognition Video Analyzer - Web UI")

# Signed, HttpOnly session cookie (SameSite=Lax; Secure when served over HTTPS).
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET or "dev-insecure-secret-change-me",
    session_cookie=config.SESSION_COOKIE,
    max_age=config.SESSION_MAX_AGE,
    same_site="lax",
    https_only=config.SESSION_HTTPS_ONLY,
)

ROOT = Path(__file__).resolve().parent.parent  # web-ui/ -- the static SPA lives here

# Kinesis client: same timeouts/retries as client/video_cap.py.
_KINESIS_CONFIG = BotoConfig(
    connect_timeout=3, read_timeout=8,
    retries={"max_attempts": 3, "mode": "standard"}, tcp_keepalive=True,
)
_kinesis = boto3.client("kinesis", region_name=config.REGION, config=_KINESIS_CONFIG)

_gw_cache = {"data": None, "at": 0.0}
_gw_lock = threading.Lock()


# --- auth helpers ----------------------------------------------------------
def _current_user(request):
    return request.session.get("user")


def require_user(request):
    if not _current_user(request):
        raise HTTPException(status_code=401, detail="authentication required")


# --- request models --------------------------------------------------------
class LoginIn(BaseModel):
    username: str
    password: str


class FrameIn(BaseModel):
    imageBase64: str
    frameCount: int = 0


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
    return {"ok": True, "user": body.username}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    user = _current_user(request)
    return {"authenticated": bool(user), "user": user}


# --- capture route (auth required) -----------------------------------------
@app.post("/api/capture-frame")
def capture_frame(body: FrameIn, request: Request):
    require_user(request)
    try:
        img_bytes = bytearray(base64.b64decode(body.imageBase64))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid base64 image")
    if not img_bytes:
        raise HTTPException(status_code=400, detail="empty image")

    # EXACT record shape from client/video_cap.py so imageprocessor is unchanged.
    frame_package = {
        "ApproximateCaptureTime": datetime.datetime.now(datetime.timezone.utc).timestamp(),
        "FrameCount": int(body.frameCount),
        "ImageBytes": img_bytes,
    }
    try:
        resp = _kinesis.put_record(
            StreamName=config.KINESIS_STREAM,
            Data=pickle.dumps(frame_package),
            PartitionKey=config.KINESIS_PARTITION_KEY,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail="kinesis put failed: %s" % e)
    return {"ok": True, "sequenceNumber": resp.get("SequenceNumber"), "shardId": resp.get("ShardId")}


# --- API Gateway config for the frame viewer (auth required) ---------------
# Replaces the old static apigw.js so the apiKey is NEVER served to anonymous users.
# Computed from the data stack via the instance role (cfn DescribeStackResource +
# apigateway GET) and cached 5 min.
def _load_gateway_config():
    now = time.time()
    if _gw_cache["data"] is not None and (now - _gw_cache["at"]) < 300:
        return _gw_cache["data"]
    with _gw_lock:
        now = time.time()
        if _gw_cache["data"] is not None and (now - _gw_cache["at"]) < 300:
            return _gw_cache["data"]
        cfn = boto3.client("cloudformation", region_name=config.REGION)
        apigw = boto3.client("apigateway", region_name=config.REGION)
        rest_id = cfn.describe_stack_resource(
            StackName=config.DATA_STACK, LogicalResourceId=config.DATA_REST_API_LOGICAL_ID,
        )["StackResourceDetail"]["PhysicalResourceId"]
        key_id = cfn.describe_stack_resource(
            StackName=config.DATA_STACK, LogicalResourceId=config.DATA_API_KEY_LOGICAL_ID,
        )["StackResourceDetail"]["PhysicalResourceId"]
        api_key = apigw.get_api_key(apiKey=key_id, includeValue=True)["value"]
        base = "https://%s.execute-api.%s.amazonaws.com/%s" % (
            rest_id, config.REGION, config.DATA_API_STAGE,
        )
        _gw_cache["data"] = {"apiBaseUrl": base, "apiKey": api_key}
        _gw_cache["at"] = time.time()
        return _gw_cache["data"]


@app.get("/api/config")
def api_config(request: Request):
    require_user(request)
    try:
        return _load_gateway_config()
    except Exception as e:
        # Frame viewing is optional; capture still works without it.
        raise HTTPException(status_code=503, detail="config unavailable: %s" % e)


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
    return FileResponse(str(ROOT / "index.html"))


@app.get("/{asset}")
def root_asset(asset: str):
    if asset in _ROOT_ASSETS and (ROOT / asset).is_file():
        return FileResponse(str(ROOT / asset))
    raise HTTPException(status_code=404, detail="not found")
