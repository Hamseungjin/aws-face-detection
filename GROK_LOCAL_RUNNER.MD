# GROK_LOCAL_RUNNER.MD

CloudFormation Stack을 실행하지 않고, 키오스크(STORE / RETRIEVE)만
로컬 또는 현재 개발 EC2에서 실행·검증하기 위한 Runbook이다.

작성 기준일: 2026-08-13  
Source of truth: 현재 repository 코드 + 이 호스트에서 실행한 AWS CLI / IAM / S3 / pytest 결과.

이 문서는 추측으로 쓰지 않았다. CloudFormation 템플릿에 적힌 권한과
**현재 EC2 Role에 실제로 붙어 있는 권한**을 구분한다.

민감정보(password, session secret, AWS access key, cookie 값, 실제 보관번호)는
기록하지 않는다. 필요한 자리는 `<...>` placeholder를 쓴다.

명령어는 두 종류로만 분류한다.

- `[실행]` — 상태를 바꾸거나 프로세스를 시작한다.
- `[확인]` — 읽기만 한다.

AWS CLI 조회는 pager가 `(END)`에서 멈추지 않도록 `AWS_PAGER=""`를 붙인다.

---

## 0. 두 실행 방식을 먼저 구분한다

### A. Stack 없이 Kiosk만 실행 (이 문서의 대상)

CloudFormation Data Stack / Application Stack을 만들지 않는다.

```text
Browser (web-ui/kiosk.html + src/kiosk.js)
  → FastAPI (web-ui/backend/app.py)
      → STORE face-verify : Rekognition CompareFaces (ID bytes vs FRAME-A bytes)
      → STORE complete    : S3 PutObject
                            locker-references/<transaction_id>/reference.jpg
      → RETRIEVE start    : Rekognition CompareFaces
                            (S3 reference.jpg vs FRAME-B bytes)
      → RETRIEVE complete : S3 DeleteObject (reference.jpg)
      → SQLite            : lockers / transactions 메타데이터만
```

이 경로에서 Kiosk는 아래를 사용하지 않는다. 코드로 확인했다.

```text
Kinesis FrameStream
DynamoDB EnrichedFrame
S3 frames/
captureId
targetFrameId
facecompare Lambda
API Gateway /face-compare
imageprocessor Lambda
```

근거:

- `web-ui/src/kiosk.js`는 `kiosk/store/face-verify`, `kiosk/store/reserve`,
  `kiosk/store/complete`, `kiosk/retrieve/start`, `kiosk/retrieve/complete`만 호출한다.
- `capture-frame`, `captureId`, `targetFrameId` 문자열은 `kiosk.js`에 없다.
- `app.py`의 kiosk 핸들러는 `_rekognition.compare_faces`와 `_s3`만 사용한다.
- `reference_face.py` 주석과 구현: STORE는 bytes vs bytes, RETRIEVE는 S3Object vs bytes.
- DynamoDB / facecompare Lambda helper는 `reference_face.py`에 legacy로 남아 있지만
  kiosk STORE/RETRIEVE는 호출하지 않는다.

### B. CloudFormation Stack을 이용한 전체 시스템 실행

두 개의 템플릿이 있다. 둘 다 **현재 이 계정/리전에서는 살아 있는 스택이 없다**.

| Stack | 템플릿 | 전형적인 이름 | 현재 상태 (CLI) |
| ----- | ------ | ------------- | --------------- |
| Data Stack | `aws-infra/aws-infra-cfn.yaml` | `video-analyzer-stack` | `describe-stacks` → 존재하지 않음 |
| Application Stack | `aws-infra/aws-infra-ec2-cfn.yaml` | `video-analyzer-ec2-stack` | `DELETE_COMPLETE`만 존재 |

Data Stack은 운영자 UI / 카메라 파이프라인용이다.

```text
Camera or Operator UI
  → POST /api/capture-frame
  → Kinesis FrameStream
  → imageprocessor Lambda
  → S3 frames/ + DynamoDB EnrichedFrame
  → 운영자 프레임 뷰어 (API Gateway + framefetcher)
  → 운영자 face-compare (API Gateway + facecompare Lambda)
```

Application Stack은 서비스 호스트다. Nginx + systemd FastAPI + KPI +
encrypted EBS SQLite + Application Instance Role을 만든다.

**Kiosk local runner는 A만 하면 된다. B를 먼저 올릴 필요는 없다.**

### 운영자 UI는 별도 경로

`web-ui/src/app.js`는 여전히 `POST /api/capture-frame`을 호출한다.
`app.py`의 `/api/capture-frame`은 `kinesis:PutRecord`를 유지한다.

즉:

```text
Kiosk UI     → Rekognition + S3 locker-references/ + SQLite
Operator UI  → Kinesis (+ Data Stack이 있을 때만 동작)
```

Kiosk local runner에서 운영자 캡처/프레임 뷰어/KPI를 검증하지 않는다.

---

## 1. Quick Start

아래는 요약이다. 처음이면 [16. 최초 Local Kiosk 실행 순서](#16-최초-local-kiosk-실행-순서)를
위에서 아래로 따른다.

### 확인 명령어

```bash
# [확인] 현재 AWS identity
AWS_PAGER="" aws sts get-caller-identity --output json

# [확인] 현재 inline IAM policy 이름
AWS_PAGER="" aws iam list-role-policies --role-name AwsFaceDetectionKioskRole

# [확인] 현재 Role에 rekognition:CompareFaces가 있는지 (정책 문서)
AWS_PAGER="" aws iam get-role-policy \
  --role-name AwsFaceDetectionKioskRole \
  --policy-name AwsFaceDetectionKioskPolicy

# [확인] frames 버킷 존재
AWS_PAGER="" aws s3api head-bucket \
  --bucket aws-face-detection-frames-115019372648-apne2 \
  --region ap-northeast-2

# [확인] 자동 테스트
cd ~/workspace/aws-face-detection
web-ui/backend/.venv/bin/python -m pytest -q
```

2026-08-13 이 호스트에서 확인한 정상 결과:

```text
pytest 전체: 114 passed
```

구 문서의 `83 passed / 7 passed`는 현재 전체가 아니다.
키오스크 관련 파일만 모으면 83, imageprocessor correlation만 모으면 7이다.

### 최초 1회 실행 명령어

현재 이 Role/계정 기준으로 **부족한 것**은 두 가지다.

1. `rekognition:CompareFaces` 권한 (inline policy에 없음, 실제 API AccessDenied)
2. S3 버킷 `aws-face-detection-frames-115019372648-apne2` (HeadBucket 404)

```bash
# [실행] 테스트용 CompareFaces 권한 추가 (현재 Role은 iam:PutRolePolicy 허용)
# 장기 운영용이 아니다. 끝나면 Application Stack / IaC로 이전한다.
AWS_PAGER="" aws iam put-role-policy \
  --role-name AwsFaceDetectionKioskRole \
  --policy-name AwsFaceDetectionKioskRekognitionCompare \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Sid": "KioskCompareFaces",
        "Effect": "Allow",
        "Action": ["rekognition:CompareFaces"],
        "Resource": "*"
      }
    ]
  }'

# [실행] frames 버킷이 없을 때만 생성
AWS_PAGER="" aws s3api create-bucket \
  --bucket aws-face-detection-frames-115019372648-apne2 \
  --region ap-northeast-2 \
  --create-bucket-configuration LocationConstraint=ap-northeast-2

# [실행] public access 차단 (Data Stack 템플릿에는 PublicAccessBlock이 없다. local/dev에서는 반드시 넣는다)
AWS_PAGER="" aws s3api put-public-access-block \
  --bucket aws-face-detection-frames-115019372648-apne2 \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

### 매번 실행할 명령어

```bash
# [실행] 작업 위치
cd ~/workspace/aws-face-detection

# [실행] backend venv (root .venv에는 uvicorn이 없다)
source web-ui/backend/.venv/bin/activate

# [실행] 리전. 자격 증명은 이 EC2 instance role을 쓴다. access key를 export하지 말 것.
export AWS_DEFAULT_REGION=ap-northeast-2
export WEBUI_SESSION_HTTPS_ONLY=false

# [실행] FastAPI
cd web-ui/backend
uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

### 실행 후 확인

```bash
# [확인] health (별도 터미널)
curl -i http://127.0.0.1:8080/healthz
# 기대: HTTP 200  {"status":"ok"}

# [확인] 브라우저
#   같은 호스트:           http://127.0.0.1:8080/kiosk
#   code-server proxy:     /proxy/8080/kiosk
```

---

## 2. 로컬 실행 시 사용하는 구성요소

코드와 현재 AWS 실측 기준이다.

| 구성요소 | Stack 없이 Kiosk 실행 시 사용 여부 | 위치/서비스 | 역할 |
| -------- | ---: | ----------- | ---- |
| Browser Kiosk | O | `web-ui/kiosk.html`, `web-ui/src/kiosk.js` | 시민 UI. 로그인 후 STORE/RETRIEVE |
| FastAPI | O | `web-ui/backend/app.py` local/EC2 process | 세션, kiosk API, Rekognition/S3 호출 |
| SQLite | O | `KIOSK_DB_PATH` 규칙. 이 호스트 기본값은 `web-ui/backend/data/kiosk.sqlite3` | locker / transaction 메타데이터 |
| Rekognition CompareFaces | O | AWS `ap-northeast-2` | STORE ID vs FRAME-A, RETRIEVE reference vs FRAME-B |
| S3 `locker-references/` | O | `FRAME_S3_BUCKET` | 거래당 `reference.jpg` 한 장 |
| boto3 credential | O | 이 EC2는 instance role `AwsFaceDetectionKioskRole` | AWS 호출. 브라우저에 키를 내려주지 않음 |
| Kinesis | X | AWS | 운영자 `/api/capture-frame` 전용 |
| DynamoDB `EnrichedFrame` | X | AWS | 운영자 프레임 메타데이터. Kiosk DB가 아님 |
| S3 `frames/` | X | AWS | 운영자/imageprocessor 프레임 저장 |
| imageprocessor Lambda | X | AWS | 운영자 파이프라인 |
| facecompare Lambda | X | AWS | 운영자 `/face-compare` legacy |
| framefetcher Lambda | X | AWS | 운영자 프레임 목록 |
| API Gateway | X | AWS | 운영자 프레임 뷰어 / face-compare |
| KPI Dashboard | X | local `:8000` 또는 Application Stack `/dashboard/` | 전체 배포 시에만 |
| Nginx | X (선택) | Application Stack만 | TLS 종료. local runner는 uvicorn 직접 |
| SSM | X (선택) | Application Stack Role에 `AmazonSSMManagedInstanceCore` | 이 local runner 경로에 불필요 |
| CloudFormation | X | Data/Application Stack | local kiosk에 불필요 |

---

## 3. SQLite DB 경로

파일 존재가 아니라 `config.py` 규칙이 경로를 결정한다.

### 결정 규칙

`web-ui/backend/config.py`:

1. `python-dotenv`가 `web-ui/backend/.env`를 **절대 경로**로 읽는다.
   `override=False`이므로 이미 있는 프로세스 환경변수가 이긴다.
2. EC2 Application Stack은 systemd `EnvironmentFile=/etc/webui.env`를 먼저 넣는다.
   그 값이 있으면 `.env`는 덮어쓰지 못한다.
3. `KIOSK_DB_PATH = os.getenv("KIOSK_DB_PATH", <backend>/data/kiosk.sqlite3)`
4. `KioskStore.__init__`이 parent 디렉터리를 만들고 `initialize()`를 호출한다.

후보와 실제 사용 여부:

| 후보 | 역할 | 현재 이 호스트 |
| ---- | ---- | -------------- |
| `web-ui/backend/data/kiosk.sqlite3` | 코드 기본값 | **실제 사용 경로** |
| `/data/kiosk/kiosk.db` | Application Stack bootstrap 값 | 디렉터리 없음. 사용하지 않음 |
| 프로세스 `KIOSK_DB_PATH` | 최우선 | 이 세션에서 UNSET |
| `web-ui/backend/.env`의 `KIOSK_DB_PATH` | dotenv | 주석 처리되어 미설정 |
| `/etc/webui.env` | Application Stack | 파일 없음 |

`web-ui/backend/data/`는 `.gitignore`에 있다. DB 파일을 커밋하지 않는다.

### SQLite DB 경로

```text
Local default:
  <repo>/web-ui/backend/data/kiosk.sqlite3
  이 워크스페이스:
  /home/ubuntu/workspace/aws-face-detection/web-ui/backend/data/kiosk.sqlite3

Application Stack:
  /data/kiosk/kiosk.db
  (encrypted EBS, /etc/webui.env, systemd RequiresMountsFor=/data/kiosk)
  bootstrap는 /data/kiosk/* 가 아니면 실패한다.

현재 이 EC2/session에서 실제 설정:
  프로세스 KIOSK_DB_PATH = UNSET
  /etc/webui.env = 없음
  web-ui/backend/.env 의 KIOSK_DB_PATH = 주석
  config.py를 이 디렉터리에서 import한 결과:
    KIOSK_DB_PATH = /home/ubuntu/workspace/aws-face-detection/web-ui/backend/data/kiosk.sqlite3
  파일 존재: 예 (36864 bytes, 2026-08-13 확인)
```

`sqlite3` CLI는 이 호스트에 없다. 조회는 Python을 사용한다.

```bash
# [확인]
python3 - <<'PY'
import sqlite3
from pathlib import Path
p = Path("/home/ubuntu/workspace/aws-face-detection/web-ui/backend/data/kiosk.sqlite3")
print("exists", p.exists(), "size", p.stat().st_size)
con = sqlite3.connect(p)
print(con.execute("SELECT COUNT(*) FROM lockers").fetchone()[0], "lockers")
print(list(con.execute("SELECT status, COUNT(*) FROM lockers GROUP BY status")))
print(list(con.execute("SELECT status, COUNT(*) FROM transactions GROUP BY status")))
PY
```

---

## 4. DB 테이블 및 컬럼

`KioskStore.initialize()` + `_migrate_transaction_columns()` + 이 호스트 실제 DB
`PRAGMA table_info`가 일치한다.

저장하지 않는 것: 신분증 이미지, FRAME-A/B bytes, face embedding, AWS 자격 증명, API key.

### lockers

| Column | Type | Key | Nullable | 의미 | 언제 변경되는지 |
| ------ | ---- | --- | -------- | ---- | --------------- |
| `locker_id` | TEXT | PRIMARY KEY | YES (PK라 SQLite notnull=0) | `01`~`12` | seed 이후 불변 |
| `status` | TEXT |  | NO | `AVAILABLE` / `RESERVED` / `OCCUPIED` / `DISABLED` | reserve, complete store, cancel, expire, complete retrieve |
| `active_transaction_id` | TEXT | FK → `transactions.transaction_id` DEFERRABLE | YES | 현재 활성 거래. 없으면 NULL | reserve 시 설정, cancel/expire/retrieve complete 시 NULL |
| `updated_at` | TEXT |  | NO | UTC ISO-8601 | 상태 변경 시 |

초기 seed (`LOCKER_SEED`):

- AVAILABLE: 01, 02, 04, 05, 07, 09, 10, 12
- OCCUPIED: 03, 06, 11 (데모 점유. 보관번호 없음)
- DISABLED: 08

`INSERT OR IGNORE`이므로 기존 행을 덮어쓰지 않는다.

### transactions

| Column | Type | Key | Nullable | 의미 | 현재 신규 코드에서 사용 여부 |
| ------ | ---- | --- | -------- | ---- | ---------------------------- |
| `transaction_id` | TEXT | PRIMARY KEY | YES (PK) | 내부 UUID | **사용**. S3 key 계산 입력 |
| `retrieval_code` | TEXT | UNIQUE | YES | 고객용 8자리 숫자. STORE 완료 전 NULL | **사용**. RETRIEVE 조회 키 |
| `locker_id` | TEXT | FK → lockers | NO | 거래 보관함 | **사용** |
| `status` | TEXT |  | NO | `RESERVED` `STORED` `RETRIEVING` `RETRIEVED` `CANCELLED` `EXPIRED` | **사용** |
| `amount` | INTEGER |  | YES | 데모 결제 금액. 코드는 2000 | **사용** |
| `mock_payment_id` | TEXT | UNIQUE | YES | `PAY-` + 6 hex | **사용**. 브라우저에 내려줌. 소유 증명이 아님 |
| `created_at` | TEXT |  | NO | 생성 시각 | **사용** |
| `reserved_at` | TEXT |  | YES | 예약 시각 | **사용** |
| `stored_at` | TEXT |  | YES | STORE 완료 시각 | **사용** |
| `retrieval_started_at` | TEXT |  | YES | RETRIEVE 시작. stale 복구에 사용 | **사용** |
| `retrieved_at` | TEXT |  | YES | RETRIEVE 완료 | **사용** |
| `reservation_expires_at` | TEXT |  | YES | 예약 만료. 기본 120초 | **사용** |
| `reference_frame_id` | TEXT |  | YES | **legacy**. 예전 EnrichedFrame `frame_id` | **신규 kiosk는 쓰지 않음**. `app.py` store/complete가 이 인자를 넘기지 않음. 컬럼은 마이그레이션으로 유지 |
| `reference_face_s3_key` | TEXT |  | YES | **legacy 위치 저장**. 신규 코드는 source of truth가 아님 | STORE complete가 계산된 키를 **기록**한다. RETRIEVE compare/delete는 `transaction_id`로 키를 **다시 계산**한다. `referencePresent` / `mark_reference_deleted`의 “있었다” 플래그로만 의미 있음 |
| `reference_face_created_at` | TEXT |  | YES | reference put 시각 | **사용**. `complete_store`가 `reference_face_s3_key`와 함께 기록 |
| `reference_face_deleted_at` | TEXT |  | YES | reference delete 시각 | **사용**. RETRIEVE complete 후 `mark_reference_deleted()` |

인덱스:

```text
UNIQUE INDEX one_active_transaction_per_locker
  ON transactions(locker_id)
  WHERE status IN ('RESERVED', 'STORED', 'RETRIEVING')

INDEX idx_transactions_retrieval_code_status
  ON transactions(retrieval_code, status)
```

### deterministic S3 key

`reference_face.build_reference_face_s3_key(transaction_id)`:

```text
<KIOSK_REFERENCE_S3_PREFIX><transaction_id>/reference.jpg
```

기본 prefix는 `locker-references/`.

예:

```text
locker-references/11111111-1111-4111-8111-111111111111/reference.jpg
```

`transaction_id`에서 계산한다. DB의 `reference_face_s3_key`를 읽어 S3 경로를
결정하지 않는다.

`complete_store(..., reference_face_s3_key=...)`는 같은 계산 결과를 컬럼에
넣어 두지만, 주석과 RETRIEVE 경로 모두 “컬럼이 source of truth가 아니다”라고
명시한다. 컬럼을 지워도 키는 `transaction_id`로 재현된다.

### 이 호스트 실제 DB 관찰 (값/코드 미기록)

2026-08-13 `PRAGMA table_info`는 위 컬럼과 동일하다.
transactions 6행 중 `reference_frame_id`가 채워진 행이 있다. 예전 경로의
잔여이며, 신규 STORE는 이 컬럼을 쓰지 않는다.

---

## 5. 현재 DB ERD

```text
Kiosk local runner SQLite
=========================

LOCKERS
  locker_id PK
  status
  active_transaction_id ────────┐
  updated_at                    │  0..1
                                │
                  1:N           │
TRANSACTIONS ◄──────────────────┘
  transaction_id PK
  locker_id FK
  retrieval_code UNIQUE
  status
  amount / mock_payment_id
  timestamps
  reference_frame_id          (legacy, unused by new kiosk)
  reference_face_s3_key       (legacy location copy, not SoT)
  reference_face_created_at
  reference_face_deleted_at
        │
        │ transaction_id  (키는 이 값으로 계산)
        ▼
S3  s3://<FRAME_S3_BUCKET>/locker-references/<transaction_id>/reference.jpg
    (거래당 1장. RETRIEVED 후 삭제)


Kiosk local runner:
  DynamoDB dependency 없음
  EnrichedFrame 테이블은 이 SQLite에 포함되지 않음
  Kinesis / frames/ / captureId 도 이 ERD에 없음
```

운영자 파이프라인이 Data Stack과 함께 살아 있을 때의 별도 그림:

```text
Operator only (NOT kiosk local DB)
  /api/capture-frame
    → Kinesis FrameStream
    → imageprocessor
    → S3 frames/<...>
    → DynamoDB EnrichedFrame (frame_id PK)
```

---

## 6. 현재 AWS 실행 주체

이 호스트에서 실행한 결과.

```bash
# [확인]
AWS_PAGER="" aws sts get-caller-identity --output json
```

```text
Account: 115019372648
IAM assumed role: AwsFaceDetectionKioskRole
Instance: i-060ed51daa9abc84f
Region: ap-northeast-2
Arn: arn:aws:sts::115019372648:assumed-role/AwsFaceDetectionKioskRole/i-060ed51daa9abc84f
```

IMDSv2로도 같은 instance id / role name / region을 확인했다.
프로세스에 `AWS_ACCESS_KEY_ID`는 UNSET이다. boto3는 instance profile을 사용한다.

이 인스턴스는 Application Stack 호스트가 아니다.

```text
/etc/webui.env 없음
/data/kiosk 없음
kiosk-fastapi.service 없음
video-analyzer-ec2-stack = DELETE_COMPLETE
```

---

## 7. 현재 IAM Role 권한 조사

### 조회 가능 / 불가

```text
Inline policies: 확인 가능 (iam:ListRolePolicies, iam:GetRolePolicy 성공)
Attached managed policies: 현재 Role 권한으로 확인 불가
  iam:ListAttachedRolePolicies → AccessDenied
iam:SimulatePrincipalPolicy → AccessDenied
iam:ListInstanceProfilesForRole → AccessDenied
iam:GetRole → 성공 (RoleName/Arn/Path만)
```

```bash
# [확인]
AWS_PAGER="" aws iam list-role-policies --role-name AwsFaceDetectionKioskRole
```

확인된 inline policy 이름:

```text
ApplicationNetworkPreflightReadOnly
AssumeApplicationDeployerRole
AwsFaceDetectionDeployS3Policy
AwsFaceDetectionKioskPolicy
AwsFaceDetectionWebUiConfigReadPolicy
WebUiAuthBootstrapPolicy
```

각 문서는 `aws iam get-role-policy`로 읽었다. 요지:

| Policy | Kiosk local runner 관련 내용 |
| ------ | ----------------------------- |
| `AwsFaceDetectionDeployS3Policy` | 아래 두 버킷에 `s3:*`. `iam:PutRolePolicy` 등 IAM 관리 액션을 `arn:aws:iam::115019372648:role/*`에 허용. CFN/Kinesis/DynamoDB/Lambda/API GW 배포 권한도 있음 |
| `AwsFaceDetectionKioskPolicy` | `kinesis:PutRecord`, `kinesis:DescribeStreamSummary` on `FrameStream`. CFN describe, `apigateway:GET`. **Rekognition 없음** |
| `AwsFaceDetectionWebUiConfigReadPolicy` | `cloudformation:DescribeStackResource` on `video-analyzer-stack` |
| `WebUiAuthBootstrapPolicy` | 특정 SSM 파라미터 `ssm:PutParameter` |
| `ApplicationNetworkPreflightReadOnly` | EC2 describe / EIP quota 조회 |
| `AssumeApplicationDeployerRole` | `sts:AssumeRole` → `ApplicationDeployerRole` |

**어떤 inline policy에도 `rekognition:CompareFaces`가 없다.**

---

## 8. Kiosk 실행에 필요한 최소 AWS 권한

코드 호출 기준. Data Stack/운영자 API 권한은 포함하지 않는다.

| 코드 | AWS API | IAM Action | 언제 |
| ---- | ------- | ---------- | ---- |
| `compare_id_to_face_bytes()` | `rekognition.compare_faces` Source=Bytes Target=Bytes | `rekognition:CompareFaces` | `POST /api/kiosk/store/face-verify` |
| `reference_object_exists()` | `s3.head_object` | IAM에는 `s3:HeadObject`가 없고 **`s3:GetObject`가 Head를 허용** | `POST /api/kiosk/retrieve/start` |
| `save_reference_face()` | 먼저 `s3.head_object` (이미 있으면 reuse) | `s3:GetObject` | `POST /api/kiosk/store/complete` |
| `save_reference_face()` | 없으면 `s3.put_object` | `s3:PutObject` | 위와 동일 |
| `compare_reference_to_face_bytes()` | `rekognition.compare_faces` Source=`S3Object` Target=Bytes | `rekognition:CompareFaces` | `POST /api/kiosk/retrieve/start` |
| 위 CompareFaces의 S3 읽기 | Rekognition이 **호출자 자격 증명**으로 S3 object를 읽음 | 호출자에게 `s3:GetObject` on that key | 위와 동일 |
| `delete_reference_face()` | `s3.delete_object` | `s3:DeleteObject` | `POST /api/kiosk/retrieve/complete` (`KIOSK_REFERENCE_DELETE_ON_RETRIEVE=true`) |

최소 세트:

```text
rekognition:CompareFaces     Resource: *

s3:GetObject                 Resource: arn:aws:s3:::<FRAME_S3_BUCKET>/locker-references/*
s3:PutObject                 Resource: arn:aws:s3:::<FRAME_S3_BUCKET>/locker-references/*
s3:DeleteObject              Resource: arn:aws:s3:::<FRAME_S3_BUCKET>/locker-references/*
```

버킷을 직접 만들 때만 추가:

```text
s3:CreateBucket
s3:PutBucketPublicAccessBlock
s3:ListBucket                  (선택, smoke test / ls)
```

Kiosk local runner에 **필요 없는** 것:

```text
kinesis:PutRecord
dynamodb:GetItem / Query
lambda:InvokeFunction (facecompare)
lambda:GetFunctionConfiguration
cloudformation:DescribeStackResource
apigateway:GET
```

---

## 9. 현재 Role에서 이미 가능한 AWS 권한

Role: **`AwsFaceDetectionKioskRole`** (현재 실행 Role)

Application Stack이 만들 Role: **`ApplicationInstanceRole`**
(`aws-infra-ec2-cfn.yaml`은 `RoleName`을 생략하므로 CFN이 고유 이름을 생성한다).
**지금 이 EC2 Role과 다른 객체다.**

CloudFormation 템플릿의 `application-rekognition-compare`는
Application Instance Role에만 들어 있다. 현재 Role에 복사되어 있지 않다.

| Action | 현재 확인 결과 | 근거 |
| ------ | -------------- | ---- |
| `s3:PutObject` | O | `AwsFaceDetectionDeployS3Policy`의 `s3:*` on 두 버킷 ARN과 `/*` |
| `s3:GetObject` | O | 동일 (`head_object` 포함) |
| `s3:DeleteObject` | O | 동일 |
| `s3:CreateBucket` (해당 이름) | O (정책상) | 동일, 버킷 ARN 자체에 `s3:*` |
| `s3:PutBucketPublicAccessBlock` | O (정책상) | 동일 |
| `rekognition:CompareFaces` | X | 6개 inline policy에 없음. 실제 `compare_faces` 호출 → `AccessDeniedException` |
| `iam:PutRolePolicy` | O | `AwsFaceDetectionDeployS3Policy` `ManageProjectIamRoles` on `role/*` |
| `iam:GetRolePolicy` / `ListRolePolicies` | O | 실제 조회 성공 |
| `iam:ListAttachedRolePolicies` | X | AccessDenied |
| `iam:SimulatePrincipalPolicy` | X | AccessDenied |
| `kinesis:PutRecord` | O (정책상) | `AwsFaceDetectionKioskPolicy`. 스트림 자체는 현재 없음 |
| attached managed policies | 확인 불가 | ListAttached AccessDenied |

실측 CompareFaces (이미지 내용은 남기지 않음):

```text
code=AccessDeniedException
message=User: ...assumed-role/AwsFaceDetectionKioskRole/i-060ed51daa9abc84f
        is not authorized to perform: rekognition:CompareFaces
        because no identity-based policy allows the rekognition:CompareFaces action
```

---

## 10. 현재 허용된 S3 Resource

`AwsFaceDetectionDeployS3Policy` Resource:

```text
arn:aws:s3:::aws-face-detection-lambda-115019372648-apne2
arn:aws:s3:::aws-face-detection-lambda-115019372648-apne2/*
arn:aws:s3:::aws-face-detection-frames-115019372648-apne2
arn:aws:s3:::aws-face-detection-frames-115019372648-apne2/*
```

코드 기본 `FRAME_S3_BUCKET`도 `aws-face-detection-frames-115019372648-apne2`이다
(`config.py`, `config/cfn-params.json`).

```bash
# [확인]
AWS_PAGER="" aws s3api head-bucket \
  --bucket aws-face-detection-lambda-115019372648-apne2 \
  --region ap-northeast-2
# 2026-08-13: 존재. BucketRegion=ap-northeast-2
# public access block: 네 항목 모두 true

AWS_PAGER="" aws s3api head-bucket \
  --bucket aws-face-detection-frames-115019372648-apne2 \
  --region ap-northeast-2
# 2026-08-13: 404 Not Found
```

```text
lambda 버킷: IAM 허용 + 리소스 존재
frames 버킷: IAM policy에는 허용되어 있으나 현재 bucket resource는 없음
```

Data Stack이 frames 버킷을 만든다. Data Stack이 없어서 버킷도 없다.

---

## 11. 현재 local runner가 추가로 준비해야 하는 AWS 리소스

조사 결과.

필요 (없으면 STORE/RETRIEVE E2E 실패):

1. `rekognition:CompareFaces` 권한 — 현재 Role에 없음
2. reference 이미지를 저장할 S3 버킷 `aws-face-detection-frames-115019372648-apne2` — 현재 없음

이미 있음:

1. EC2 IAM credential (`AwsFaceDetectionKioskRole` instance profile)
2. 해당 frames 버킷 ARN에 대한 `s3:*` IAM policy
3. 로컬 SQLite 기본 경로와 FastAPI 코드
4. `web-ui/backend/.env`의 로그인 secret (값은 문서에 적지 않음)
5. `iam:PutRolePolicy` — 테스트용으로 CompareFaces를 직접 붙일 수 있음

필요 없음 (Kiosk only):

```text
video-analyzer-stack
Kinesis FrameStream
DynamoDB EnrichedFrame
imageprocessor / facecompare / framefetcher
API Gateway
Application Stack / Nginx / EBS /data/kiosk
```

---

## 12. 권한 추가 방법

### Local/dev test (임시, 수동)

현재 Role은 `iam:PutRolePolicy`를 실제로 허용한다. 정책 문서에 있다.

```bash
# [실행] 현재 테스트 환경에만 적용. production에 남기지 말 것.
AWS_PAGER="" aws iam put-role-policy \
  --role-name AwsFaceDetectionKioskRole \
  --policy-name AwsFaceDetectionKioskRekognitionCompare \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Sid": "KioskCompareFaces",
        "Effect": "Allow",
        "Action": ["rekognition:CompareFaces"],
        "Resource": "*"
      }
    ]
  }'
```

```bash
# [확인] 반영됐는지
AWS_PAGER="" aws iam get-role-policy \
  --role-name AwsFaceDetectionKioskRole \
  --policy-name AwsFaceDetectionKioskRekognitionCompare
```

IAM 변경은 보통 수 초 안에 보이지만, 이미 떠 있는 프로세스는 재시작할 필요는 없다.
권한은 다음 API 호출부터 평가된다.

테스트가 끝나면 수동 정책을 지운다.

```bash
# [실행] 정리. 이 Role의 다른 정책은 지우지 말 것.
AWS_PAGER="" aws iam delete-role-policy \
  --role-name AwsFaceDetectionKioskRole \
  --policy-name AwsFaceDetectionKioskRekognitionCompare
```

### Production / Stack

수동 `put-role-policy`를 장기 source of truth로 쓰지 않는다.

정식 권한은 `aws-infra/aws-infra-ec2-cfn.yaml`의 `ApplicationInstanceRole`이다.

```text
application-rekognition-compare  → rekognition:CompareFaces
application-reference-face-s3    → s3:GetObject/PutObject/DeleteObject
                                   on .../frames/* 와 .../locker-references/*
```

```text
local test  = temporary / manual
production  = CloudFormation / IaC
```

---

## 13. S3 bucket 생성 방법

Stack 없이 테스트할 때, IAM은 아래 이름을 허용하지만 버킷이 없으면 만든다.

```text
aws-face-detection-frames-115019372648-apne2
```

다른 이름을 쓰면 `FRAME_S3_BUCKET`을 맞춰야 하고, 현재 Role의 S3 허용 ARN에도
없을 수 있다. **코드 기본값과 IAM 허용 이름을 그대로 쓰는 것이 안전하다.**

```bash
# [확인] 없을 때만 아래 [실행]으로 진행
AWS_PAGER="" aws s3api head-bucket \
  --bucket aws-face-detection-frames-115019372648-apne2 \
  --region ap-northeast-2
```

404이면:

```bash
# [실행] 버킷 생성. 이미 있으면 실패한다. 다른 계정 이름을 만들지 말 것.
AWS_PAGER="" aws s3api create-bucket \
  --bucket aws-face-detection-frames-115019372648-apne2 \
  --region ap-northeast-2 \
  --create-bucket-configuration LocationConstraint=ap-northeast-2

# [실행] public access 차단
AWS_PAGER="" aws s3api put-public-access-block \
  --bucket aws-face-detection-frames-115019372648-apne2 \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

```bash
# [확인]
AWS_PAGER="" aws s3api get-public-access-block \
  --bucket aws-face-detection-frames-115019372648-apne2
# 기대: 네 항목 모두 true
```

Data Stack 템플릿의 `FrameS3Bucket`에는 PublicAccessBlock 속성이 없다.
lifecycle은 `frames/`, `logging/`만 만료하고 `locker-references/`는 만료하지 않는다.
local/dev 수동 버킷에도 앱이 reference를 직접 지운다.

**경고:** 이 버킷 이름은 Data Stack 파라미터와 같다. 나중에 Data Stack을 올리면
같은 이름을 다시 만들려다 충돌한다. 그때는 기존 버킷을 import하거나,
비어 있으면 삭제 후 스택이 재생성하게 한다.

---

## 14. 환경변수

Source of truth는 `web-ui/backend/config.py`이다.

로드 순서:

1. 프로세스 환경 (`/etc/webui.env`를 systemd가 주입한 경우 포함)
2. `web-ui/backend/.env` (`override=False`)
3. `config.py` 기본값

현재 이 세션:

```text
/etc/webui.env 없음
프로세스 KIOSK_* / FRAME_S3_BUCKET UNSET
web-ui/backend/.env 존재 (gitignore). KIOSK_DB_PATH는 주석.
config import 결과 REGION=ap-northeast-2, SESSION_HTTPS_ONLY=False,
FRAME_S3_BUCKET=aws-face-detection-frames-115019372648-apne2 (코드 기본값)
```

Stack 없이 Kiosk만 실행할 때 필요한 변수:

| env | 필수 여부 | local 기본값 | 역할 |
| --- | --------- | ------------ | ---- |
| `WEBUI_AUTH_USERNAME` | 필수 (로그인) | `""` → 로그인 전부 실패 | 키오스크 잠금 해제 |
| `WEBUI_AUTH_PASSWORD_HASH` | 필수 | `""` | PBKDF2 해시. 비밀번호 평문 아님 |
| `WEBUI_SESSION_SECRET` | 필수에 가깝다 | 비어 있으면 `"dev-insecure-secret-change-me"` | 세션 쿠키 서명 |
| `WEBUI_SESSION_HTTPS_ONLY` | 로컬 HTTP면 사실상 필수 `false` | `false` | `true`이면 HTTP에서 쿠키가 안 붙음 |
| `WEBUI_SESSION_SAME_SITE` | 선택 | `lax` | 쿠키 SameSite |
| `WEBUI_SESSION_COOKIE` | 선택 | `webui_session` | 쿠키 이름 |
| `WEBUI_PORT` | 선택 | `8080` | 문서화용. uvicorn `--port`가 실제 listen |
| `AWS_DEFAULT_REGION` 또는 `AWS_REGION` | 권장 | `ap-northeast-2` | Rekognition/S3 리전 |
| `FRAME_S3_BUCKET` | 권장 | `aws-face-detection-frames-115019372648-apne2` | reference 버킷 |
| `KIOSK_REFERENCE_S3_PREFIX` | 선택 | `locker-references/` | 키 prefix |
| `FACE_SIMILARITY_THRESHOLD` | 선택 | `90` | CompareFaces 비즈니스 threshold |
| `KIOSK_STORE_VERIFICATION_SECONDS` | 선택 | `KIOSK_RESERVATION_SECONDS` (120) | face-verify 후 complete 유효 시간 |
| `KIOSK_REFERENCE_DELETE_ON_RETRIEVE` | 선택 | `true` | RETRIEVED 후 reference 삭제 |
| `KIOSK_REFERENCE_POST_RETRIEVAL_RETENTION_SECONDS` | 선택 | `0` | `0`이면 즉시 삭제 |
| `KIOSK_DB_PATH` | 선택 | `web-ui/backend/data/kiosk.sqlite3` | SQLite 파일 |
| `KIOSK_RESERVATION_SECONDS` | 선택 | `120` | 예약 TTL |
| `KIOSK_RETRIEVAL_SECONDS` | 선택 | `120` | stale RETRIEVING 복구 |
| `KIOSK_RETRIEVAL_MAX_FAILURES` | 선택 | `5` | 프로세스 로컬 rate limit |
| `KIOSK_RETRIEVAL_RATE_WINDOW_SECONDS` | 선택 | `60` | rate window |
| `KIOSK_MAX_ID_IMAGE_BYTES` | 선택 | `5242880` | ID/얼굴 최대 바이트 |

Kiosk-only에서 **설정할 필요 없는** 변수:

```text
KINESIS_STREAM
ENRICHED_FRAME_TABLE
FACECOMPARE_FUNCTION_NAME
IMAGEPROCESSOR_FUNCTION_NAME
DATA_STACK / DATA_API_STAGE
AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY   (이 EC2에서는 instance role 사용)
```

비밀 값은 이렇게만 표시한다.

```text
WEBUI_AUTH_PASSWORD_HASH=<REQUIRED_SECRET>
WEBUI_SESSION_SECRET=<REQUIRED_SECRET>
```

생성 (값은 화면에 나오므로 로그/이 문서에 붙이지 말 것):

```bash
# [실행] 해시/시크릿 생성. 출력은 .env에만 넣고 커밋하지 말 것.
cd web-ui/backend
python auth.py '<your-password>'
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

`.env`가 없으면:

```bash
# [실행]
cd web-ui/backend
cp .env.example .env
# WEBUI_AUTH_* / WEBUI_SESSION_SECRET 만 채운다.
# AWS 키를 넣지 않는다.
# 로컬 HTTP: WEBUI_SESSION_HTTPS_ONLY=false
```

---

## 15. 실행 명령어와 확인 명령어

### 실행 명령어 `[실행]`

시스템 상태를 바꾸거나 서비스를 시작한다.

```text
cd ...
source web-ui/backend/.venv/bin/activate
export AWS_DEFAULT_REGION=...
export WEBUI_SESSION_HTTPS_ONLY=false
aws iam put-role-policy
aws iam delete-role-policy
aws s3api create-bucket
aws s3api put-public-access-block
aws s3api put-object / delete-object   (smoke test object)
uvicorn ...
systemctl restart ...                  (Application Stack 호스트에서만)
```

### 확인 명령어 `[확인]`

```text
pwd
git status
python --version / which python
pytest
aws sts get-caller-identity
aws iam list-role-policies
aws iam get-role-policy
aws s3api head-bucket
aws s3api get-public-access-block
aws s3api head-object
curl /healthz
python + sqlite3 SELECT
```

---

## 16. 최초 Local Kiosk 실행 순서

신규 개발자가 이 레포와 이 개발 EC2만 있다고 가정한다.
Application Stack / Data Stack은 올리지 않는다.

### STEP 0. 프로젝트 루트

```bash
# [실행]
cd ~/workspace/aws-face-detection
```

```bash
# [확인]
pwd
# 기대: /home/ubuntu/workspace/aws-face-detection

git status
# 기대: 레포가 dirty여도 이 문서 작성에는 무관. 브랜치 확인용.
```

실패하면: 경로가 다르다. 실제 clone 위치로 `cd`한다.

### STEP 1. Python environment 확인

root `.venv`에는 `pytest`는 있고 `uvicorn`은 없다.
FastAPI 실행은 **`web-ui/backend/.venv`** 를 쓴다 (uvicorn 0.52.1, Python 3.11.15).

```bash
# [실행]
source web-ui/backend/.venv/bin/activate
```

venv가 없으면:

```bash
# [실행]
cd web-ui/backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest httpx    # 테스트용. requirements.txt에는 pytest가 없음
cd ../..
```

```bash
# [확인]
python --version
# 기대: Python 3.11.x

which python
# 기대: .../web-ui/backend/.venv/bin/python

python -c "import fastapi, uvicorn, boto3; print('ok')"
```

실패하면: venv 생성/의존성 설치를 다시 한다. 시스템 `python3` (이 호스트는 3.14)로
uvicorn을 실행하지 말 것.

### STEP 2. 자동 테스트

AWS를 호출하지 않는다. Rekognition/S3는 테스트가 mock한다.

```bash
# [확인]
cd ~/workspace/aws-face-detection
web-ui/backend/.venv/bin/python -m pytest -q
```

2026-08-13 실제 결과:

```text
114 passed, 1 warning in 2.77s
```

키오스크 관련만:

```bash
# [확인]
web-ui/backend/.venv/bin/python -m pytest -q \
  tests/test_app_kiosk.py \
  tests/test_kiosk_store.py \
  tests/test_kiosk_javascript_correlation.py \
  tests/test_session_auth.py
# 2026-08-13: 83 passed
```

`node tests/kiosk_correlation_test.js`를 인자 없이 실행하면 실패한다.
pytest가 `kiosk.js` 경로를 넘겨 실행한다.

실패하면: 코드가 깨진 것이다. FastAPI를 띄우기 전에 테스트를 고친다.

### STEP 3. AWS identity 확인

```bash
# [확인]
AWS_PAGER="" aws sts get-caller-identity --output json
```

기대 (이 개발 EC2):

```text
Account 115019372648
Arn ...assumed-role/AwsFaceDetectionKioskRole/i-060ed51daa9abc84f
```

실패 `NoCredentialsError` / `ExpiredToken`:
로컬 PC라면 `AWS_PROFILE` 또는 `~/.aws/credentials`를 확인한다.
**access key를 레포/.env에 넣지 말 것.**

### STEP 4. 필요한 AWS 권한 확인

```bash
# [확인]
AWS_PAGER="" aws iam list-role-policies --role-name AwsFaceDetectionKioskRole

AWS_PAGER="" aws iam get-role-policy \
  --role-name AwsFaceDetectionKioskRole \
  --policy-name AwsFaceDetectionDeployS3Policy

# CompareFaces가 있는지 모든 inline 문서를 본다.
# 2026-08-13: 없음.

# [확인] 실제 API (작은 잘못된 JPEG여도 권한 여부는 판별된다)
python3 - <<'PY'
import boto3
from botocore.exceptions import ClientError
c = boto3.client("rekognition", region_name="ap-northeast-2")
tiny = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffd9"
)
try:
    c.compare_faces(
        SourceImage={"Bytes": tiny},
        TargetImage={"Bytes": tiny},
        SimilarityThreshold=0,
    )
    print("unexpected success")
except ClientError as e:
    err = (e.response or {}).get("Error", {})
    print(err.get("Code"), (err.get("Message") or "")[:200])
PY
```

기대 (권한 추가 전): `AccessDeniedException` + `rekognition:CompareFaces`.

### STEP 5. 필요한 권한 추가

CompareFaces가 AccessDenied일 때만.

```bash
# [실행]
AWS_PAGER="" aws iam put-role-policy \
  --role-name AwsFaceDetectionKioskRole \
  --policy-name AwsFaceDetectionKioskRekognitionCompare \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Sid": "KioskCompareFaces",
        "Effect": "Allow",
        "Action": ["rekognition:CompareFaces"],
        "Resource": "*"
      }
    ]
  }'
```

```bash
# [확인]
AWS_PAGER="" aws iam get-role-policy \
  --role-name AwsFaceDetectionKioskRole \
  --policy-name AwsFaceDetectionKioskRekognitionCompare
```

`iam:PutRolePolicy`가 AccessDenied이면 현재 Role로는 추가할 수 없다.
관리자에게 Application Stack Role 또는 별도 정책을 요청한다.
(이 호스트의 `AwsFaceDetectionKioskRole`은 2026-08-13 기준 허용되어 있다.)

### STEP 6. S3 bucket 존재 확인

```bash
# [확인]
AWS_PAGER="" aws s3api head-bucket \
  --bucket aws-face-detection-frames-115019372648-apne2 \
  --region ap-northeast-2
```

- 성공 → STEP 8로
- 404 Not Found → STEP 7
- 403 AccessDenied → 버킷은 다른 계정/정책 문제. 이름을 바꿔 만들지 말고 IAM을 본다

### STEP 7. S3 bucket 생성

없을 때만.

[13. S3 bucket 생성 방법](#13-s3-bucket-생성-방법)의 `[실행]` 두 명령을 그대로 실행한다.

### STEP 8. S3 permission smoke test

테스트 object는 **마지막에 반드시 삭제**한다. 신분증/얼굴 이미지를 올리지 말 것.

```bash
BUCKET=aws-face-detection-frames-115019372648-apne2
KEY=local-runner-smoke/probe.txt

# [실행] Put
echo "probe" | AWS_PAGER="" aws s3api put-object \
  --bucket "$BUCKET" --key "$KEY" --body /dev/stdin \
  --content-type text/plain

# [확인] Head / Get
AWS_PAGER="" aws s3api head-object --bucket "$BUCKET" --key "$KEY"
AWS_PAGER="" aws s3api get-object --bucket "$BUCKET" --key "$KEY" /tmp/s3-probe-out.txt
# 기대: ContentLength > 0. /tmp 파일에 얼굴 이미지가 아니어야 한다.

# [실행] Delete
AWS_PAGER="" aws s3api delete-object --bucket "$BUCKET" --key "$KEY"

# [확인] 삭제됐는지 (404/NotFound가 정상)
AWS_PAGER="" aws s3api head-object --bucket "$BUCKET" --key "$KEY" || true
rm -f /tmp/s3-probe-out.txt
```

Put AccessDenied → IAM Resource ARN과 `FRAME_S3_BUCKET`이 다르다.
NoSuchBucket → STEP 7을 다시 한다.

### STEP 9. Local 환경변수 설정

이 개발 EC2는 instance role을 쓰므로 AWS 키를 넣지 않는다.

```bash
# [실행]
export AWS_DEFAULT_REGION=ap-northeast-2
export WEBUI_SESSION_HTTPS_ONLY=false
# 필요하면:
# export FRAME_S3_BUCKET=aws-face-detection-frames-115019372648-apne2
# export KIOSK_DB_PATH=/home/ubuntu/workspace/aws-face-detection/web-ui/backend/data/kiosk.sqlite3
```

로그인 secret은 `web-ui/backend/.env`에 이미 있으면 추가 export가 필요 없다.
없으면 STEP의 `.env` 생성 절차를 따른다.

```bash
# [확인] secret 값을 출력하지 말 것
cd web-ui/backend
python - <<'PY'
import config
print("REGION", config.REGION)
print("KIOSK_DB_PATH", config.KIOSK_DB_PATH)
print("FRAME_S3_BUCKET", config.FRAME_S3_BUCKET)
print("SESSION_HTTPS_ONLY", config.SESSION_HTTPS_ONLY)
print("FACE_SIMILARITY_THRESHOLD", config.FACE_SIMILARITY_THRESHOLD)
print("KIOSK_REFERENCE_DELETE_ON_RETRIEVE", config.KIOSK_REFERENCE_DELETE_ON_RETRIEVE)
print("auth_username_set", bool(config.AUTH_USERNAME))
print("auth_hash_set", bool(config.AUTH_PASSWORD_HASH))
print("session_secret_set", bool(config.SESSION_SECRET))
PY
```

### STEP 10. FastAPI 실행

실제 import path는 `web-ui/backend`를 CWD로 두고 `app:app`이다.

권장 (STEP 1에서 backend venv를 activate한 뒤):

```bash
# [실행]
cd ~/workspace/aws-face-detection/web-ui/backend
uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

activate 없이 절대 경로로 실행할 때:

```bash
# [실행]
cd ~/workspace/aws-face-detection/web-ui/backend
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

같은 호스트 브라우저만 쓰면 `--host 127.0.0.1`이 더 좁다.
code-server `/proxy/8080/` 또는 원격 브라우저면 `0.0.0.0`이 필요하다.

**하지 말 것:**

```text
../../.venv/bin/uvicorn     # root venv. ModuleNotFoundError: uvicorn
시스템 python3.14로 uvicorn
Application Stack처럼 127.0.0.1만 bind한 뒤 외부에서 :8080 접속
```

기대 로그 (비밀 없음):

```text
session_cookie_config ... https_only=False secret_configured=True ...
Uvicorn running on http://0.0.0.0:8080
```

### STEP 11. Health check

별도 터미널.

```bash
# [확인]
curl -i http://127.0.0.1:8080/healthz
```

기대:

```text
HTTP/1.1 200 OK
{"status":"ok"}
```

Connection refused → STEP 10 프로세스가 없거나 다른 포트.
Address already in use → 아래 트러블슈팅.

### STEP 12. Browser 접속

Route: **`GET /kiosk`** → `web-ui/kiosk.html`.

| 환경 | URL |
| ---- | --- |
| 같은 머신 브라우저 | `http://127.0.0.1:8080/kiosk` |
| code-server proxy | `https://<code-server-host>/proxy/8080/kiosk` (`kiosk.js`가 `/proxy/<port>` prefix를 붙임) |
| Application Stack | `https://<ApplicationElasticIp>/kiosk` (Nginx TLS, FastAPI는 loopback) |

로그인: 키오스크는 담당자 잠금 해제(`POST /api/login`) 후에만 동작한다.
`localhost`와 `127.0.0.1`을 섞지 말 것. 쿠키 호스트가 달라 401이 난다.

운영자 UI는 `http://127.0.0.1:8080/` 이다. Kiosk E2E와 섞지 말 것.

### STEP 13. STORE E2E

브라우저 Network에서 순서:

```text
POST /api/login
GET  /api/me
POST /api/kiosk/store/face-verify
GET  /api/kiosk/lockers
POST /api/kiosk/store/reserve
POST /api/kiosk/store/complete
```

있으면 안 되는 요청:

```text
POST /api/capture-frame
targetFrameId
captureId
/face-compare
/enrichedframe
```

face-verify 실패 (`SIMILARITY_BELOW_THRESHOLD`, `NO_FACE_IN_SOURCE_OR_TARGET`,
`ACCESS_DENIED`)면 complete까지 가지 않는 것이 정상이다.

### STEP 14. STORE 후 DB 확인

브라우저에 표시된 `transactionId`를 쓴다. 보관번호/얼굴은 로그에 남기지 말 것.

```bash
# [확인]
python3 - <<'PY'
import sqlite3, sys
tid = sys.argv[1]
con = sqlite3.connect(
    "/home/ubuntu/workspace/aws-face-detection/web-ui/backend/data/kiosk.sqlite3"
)
con.row_factory = sqlite3.Row
row = con.execute(
    "SELECT transaction_id, locker_id, status, retrieval_code IS NOT NULL AS has_code, "
    "reference_frame_id, reference_face_s3_key, "
    "reference_face_created_at IS NOT NULL AS has_created, "
    "reference_face_deleted_at "
    "FROM transactions WHERE transaction_id=?",
    (tid,),
).fetchone()
print(dict(row) if row else "NOT FOUND")
PY '<transaction_id>'
```

기대 (STORE 직후):

```text
status = STORED
has_code = 1
reference_face_s3_key = locker-references/<transaction_id>/reference.jpg
has_created = 1
reference_face_deleted_at = None
reference_frame_id = None     # 신규 경로
```

### STEP 15. STORE 후 S3 확인

```bash
# [확인]
TID='<transaction_id>'
AWS_PAGER="" aws s3api list-objects-v2 \
  --bucket aws-face-detection-frames-115019372648-apne2 \
  --prefix "locker-references/${TID}/"

AWS_PAGER="" aws s3api head-object \
  --bucket aws-face-detection-frames-115019372648-apne2 \
  --key "locker-references/${TID}/reference.jpg"
```

기대: 그 prefix 아래 **object 한 개** `reference.jpg`.
`frames/` 아래에 같은 거래 파일이 생기면 잘못된 경로다.

### STEP 16. RETRIEVE E2E

Network:

```text
POST /api/kiosk/retrieve/start     # retrievalCode + faceImageBase64
POST /api/kiosk/retrieve/complete
```

있으면 안 되는 것:

```text
POST /api/capture-frame
S3 frames/ 에 FRAME-B PutObject
DynamoDB PutItem
```

`retrieve/start`는 얼굴 비교가 성공할 때만 `STORED → RETRIEVING`으로 바꾼다.
코드만으로 문을 여는 API는 없다.

### STEP 17. RETRIEVE 후 DB/S3 확인

```bash
# [확인] DB
python3 - <<'PY'
import sqlite3, sys
tid = sys.argv[1]
con = sqlite3.connect(
    "/home/ubuntu/workspace/aws-face-detection/web-ui/backend/data/kiosk.sqlite3"
)
con.row_factory = sqlite3.Row
print(dict(con.execute(
    "SELECT status, reference_face_deleted_at IS NOT NULL AS deleted "
    "FROM transactions WHERE transaction_id=?",
    (tid,),
).fetchone()))
PY '<transaction_id>'

# [확인] S3 — reference가 없어야 한다
AWS_PAGER="" aws s3api head-object \
  --bucket aws-face-detection-frames-115019372648-apne2 \
  --key "locker-references/<transaction_id>/reference.jpg" || true
```

기대 (`KIOSK_REFERENCE_DELETE_ON_RETRIEVE=true`, retention 0):

```text
status = RETRIEVED
deleted = 1
head-object → 404 / Not Found
```

---

## 17. 명령어 Quick Start (복붙용)

[1. Quick Start](#1-quick-start)와 같다. 위에서 실행 순서를 이미 따라왔으면
이 절은 치트시트다.

### 확인 명령어

```bash
AWS_PAGER="" aws sts get-caller-identity
AWS_PAGER="" aws iam list-role-policies --role-name AwsFaceDetectionKioskRole
AWS_PAGER="" aws s3api head-bucket \
  --bucket aws-face-detection-frames-115019372648-apne2 \
  --region ap-northeast-2
cd ~/workspace/aws-face-detection
web-ui/backend/.venv/bin/python -m pytest -q
```

### 최초 1회 실행 명령어

```bash
# CompareFaces가 AccessDenied일 때만
AWS_PAGER="" aws iam put-role-policy --role-name AwsFaceDetectionKioskRole \
  --policy-name AwsFaceDetectionKioskRekognitionCompare \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Sid":"KioskCompareFaces","Effect":"Allow","Action":["rekognition:CompareFaces"],"Resource":"*"}]}'

# frames 버킷 404일 때만
AWS_PAGER="" aws s3api create-bucket \
  --bucket aws-face-detection-frames-115019372648-apne2 \
  --region ap-northeast-2 \
  --create-bucket-configuration LocationConstraint=ap-northeast-2
AWS_PAGER="" aws s3api put-public-access-block \
  --bucket aws-face-detection-frames-115019372648-apne2 \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

### 매번 실행할 명령어

```bash
cd ~/workspace/aws-face-detection
source web-ui/backend/.venv/bin/activate
export AWS_DEFAULT_REGION=ap-northeast-2
export WEBUI_SESSION_HTTPS_ONLY=false
cd web-ui/backend
uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

### 실행 후 확인

```bash
curl -i http://127.0.0.1:8080/healthz
# 브라우저: http://127.0.0.1:8080/kiosk
```

---

## 18. Stack 실행 시와 Local 실행 시 차이

현재 CFN 파일 기준. 이 계정에는 두 스택 모두 살아 있지 않다.

| 항목 | Stack 없이 Kiosk | Application Stack (`aws-infra-ec2-cfn.yaml`) | Data Stack (`aws-infra-cfn.yaml`) |
| ---- | ---------------- | -------------------------------------------- | --------------------------------- |
| FastAPI | `uvicorn` 직접, `:8080` | systemd `kiosk-fastapi.service`, `127.0.0.1:${WEBUI_PORT}` | 생성하지 않음 |
| SQLite | `web-ui/backend/data/kiosk.sqlite3` | encrypted EBS `/data/kiosk/kiosk.db` | 없음 |
| Rekognition CompareFaces | FastAPI가 직접 호출 | FastAPI가 직접 호출 (Role에 정책 있음) | facecompare Lambda도 CompareFaces 보유 |
| reference S3 | 수동 생성한 frames 버킷 | 파라미터 `FrameS3BucketNameParameter`로 연결 | `FrameS3Bucket` 리소스 생성 |
| Kinesis | X | 운영자 `/api/capture-frame`이 접근 | `FrameStream` 생성 |
| DynamoDB EnrichedFrame | X | 운영자/KPI가 읽을 수 있음 (Role에 GetItem/Query) | 테이블 생성 |
| imageprocessor | X | 운영자 DetectLabels 토글만 | Function + EventSourceMapping |
| facecompare Lambda | X | 운영자 invoke 권한은 Role에 있음. Kiosk는 미사용 | Function + API GW `/face-compare` |
| API Gateway | X | 운영자 `/api/config`가 조회 | RestApi + usage plan + API key |
| Nginx | X (선택) | O (`aws-infra/nginx/application.conf`) | 없음 |
| SSM | 이 개발 EC2는 별개 | O (`AmazonSSMManagedInstanceCore`) | 없음 |
| EIP | 없음 (개발 EC2 public DNS는 별개) | `ApplicationElasticIp` | 없음 |
| KPI | X | loopback `:8000`, `/dashboard/` | 로그/메트릭 소스 |
| Instance Role | `AwsFaceDetectionKioskRole` (현재) | CFN 생성 `ApplicationInstanceRole` (이름 자동) | Lambda execution roles |
| 현재 이 계정 상태 | 이 문서가 다루는 호스트 | `DELETE_COMPLETE` | 존재하지 않음 |

---

## 19. 현재 Stack의 역할

### Data Stack — `aws-infra/aws-infra-cfn.yaml`

전형적인 이름: `video-analyzer-stack` (`config/global-params.json`).

생성하는 리소스 (템플릿 logical ID):

```text
FrameS3Bucket
ImageProcessorLambdaExecutionRole + ImageProcessorPolicy
FrameFetcherLambdaExecutionRole + FrameFetcherPolicy
FaceCompareLambdaExecutionRole + FaceComparePolicy
FrameStream                          (default name FrameStream, shard 1)
ImageProcessorLambda                 (function name imageprocessor)
EventSourceMapping                   (Kinesis → imageprocessor)
FrameFetcherLambda                   (function name framefetcher)
FaceCompareLambda                    (function name facecompare)
EnrichedFrameTable                   (default name EnrichedFrame + GSI + TTL expire_at)
VidAnalyzerRestApi
EnrichedFrameResource + GET + OPTIONS
FaceCompareResource + POST + OPTIONS
VidAnalyzerApiDeployment + DevStage
DevUsagePlan + VidAnalyzerApiKey + DevUsagePlanKey
LambdaInvokePermissionSTAR / GET
FaceCompareLambdaInvokePermissionPOST
```

Kiosk direct Rekognition 리팩터 이후에도 Data Stack이 남는 이유:

```text
Data Stack
  → 기존 operator / camera frame processing
  → Kinesis, imageprocessor, EnrichedFrame, frames/, API Gateway, facecompare
  → 운영자 UI와 capture client가 여전히 이 경로를 사용

Kiosk는 더 이상 이 스택 없이 STORE/RETRIEVE를 수행할 수 있다.
```

2026-08-13 CLI:

```text
video-analyzer-stack: 존재하지 않음
imageprocessor / facecompare: Function not found
FrameStream: not found
EnrichedFrame: table not found
frames 버킷: 404
```

### Application Stack — `aws-infra/aws-infra-ec2-cfn.yaml`

전형적인 이름: `video-analyzer-ec2-stack`.

```text
ApplicationSecurityGroup          (:443, :80. SSH 없음)
ApplicationInstanceRole           (RoleName 생략, SSM managed policy + inline)
ApplicationInstanceProfile
ApplicationInstance               (AL2023, t3.small 기본)
ApplicationDataVolume             (encrypted gp3, Purpose=kiosk-sqlite)
ApplicationDataVolumeAttachment   (/dev/xvdf)
ApplicationElasticIp + Association
SchedulerRole + 4 schedules       (EnableSchedulerParameter=true 일 때만)
```

UserData가 artifact에서 bootstrap을 받아 설치한다.

```text
Nginx TLS
FastAPI loopback
KPI loopback
/etc/webui.env
KIOSK_DB_PATH=/data/kiosk/kiosk.db
```

2026-08-13: `video-analyzer-ec2-stack`는 `DELETE_COMPLETE`만 있다.

---

## 20. 트러블슈팅

### AccessDenied: rekognition:CompareFaces

- 증상: `store/face-verify` 또는 `retrieve/start`가 `ACCESS_DENIED` / 502.
  CLI probe도 `AccessDeniedException`.
- 원인: 현재 `AwsFaceDetectionKioskRole` inline policy에 CompareFaces가 없다.
  Application Stack 템플릿의 권한과 혼동하지 말 것.
- 확인: STEP 4의 CompareFaces probe, `list-role-policies` / `get-role-policy`.
- 해결: STEP 5 임시 정책. 장기적으로는 Application Stack Role.

### S3 HeadBucket 404

- 증상: `head-bucket` → `404 Not Found`. STORE complete가 `AWS_API_ERROR` / `ACCESS_DENIED`.
- 원인: frames 버킷 리소스가 없다. Data Stack 미배포.
- 확인: STEP 6.
- 해결: STEP 7 수동 생성. 또는 Data Stack 배포 (이 문서 범위 밖).

### S3 AccessDenied (HeadBucket 403)

- 증상: 버킷은 있는데 호출자가 거부됨.
- 원인: 다른 계정 버킷, 또는 `FRAME_S3_BUCKET`이 IAM 허용 ARN과 다름.
- 확인: `get-caller-identity`, `FRAME_S3_BUCKET`, DeployS3Policy Resource.
- 해결: 허용된 이름만 사용. 임의 버킷명을 만들지 말 것.

### S3 PutObject AccessDenied

- 증상: `save_reference_face` → `ACCESS_DENIED`.
- 원인: 버킷/키 ARN이 `s3:*` 허용 밖. 또는 버킷 정책 deny.
- 확인: STEP 8 smoke Put.
- 해결: 버킷 이름을 코드 기본값으로. 키는 `locker-references/` 아래.

### NoCredentialsError

- 증상: 502 `AWS credentials unavailable`. boto3 `NoCredentialsError`.
- 원인: instance role/프로필/키가 없음.
- 확인: `aws sts get-caller-identity`. `AWS_EC2_METADATA_DISABLED`가 켜져 있지 않은지.
- 해결: 이 EC2는 IMDS role을 쓴다. 로컬 PC는 `AWS_PROFILE`. 키를 git에 넣지 말 것.

### ResourceNotFound

- 증상: 운영자 `/api/capture-frame` 502 `AWS resource not found`.
  `DescribeStreamSummary` → FrameStream not found.
- 원인: Data Stack이 없다. **Kiosk local runner는 이 오류를 무시해도 된다.**
- 확인: `aws kinesis describe-stream-summary --stream-name FrameStream`.
- 해결: 운영자 파이프라인이 필요할 때만 Data Stack 배포.

### Connection refused :8080

- 증상: `curl http://127.0.0.1:8080/healthz` 실패.
- 원인: uvicorn 미실행, 다른 포트, `--host` 불일치.
- 확인: 실행 터미널 로그. `ss -lntp | grep 8080`.
- 해결: STEP 10.

### Address already in use

- 증상: uvicorn이 `bind` 실패.
- 원인: 다른 프로세스가 8080 사용.
- 확인: `ss -lntp | grep 8080`.
- 해결: 기존 프로세스 종료, 또는 `--port 8081` 후 URL도 변경.

### STORE_FACE_MISMATCH

- 증상: `POST /api/kiosk/store/complete` → 403 `STORE_FACE_MISMATCH`.
- 원인: complete의 `verifiedFaceImageBase64` SHA-256이 세션
  `verified_store_face_hash`와 다름. 다른 사진을 보냈거나 재인코딩됨.
- 확인: 브라우저가 face-verify 때 쓴 FRAME-A bytes를 그대로 complete에 보내는지.
  `kiosk.js` `completeStore`가 메모리의 검증된 프레임을 쓰는지.
- 해결: STORE를 처음부터. face-verify 후 사진을 바꾸지 말 것.
  검증 유효 시간(`KIOSK_STORE_VERIFICATION_SECONDS`, 기본 120) 만료면
  `STORE_FACE_VERIFICATION_EXPIRED`.

### REFERENCE_FACE_MISSING

- 증상: `retrieve/start` 200 body `reason=REFERENCE_FACE_MISSING`, status STORED.
- 원인: S3에 `locker-references/<tid>/reference.jpg`가 없거나
  DB `reference_face_deleted_at`이 이미 채워짐.
- 확인: STEP 15 `head-object`. DB `reference_face_deleted_at`.
- 해결: 그 거래는 찾기 불가. 새로 STORE. 버킷 삭제/오타/`FRAME_S3_BUCKET` 불일치 확인.

### 로그인 200 후 kiosk API 401

- 원인: `WEBUI_SESSION_HTTPS_ONLY=true` + HTTP, 또는 `localhost`/`127.0.0.1` 혼용.
- 확인: FastAPI 시작 로그 `https_only=`. 브라우저 Application → cookie `Secure`.
- 해결: `WEBUI_SESSION_HTTPS_ONLY=false` 후 uvicorn 재시작. 호스트를 하나로.

---

## 21. 보안 주의사항

Local/dev라도 동일하다.

```text
신분증 이미지 persistent 저장 금지 (디스크, SQLite, 로그)
FRAME-B 저장 금지 (S3 frames/ 포함)
FRAME-A는 STORE 완료 후 locker-references/<tid>/reference.jpg 한 장만
RETRIEVED 후 reference 삭제 (기본 KIOSK_REFERENCE_DELETE_ON_RETRIEVE=true)

Base64 / raw bytes 로그 금지
AWS access key 문서·.env 커밋 금지
session secret / password hash 문서 기록 금지
S3 public access 금지
브라우저에 reference S3 key를 내려주지 않음
보관번호만으로 보관함을 여는 API 없음
```

수동 IAM (`put-role-policy`)은 테스트 후 삭제하고, 남는 권한은
`aws-infra/aws-infra-ec2-cfn.yaml`으로만 유지한다.

이 문서 작성 시 `web-ui/backend/.env`를 열어 키 이름만 확인했다. 값은 복사하지 않았다.

---

## 22. 이 문서를 쓰기 위해 실행한 조사

| 명령 | 결과 요약 |
| ---- | ---------- |
| `aws sts get-caller-identity` | Account 115019372648, Role `AwsFaceDetectionKioskRole`, instance `i-060ed51daa9abc84f` |
| IMDSv2 instance-id / iam / region | 위와 동일, `ap-northeast-2` |
| `iam list-role-policies` | inline 6개 |
| `iam get-role-policy` × 6 | CompareFaces 없음. frames/lambda 버킷 `s3:*`. `iam:PutRolePolicy` 있음 |
| `iam list-attached-role-policies` | AccessDenied → attached는 확인 불가 |
| `iam simulate-principal-policy` | AccessDenied |
| Rekognition `compare_faces` probe | `AccessDeniedException` |
| `s3api head-bucket` lambda | 존재, public access block ON |
| `s3api head-bucket` frames | 404 Not Found |
| `cloudformation describe-stacks video-analyzer-stack` | 존재하지 않음 |
| `list-stacks` application | `video-analyzer-ec2-stack` DELETE_COMPLETE만 |
| `lambda get-function` imageprocessor/facecompare | ResourceNotFound |
| `kinesis describe-stream-summary FrameStream` | ResourceNotFound |
| `dynamodb describe-table EnrichedFrame` | ResourceNotFound |
| `config.py` import | `KIOSK_DB_PATH=.../web-ui/backend/data/kiosk.sqlite3` |
| SQLite `PRAGMA table_info` | lockers 4컬럼, transactions 16컬럼 |
| `pytest -q` | **114 passed** |
| kiosk 4파일 pytest | **83 passed** |
| `/etc/webui.env`, `/data/kiosk` | 없음 |

---

## 23. 자가 점검 (문서 품질)

1. Kiosk local runner가 Kinesis가 필요하다고 쓰지 않았다. 운영자 UI만 Kinesis.
2. DynamoDB를 Kiosk DB라고 쓰지 않았다. Kiosk DB는 SQLite만.
3. SQLite 컬럼은 `initialize()` + 실제 `PRAGMA`와 같다.
4. `reference_frame_id` / `reference_face_s3_key`는 legacy로 표시했고,
   S3 경로 source of truth는 `transaction_id` 계산 키이다.
5. IAM Role 이름은 CLI `AwsFaceDetectionKioskRole`과 같다.
6. CFN `ApplicationInstanceRole`의 CompareFaces와 현재 Role 권한을 섞지 않았다.
7. frames 버킷은 404로 기록했다. 존재한다고 쓰지 않았다.
8. `[실행]` / `[확인]`을 분리했다.
9. STEP 0–17을 위에서 아래로 따라갈 수 있다.
10. password, session secret, access key, cookie 값을 넣지 않았다.
