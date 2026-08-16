# AWS Face Detection 실행 요약

## 1. 기본 구성

```text
AWS Region:      ap-northeast-2
Stack:           video-analyzer-stack
Kinesis:         FrameStream
DynamoDB:        EnrichedFrame
API Stage:       development
```

데이터 흐름:

```text
카메라/키오스크
  → Kinesis
  → ImageProcessor Lambda
  → S3 / DynamoDB
  → FaceCompare
  → API Gateway
  → Web UI
```

> 키오스크 얼굴 비교를 사용하려면 AWS 데이터 스택이 먼저 실행되어 있어야 한다.

---

## 2. AWS 환경 확인

```bash
export AWS_PROFILE=<profile-name>
export AWS_DEFAULT_REGION=ap-northeast-2

aws sts get-caller-identity
```

기존 스택 확인:

```bash
aws cloudformation describe-stacks \
  --stack-name video-analyzer-stack \
  --region ap-northeast-2 \
  --query 'Stacks[0].StackStatus' \
  --output text
```

`CREATE_COMPLETE` 또는 `UPDATE_COMPLETE`이면 새로 배포할 필요 없음.

---

## 3. AWS 스택 배포

Lambda 패키징:

```bash
pynt packagelambda
```

최초 배포:

```bash
pynt deploylambda
pynt createstack
pynt stackstatus
pynt setlogretention
```

기존 Lambda 코드 갱신:

```bash
pynt packagelambda
pynt "updatelambda[imageprocessor,facecompare,framefetcher]"
```

전체 Lambda 갱신이 필요한 경우:

```bash
pynt packagelambda
pynt updatelambda
```

CloudFormation까지 변경했다면:

```bash
pynt updatestack
```

---

## 4. Web UI / 키오스크 실행

```bash
cd web-ui/backend

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
```

`.env` 주요 설정:

```text
WEBUI_AUTH_USERNAME=<username>
WEBUI_AUTH_PASSWORD_HASH=<password-hash>
WEBUI_SESSION_SECRET=<session-secret>

AWS_DEFAULT_REGION=ap-northeast-2
KINESIS_STREAM=FrameStream
DATA_STACK=video-analyzer-stack
DATA_API_STAGE=development
```

실행:

```bash
export AWS_PROFILE=<profile-name>

uvicorn app:app --host 127.0.0.1 --port 8080
```

접속:

```text
운영자 UI    http://localhost:8080/
키오스크     http://localhost:8080/kiosk
Health      http://localhost:8080/healthz
```

---

## 5. 키오스크 동작 확인

확인 순서:

1. `/healthz` → `{"status":"ok"}`
2. 운영자/키오스크 로그인
3. 키오스크에서 ID 이미지 선택
4. 카메라 촬영
5. `/api/capture-frame` 응답의 `captureId` 확인
6. `/face-compare`의 `targetFrameId` 확인
7. 두 ID가 동일한지 확인

DynamoDB 직접 확인:

```bash
aws dynamodb get-item \
  --table-name EnrichedFrame \
  --key '{"frame_id":{"S":"<captureId>"}}' \
  --consistent-read \
  --region ap-northeast-2
```

`TARGET_FRAME_NOT_READY` 발생 시 같은 `captureId`로 일정 시간 재시도한다.

---

## 6. 선택: 로컬 카메라 Producer

운영자 화면에 지속적으로 프레임을 보내고 싶을 때 사용.

```bash
cd /path/to/aws-face-detection
source .venv/bin/activate

export AWS_PROFILE=<profile-name>
export AWS_DEFAULT_REGION=ap-northeast-2

pynt videocapture[60,300]
```

* 60프레임마다 이미지 전송
* 300초 후 종료
* 즉시 종료: `q`

> 키오스크 촬영에는 별도 producer가 필요하지 않음.

---

## 7. 선택: KPI Dashboard

```bash
cd kpi-dashboard/backend

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

export AWS_PROFILE=<profile-name>
export AWS_DEFAULT_REGION=ap-northeast-2
export KPI_LOOKBACK_HOURS=1

uvicorn app:app --host 127.0.0.1 --port 8000
```

접속:

```text
Dashboard   http://localhost:8000/
Health      http://localhost:8000/healthz
KPI API     http://localhost:8000/api/kpis
```

---

## 8. 테스트

```bash
python -m pytest -q

node --check web-ui/src/kiosk.js

node tests/kiosk_correlation_test.js web-ui/src/kiosk.js

git diff --check
```

---

## 9. 종료

로컬 서버:

```text
Ctrl + C
```

EC2 스택 삭제:

```bash
pynt deleteec2stack
```

AWS 데이터 스택 삭제:

```bash
pynt deletestack
```

> `deletestack` 실행 시 S3의 저장 프레임도 삭제될 수 있으므로 주의한다.

---

## 가장 자주 쓰는 개발 실행 순서

```bash
# 1. AWS 확인
export AWS_PROFILE=<profile-name>
export AWS_DEFAULT_REGION=ap-northeast-2
aws sts get-caller-identity

# 2. Web UI
cd web-ui/backend
source .venv/bin/activate
uvicorn app:app --host 127.0.0.1 --port 8080

# 3. 접속
http://localhost:8080/
http://localhost:8080/kiosk
```

AWS 스택이 이미 배포되어 있다면 **Web UI 실행만으로 대부분의 로컬 개발/테스트가 가능하다.**
