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
| webui | ✅ | `pynt webui` → `pynt webuiserver` (http://localhost:8080) |
| KPI Dashboard | ✅ | `kpi-dashboard/backend`에서 `uvicorn app:app` (http://localhost:8000) |
| video capture client | ✅ | `pynt videocapture[60]` (로컬 카메라 → Kinesis) |
| **AWS 백엔드 (이미 배포돼 있어야 함)** | ❌ 로컬 불가 | `video-analyzer-stack`: Kinesis · Lambda · Rekognition · S3 · DynamoDB · API Gateway |

> webui는 정적 SPA로, 브라우저가 API Gateway를 직접 호출합니다. KPI Dashboard는 boto3로
> CloudWatch/DynamoDB를 읽습니다. capture client는 로컬 카메라 프레임을 Kinesis로 보냅니다.

---

## 2. 사전 조건

- **Python 3.11 또는 3.12 권장** (최소 3.9 — `zoneinfo` 사용). Lambda 런타임은 3.12.
- **pip + venv**.
- **AWS CLI** (권장 — 자격증명 확인/디버깅용).
- **AWS 자격증명**: 기본 자격증명 체인(`~/.aws/credentials`, 환경변수, 또는 `AWS_PROFILE`).
  - 확인: `aws sts get-caller-identity`
  - 프로필 사용: `AWS_PROFILE=<프로필명>`
- **리전**: `AWS_DEFAULT_REGION=ap-northeast-2`
- **`video-analyzer-stack`이 ap-northeast-2에 배포돼 있어야 함** (필수). 없으면 webui `apigw.js` 생성,
  KPI 조회, capture 전송이 모두 실패.
- **API Gateway URL / API Key**: 직접 입력 불필요. `pynt webui`가 스택에서 자동 조회해
  `build/web-ui/src/apigw.js`에 기록합니다.
- **로컬 IAM 권한** (admin이면 전부 충족):

  | 구성요소 | 필요한 최소 권한 |
  |---|---|
  | webui 빌드(`pynt webui`) | `cloudformation:DescribeStackResource`, `apigateway:GET` |
  | capture client | `kinesis:PutRecord` |
  | KPI Dashboard | `logs:StartQuery`/`GetQueryResults`/`StopQuery`, `cloudwatch:GetMetricData`, `dynamodb:Query`/`DescribeTable` |
  | webui 브라우저 → API GW | (IAM 아님, apigw.js의 API Key 사용) |

> 루트 프로젝트(webui/capture)는 `pynt`, `boto3`, `opencv-python`, `pytz`가 설치된 Python 환경이
> 필요합니다(프로젝트 README의 사전 준비 참고). KPI Dashboard는 별도 venv를 씁니다.

---

## 3. webui 로컬 실행

- 빌드 태스크: `pynt webui` — `web-ui/`를 `build/web-ui/`로 복사하고, `video-analyzer-stack`을 조회해
  `build/web-ui/src/apigw.js`에 `var apiBaseUrl=...; var apiKey=...;`를 기록.
- 서버 태스크: `pynt webuiserver` — `build/web-ui/`를 `http.server`로 `0.0.0.0:8080` 서빙(블로킹).
- 실행:
  ```
  pynt webui
  pynt webuiserver
  ```
  → 브라우저 **http://localhost:8080**
- **/enrichedframe 확인**: 페이지가 3초마다 자동 폴링 → DevTools Network에서
  `GET .../development/enrichedframe` 200, 최근 프레임/라벨 표시.
- **/face-compare 확인**: 페이지의 얼굴 업로드 UI에서 이미지 업로드 →
  `POST .../face-compare` 200, 유사도 결과 표시.
- 포트 변경: `pynt "webuiserver[web-ui/,9090]"` (그 후 http://localhost:9090).

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

**터미널 1 — webui** (루트 프로젝트 env: `pynt`/`boto3`/`opencv-python`/`pytz`)
```powershell
cd C:\Users\hsjki\IdeaProjects\amazon-rekognition-video-analyzer-master
$env:AWS_PROFILE="default"
$env:AWS_DEFAULT_REGION="ap-northeast-2"
pynt webui
pynt webuiserver                      # http://localhost:8080  (블로킹)
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
# 터미널 1 — webui
cd ~/.../amazon-rekognition-video-analyzer-master
export AWS_PROFILE=default
export AWS_DEFAULT_REGION=ap-northeast-2
pynt webui && pynt webuiserver        # http://localhost:8080

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
| AWS credentials 없음 | `aws sts get-caller-identity`로 확인. `AWS_PROFILE` 또는 `~/.aws/credentials` 설정. |
| AccessDenied | 로컬 IAM 권한 부족(2장 표). KPI는 카드별 error로 표시(partial failure). |
| `video-analyzer-stack` 못 찾음 | 리전 불일치(ap-northeast-2?) 또는 미배포. `aws cloudformation describe-stacks --stack-name video-analyzer-stack --region ap-northeast-2`. |
| `apigw.js` 생성 실패 / 빈 값 | `pynt webui`의 스택 조회 실패. 자격증명·리전·스택명 확인 후 `build/web-ui/src/apigw.js` 내용 확인. |
| KPI가 mock으로 보임 | 구버전 코드. 현재 코드는 `source=live`. |
| KPI가 error 배지 | `/api/kpis` 최상위 `error`/`collect_error`(브라우저 콘솔 `console.warn`). region/네트워크/자격증명 확인. |
| KPI 카드별 error | 권한 부족(`logs:StartQuery` / `dynamodb:Query` / `cloudwatch:GetMetricData`) — 다른 카드는 정상. |
| localhost 접속 안 됨 | 서버 프로세스 실행 여부, 방화벽, `--host 127.0.0.1` 확인. |
| 포트 8080 / 8000 충돌 | webui: `pynt "webuiserver[web-ui/,9090]"`. KPI: `KPI_PORT=8010` + `uvicorn app:app --port 8010`. |
| CloudWatch Logs Insights 권한 부족 | 해당 카드 `{"error":"...logs:StartQuery..."}`. IAM 보강 또는 일부 카드 미표시 수용. |
| DynamoDB Query 권한 부족 | frames 카드 `{"error":"...dynamodb:Query..."}`. `EnrichedFrame` + GSI Query 권한 부여. |

---

> EC2 배포(고정 IP, 스케줄러 등)는 [docs/EC2_DEPLOYMENT.md](EC2_DEPLOYMENT.md) 참고.
