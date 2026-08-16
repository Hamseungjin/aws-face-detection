# 로컬 PC 실행 가이드 (webui + KPI Dashboard + capture client)

EC2를 쓰지 않을 때(또는 EC2 운영 시간대가 아닐 때) **내 로컬 PC**에서 webui와 KPI Dashboard를
실행하는 방법입니다. 기존 AWS 서버리스 백엔드(Kinesis · Lambda · Rekognition · S3 · DynamoDB ·
API Gateway)는 **이미 AWS에 배포된 `video-analyzer-stack`을 그대로** 사용합니다. 로컬 PC는 화면
서버 + 개발 실행 환경 역할만 합니다. 도메인 / EIP / EC2는 로컬 실행에서 사용하지 않습니다.

리전: **ap-northeast-2** · 데이터 스택: **video-analyzer-stack**

---

## 1. 로컬에서 실행 가능한 구성요소

| 구분 | 로컬 실행 | 실행 방법 요약 |
|---|---|---|
| webui (FastAPI) | ✅ | `web-ui/backend`에서 `uvicorn app:app --port 8080` (http://localhost:8080) |
| KPI Dashboard | ✅ | `kpi-dashboard/backend`에서 `uvicorn app:app` (http://localhost:8000) |
| video capture client | ✅ | `pynt videocapture[60]` (로컬 카메라 → Kinesis) |
| **AWS 백엔드 (이미 배포돼 있어야 함)** | ❌ 로컬 불가 | `video-analyzer-stack`: Kinesis · Lambda · Rekognition · S3 · DynamoDB · API Gateway |

> **webui 백엔드(FastAPI)** 가 브라우저 로그인·세션·`POST /api/capture-frame`(Kinesis PutRecord)·
> `GET/POST /api/detect-labels`(imageprocessor Lambda 설정)·`GET /api/config`(API Gateway 조회)를
> 처리합니다. AWS 자격 증명은 **브라우저에 절대 내려가지 않고** 백엔드의 boto3 기본 자격 증명 체인만
> 사용합니다. KPI Dashboard는 boto3로 CloudWatch/DynamoDB를 읽습니다. capture client는 로컬 카메라
> 프레임을 Kinesis로 보냅니다.

### 배포(EC2) vs 로컬 인증 차이

| 환경 | 자격 증명 공급자 | 비고 |
|---|---|---|
| 배포 webui EC2 | **EC2 instance profile / IAM role** | `/etc/webui.env`에 키 없음. boto3가 IMDS 역할 사용 |
| 로컬 PC / 개발 호스트 | **boto3 기본 체인** | env 키, `~/.aws/credentials`, 또는 `AWS_PROFILE` |
| 이 저장소의 테스트 | 더미 키 + `AWS_EC2_METADATA_DISABLED=true` | AWS 호출 없음 |

로컬에 자격 증명이 없으면 `NoCredentialsError` → HTTP 502
(`… failed: AWS credentials unavailable`). EC2 역할이 있어도 **데이터 스택이 삭제/미배포**이면
`ResourceNotFoundException` → HTTP 502 (`… failed: AWS resource not found`).

---

## 2. 사전 조건

- **Python 3.11 또는 3.12 권장** (최소 3.9 — `zoneinfo` 사용). Lambda 런타임은 3.12.
- **pip + venv**.
- **AWS CLI** (권장 — 자격증명 확인/디버깅용).
- **AWS 자격증명** (로컬): boto3 기본 체인. **소스/프론트/`.env`에 액세스 키를 넣지 마세요.**
  - 확인: `aws sts get-caller-identity` (또는 `AWS_PROFILE=<name> aws sts get-caller-identity`)
  - 프로필: `export AWS_PROFILE=<프로필명>`
  - shared files: `~/.aws/credentials` + `~/.aws/config`
- **리전**: `AWS_DEFAULT_REGION=ap-northeast-2` (또는 `AWS_REGION`)
- **`video-analyzer-stack`이 ap-northeast-2에 CREATE_COMPLETE/UPDATE_COMPLETE 여야 함** (필수).
  삭제(`DELETE_COMPLETE`)되거나 없으면:
  - Kinesis 스트림 `FrameStream` 없음 → `/api/capture-frame` 502
  - Lambda `imageprocessor` 없음 → `/api/detect-labels` 502
  - API Gateway 조회 실패 → `/api/config` 503
  재생성: `pynt packagelambda` → `pynt deploylambda` → `pynt createstack` → `pynt stackstatus`
- **webui 로컬 설정**: `cd web-ui/backend && cp .env.example .env` 후 `WEBUI_AUTH_*` /
  `WEBUI_SESSION_SECRET`만 채움 (AWS 키 넣지 않음). 자세한 non-secret 키는 `.env.example` 참고.
- **로컬 IAM 권한** (admin이면 전부 충족):

  | 구성요소 | 필요한 최소 권한 |
  |---|---|
  | webui FastAPI `POST /api/capture-frame` | `kinesis:PutRecord` on `stream/FrameStream` |
  | webui FastAPI `GET/POST /api/detect-labels` | `lambda:GetFunctionConfiguration`, `lambda:UpdateFunctionConfiguration` on `function:imageprocessor` |
  | webui FastAPI `GET /api/config` | `cloudformation:DescribeStackResource` on data stack, `apigateway:GET` on API keys |
  | capture client | `kinesis:PutRecord` |
  | KPI Dashboard | `logs:StartQuery`/`GetQueryResults`/`StopQuery`, `cloudwatch:GetMetricData`, `dynamodb:Query`/`DescribeTable` |
  | 브라우저 → API GW (프레임 뷰어) | (IAM 아님, `/api/config`이 내려준 API Key) |

> 루트 프로젝트(capture/`pynt`)는 `pynt`, `boto3`, `opencv-python`, `pytz`가 설치된 Python 환경이
> 필요합니다. webui·KPI 백엔드는 각각 별도 venv를 권장합니다.

---

## 3. webui 로컬 실행 (FastAPI)

```bash
cd web-ui/backend
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
# requirements.txt includes opencv-python-headless for the kiosk Quality Gate.
# Do not also install opencv-python or opencv-contrib-python in this venv.
cp .env.example .env
# .env 에 WEBUI_AUTH_USERNAME / WEBUI_AUTH_PASSWORD_HASH / WEBUI_SESSION_SECRET 설정
#   비밀번호 해시: python auth.py 'your-password'
#   세션 시크릿:  python -c "import secrets; print(secrets.token_urlsafe(48))"

export AWS_PROFILE=default                 # 로컬 프로필명으로 변경
export AWS_DEFAULT_REGION=ap-northeast-2
aws sts get-caller-identity                # 실패하면 capture/detect-labels 도 실패

uvicorn app:app --host 127.0.0.1 --port 8080
```

→ 브라우저 **http://localhost:8080** (운영자 UI), **http://localhost:8080/kiosk** (키오스크 UI)

- 로그인 후 `GET /api/detect-labels`, 카메라 캡처 시 `POST /api/capture-frame`이 백엔드 → AWS를 호출.
- 프레임 뷰어/ face-compare 는 로그인 후 `GET /api/config`이 API Gateway URL·Key를 내려준 뒤
  브라우저가 API GW를 직접 호출합니다 (키는 익명 사용자에게 제공되지 않음).
- health: `curl http://127.0.0.1:8080/healthz` → `{"status":"ok"}`
- 포트 변경: `uvicorn app:app --port 9090`

> 구버전 정적 서버(`pynt webui` / `pynt webuiserver` + `apigw.js`) 경로는 더 이상 기본 로컬
> 실행 방식이 아닙니다. 운영자/키오스크 캡처·DetectLabels 토글은 FastAPI 백엔드가 필요합니다.

---

## 4. KPI Dashboard 로컬 실행

- 반드시 `kpi-dashboard/backend`에서 실행해야 `app:app` import와 정적 프론트 경로(`../frontend`)가 맞습니다.
- 의존성: `kpi-dashboard/backend/requirements.txt` = `fastapi`, `uvicorn[standard]`, `boto3`.
- 환경변수:
  - `AWS_DEFAULT_REGION=ap-northeast-2`
  - `KPI_PORT=8000`
  - `KPI_LOOKBACK_HOURS=1` (Logs Insights 비용 가드레일 — 유지 권장)
  - (선택) `DATA_STACK=video-analyzer-stack` — 현재 KPI 백엔드는 직접 사용하지 않고 로그그룹/테이블/
    스트림을 이름 기본값(`/aws/lambda/*`, `EnrichedFrame`, `FrameStream`)으로 조회합니다. 넣어도 무해.
- 실행:
  ```
  uvicorn app:app --host 127.0.0.1 --port 8000
  ```
  → 브라우저 **http://localhost:8000**
- health check: **http://localhost:8000/healthz** → `{"status":"ok"}`
- KPI API: **http://localhost:8000/api/kpis** → 최상위 **`"source":"live"`** 확인(프론트 배지 LIVE).
- 권한 부족 시: 전체 500이 아니라 **섹션별 partial failure** — 해당 카드만
  `{"error":"...AccessDenied... not authorized to perform: logs:StartQuery ..."}`(또는
  `dynamodb:Query`, `cloudwatch:GetMetricData`)로 내려오고 나머지는 정상. 프론트는 그 카드에
  "error"(값에 마우스를 올리면 메시지)로 표시.

---

## 5. video capture client 로컬 실행

- 실행:
  - `pynt videocapture[60]` — 60프레임마다 1장 캡처(비용 친화 기본값)
  - `pynt videocapture[60,300]` — 300초 후 자동 종료(비용 안전)
  - 또는 `cd client && python video_cap.py 60`
- config / 권한: `client/video_cap.py`에 `STREAM_NAME="FrameStream"`가 하드코딩, region은 boto3 세션.
  기본 `enable_rekog=False`이므로 **`kinesis:PutRecord` 권한만** 필요.
- 의존성: `opencv-python`(cv2), `boto3`, `pytz` (+ numpy).
- capture_rate / fps / stream: 1번째 인자 = capture_rate(기본 60), 소스 fps는 카메라에서 읽음(없으면
  30 가정), 스트림 = `FrameStream`.
- 실행 중 확인 항목(구조화 로그는 **stdout이 아니라** `logging/capture/YYYY/MM/DD/HH.log`(KST)의
  `kinesis_put_success` JSON 라인):
  - `encode_ms` (JPEG 인코딩 시간)
  - `put_record_ms` (Kinesis PutRecord 시간)
  - `total_ms` (프레임 처리 총 시간)
  - `retry_attempts` (botocore 내부 재시도 횟수 — 0이 아니면 지연이 백오프/재시도 때문)
  - (+ `jpeg_bytes`, `http_status`)
  - 콘솔에는 `[cost] ...` 요약과 `print(response)`만 표시됩니다.
- 종료: 미리보기 창에서 **`q`** 키, 또는 `max_seconds` 자동 종료.

---

## 6. Windows PowerShell — 전체 실행 순서

**터미널 1 — webui (FastAPI)**
```powershell
cd C:\Users\hsjki\IdeaProjects\amazon-rekognition-video-analyzer-master\web-ui\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# .env 준비 후:
$env:AWS_PROFILE="default"
$env:AWS_DEFAULT_REGION="ap-northeast-2"
aws sts get-caller-identity
uvicorn app:app --host 127.0.0.1 --port 8080   # http://localhost:8080
```

**터미널 2 — KPI Dashboard**
```powershell
cd C:\Users\hsjki\IdeaProjects\amazon-rekognition-video-analyzer-master\kpi-dashboard\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:AWS_PROFILE="default"
$env:AWS_DEFAULT_REGION="ap-northeast-2"
$env:DATA_STACK="video-analyzer-stack"
$env:KPI_PORT="8000"
$env:KPI_LOOKBACK_HOURS="1"
uvicorn app:app --host 127.0.0.1 --port 8000   # http://localhost:8000
```

**터미널 3 — video capture** (루트 프로젝트 env)
```powershell
cd C:\Users\hsjki\IdeaProjects\amazon-rekognition-video-analyzer-master
$env:AWS_PROFILE="default"
$env:AWS_DEFAULT_REGION="ap-northeast-2"
pynt videocapture[60]                 # 종료: 미리보기 창에서 'q'
# 비용 안전: pynt videocapture[60,300]   (300초 후 자동 종료)
```

> PowerShell에서 환경변수는 `$env:NAME="값"` 형식(현재 세션에만 적용). 영구 설정은 `setx`.

---

## 7. macOS / Linux — 전체 실행 순서

```bash
# 터미널 1 — webui (FastAPI)
cd ~/.../amazon-rekognition-video-analyzer-master/web-ui/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# .env 준비 후:
export AWS_PROFILE=default
export AWS_DEFAULT_REGION=ap-northeast-2
aws sts get-caller-identity
uvicorn app:app --host 127.0.0.1 --port 8080   # http://localhost:8080

# 터미널 2 — KPI Dashboard
cd ~/.../amazon-rekognition-video-analyzer-master/kpi-dashboard/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export AWS_PROFILE=default
export AWS_DEFAULT_REGION=ap-northeast-2
export DATA_STACK=video-analyzer-stack
export KPI_PORT=8000
export KPI_LOOKBACK_HOURS=1
uvicorn app:app --host 127.0.0.1 --port 8000   # http://localhost:8000

# 터미널 3 — video capture
cd ~/.../amazon-rekognition-video-analyzer-master
export AWS_PROFILE=default
export AWS_DEFAULT_REGION=ap-northeast-2
pynt videocapture[60]                 # 종료: 'q'
```

> Linux는 카메라(`/dev/video0`) 접근 권한과 GUI(`cv2.imshow`) 환경이 필요합니다. 헤드리스 서버에서는
> 미리보기 창이 뜨지 않으므로 `pynt videocapture[60,300]`처럼 자동 종료로 운용하세요.

---

## 8. 로컬 실행 시 비용 주의

- **EC2 / EIP 비용은 없음** (로컬 실행).
- 다만 AWS를 호출하면 과금될 수 있음: **API Gateway, Lambda, Rekognition, Kinesis, DynamoDB, S3,
  CloudWatch Logs Insights**.
- **KPI Dashboard**는 CloudWatch Logs Insights를 조회하므로(스캔량 기반 과금) **`KPI_LOOKBACK_HOURS=1`
  유지 권장**. 백엔드 TTL 캐시(기본 60초)와 함께 비용 가드레일.
- **face-compare**는 Rekognition **CompareFaces** 호출당 과금. capture 측 DetectLabels가 서버에서
  켜져 있으면 프레임당 추가 과금.
- Kinesis(샤드 시간 + PutRecord), DynamoDB(요청 단위), S3(저장/요청)도 사용량만큼 과금.

---

## 9. 보안 주의

- webui `apigw.js`에 **API Key가 평문**으로 들어가며 브라우저로 전달됩니다 → 로컬 실행이라도
  **브라우저 소스/네트워크 탭에 키가 보입니다**.
- 이 키는 인증 비밀이 아니라 usage-plan throttling/quota 제어용이지만, **운영용 보안 구조는 아닙니다**
  (운영 전환 시 Cognito / Lambda Authorizer / 백엔드 프록시 필요 — P2).
- 로컬 자격증명(`~/.aws/credentials`) 노출에 주의.

---

## 10. 문제 해결

| 증상 | 원인 / 해결 |
|---|---|
| `POST /api/capture-frame` 502 · `kinesis PutRecord failed: AWS credentials unavailable` | 로컬 자격 증명 없음. `aws sts get-caller-identity`, `AWS_PROFILE` 또는 `~/.aws/credentials` 설정. **키를 소스/프론트에 넣지 말 것.** |
| `GET /api/detect-labels` 502 · `lambda … AWS credentials unavailable` | 위와 동일 (boto3 체인). 예전 UI 문구 `Unable to locate credentials` 도 같은 원인. |
| capture/detect-labels 502 · `AWS resource not found` | **데이터 스택 미배포/삭제**. `aws kinesis describe-stream-summary --stream-name FrameStream --region ap-northeast-2`, `aws lambda get-function-configuration --function-name imageprocessor --region ap-northeast-2`. 없으면 `pynt createstack`. |
| capture/detect-labels 502 · `AWS access denied` | 자격 증명은 있으나 IAM 부족(2장 표). |
| `/api/config` 503 | 스택 조회 실패 또는 `cloudformation:DescribeStackResource` / `apigateway:GET` 권한 부족. 캡처와 무관(프레임 뷰어만 영향). |
| AWS credentials 없음 (CLI) | `aws sts get-caller-identity`로 확인. `AWS_PROFILE` 또는 `~/.aws/credentials` 설정. |
| AccessDenied | 로컬 IAM 권한 부족(2장 표). KPI는 카드별 error로 표시(partial failure). |
| `video-analyzer-stack` 못 찾음 | 리전 불일치(ap-northeast-2?) 또는 미배포/삭제. `aws cloudformation describe-stacks --stack-name video-analyzer-stack --region ap-northeast-2`. |
| KPI가 mock으로 보임 | 구버전 코드. 현재 코드는 `source=live`. |
| KPI가 error 배지 | `/api/kpis` 최상위 `error`/`collect_error`(브라우저 콘솔 `console.warn`). region/네트워크/자격증명 확인. |
| KPI 카드별 error | 권한 부족(`logs:StartQuery` / `dynamodb:Query` / `cloudwatch:GetMetricData`) — 다른 카드는 정상. |
| localhost 접속 안 됨 | 서버 프로세스 실행 여부, 방화벽, `--host 127.0.0.1` 확인. |
| 포트 8080 / 8000 충돌 | webui: `uvicorn app:app --port 9090`. KPI: `KPI_PORT=8010` + `uvicorn app:app --port 8010`. |
| CloudWatch Logs Insights 권한 부족 | 해당 카드 `{"error":"...logs:StartQuery..."}`. IAM 보강 또는 일부 카드 미표시 수용. |
| DynamoDB Query 권한 부족 | frames 카드 `{"error":"...dynamodb:Query..."}`. `EnrichedFrame` + GSI Query 권한 부여. |
| Chrome `chrome-extension://… message port closed` | **브라우저 확장 프로그램 메시지**. 이 앱/AWS와 무관. |

---

> EC2 배포(고정 IP, 스케줄러 등)는 [docs/EC2_DEPLOYMENT.md](EC2_DEPLOYMENT.md) 참고.
