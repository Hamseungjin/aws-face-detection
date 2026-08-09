# 서울 AI 안심 보관함 키오스크 UI — Phase 1.2 + Phase 2A

## 개요

시민용 키오스크는 기존 운영자 대시보드를 변경하지 않고 별도 경로로 제공됩니다.

- 운영자/디버그 대시보드: `GET /`
- 시민용 키오스크: `GET /kiosk`
- 프런트엔드: 빌드 없는 Vue 2 + JavaScript + Axios + CSS
- 권장 화면: 세로형 1080×1920
- 거래 저장소: 동일 FastAPI 호스트의 SQLite 파일

Phase 1의 브라우저별 고정 보관함 목록은 여러 브라우저가 같은 보관함을 동시에
선택할 수 있었습니다. Phase 1.1은 FastAPI에 작은 `sqlite3` 거래 계층을 추가해,
같은 FastAPI 서버에 접속한 키오스크 브라우저들이 보관함 상태와 완료된 거래를
공유하고 보관함을 원자적으로 예약하도록 합니다.

```text
Kiosk browser A ─┐
Kiosk browser B ─┼──> Same FastAPI server ──> kiosk.sqlite3
Kiosk browser C ─┘
```

## 상태와 화면 흐름

화면은 하나의 명시적인 `state`와 허용 전이 목록으로 제어됩니다.

공통 본인 확인:

```text
ATTRACT → MODE_SELECT → CONSENT → ID_CAPTURE → FACE_CAPTURE
→ WAITING_FOR_FRAME → VERIFYING
```

STORE:

```text
VERIFYING → LOCKER_SELECT → LOCKER_RESERVING → PAYMENT
→ STORE_COMPLETING → LOCKER_OPENING → COMPLETE → ATTRACT
```

진행 표시는 `본인 인증 → 보관함 선택 → 결제 → 보관`입니다.

RETRIEVE:

```text
VERIFYING → RETRIEVAL_CODE → RETRIEVE_CONFIRM
→ RETRIEVE_SETTLEMENT → LOCKER_OPENING → COMPLETE → ATTRACT
```

진행 표시는 `본인 인증 → 보관번호 확인 → 정산 → 찾기`입니다. RETRIEVE는 사용 가능한
보관함 목록을 보여 주지 않으며 `mock_payment_id`도 요구하지 않습니다.

## SQLite 경로와 설정

`KIOSK_DB_PATH`로 데이터베이스 파일을 지정합니다. 지정하지 않으면 다음 개발 기본값을
사용하며 디렉터리와 파일은 자동 생성됩니다.

```text
web-ui/backend/data/kiosk.sqlite3
```

`web-ui/backend/data/`는 Git에서 제외됩니다. 단일 EC2 배포에서 재시작 후에도 데이터를
유지하려면 예를 들어 다음처럼 호스트의 지속 경로를 설정할 수 있습니다.

```text
KIOSK_DB_PATH=/var/lib/webui/kiosk.sqlite3
KIOSK_RESERVATION_SECONDS=120
KIOSK_RETRIEVAL_SECONDS=120
KIOSK_RETRIEVAL_MAX_FAILURES=5
KIOSK_RETRIEVAL_RATE_WINDOW_SECONDS=60
```

이 단계에서는 배포/bootstrap이나 EC2 볼륨 구성을 자동 변경하지 않습니다.

각 SQLite 연결은 다음을 사용합니다.

- `PRAGMA foreign_keys = ON`
- `PRAGMA busy_timeout = 5000`
- WAL journal mode
- 쓰기 작업별 짧은 `BEGIN IMMEDIATE` 트랜잭션
- 모든 런타임 값에 parameterized SQL

## 데이터베이스 스키마

### `lockers`

| 열 | 의미 |
|---|---|
| `locker_id` | `01`~`12` 보관함 식별자, 기본 키 |
| `status` | `AVAILABLE`, `RESERVED`, `OCCUPIED`, `DISABLED` |
| `active_transaction_id` | 현재 활성 거래, 없으면 `NULL` |
| `updated_at` | UTC ISO-8601 변경 시각 |

### `transactions`

| 열 | 의미 |
|---|---|
| `transaction_id` | 내부 UUID |
| `retrieval_code` | 고객용 8자리 보관번호, `UNIQUE`, STORE 완료 전에는 `NULL` |
| `locker_id` | 거래의 보관함 |
| `status` | `RESERVED`, `STORED`, `RETRIEVING`, `RETRIEVED`, `CANCELLED`, `EXPIRED` |
| `amount` | STORE 데모 결제 금액(원) |
| `mock_payment_id` | `PAY-` + 6자리 16진수 데모 결제 참조, `UNIQUE` |
| 시간 열 | 생성, 예약, 보관, 찾기 시작/완료, 예약 만료 시각 |

활성 상태(`RESERVED`, `STORED`, `RETRIEVING`)에는 보관함당 거래 하나만 존재하도록
partial unique index도 둡니다.

초기 데이터는 `INSERT OR IGNORE`로 넣으므로 재시작/재초기화가 기존 상태를 덮어쓰지
않습니다.

Phase 1.2는 기존 `retrieval_started_at`을 lease 시각으로 재사용하므로 새 열이나 파괴적
마이그레이션이 없습니다. 초기화는 계속 `CREATE ... IF NOT EXISTS`와 `INSERT OR IGNORE`만
사용하며 기존 거래/seed 상태를 삭제하거나 다시 만들지 않습니다.

- AVAILABLE: 01, 02, 04, 05, 07, 09, 10, 12
- OCCUPIED: 03, 06, 11
- DISABLED: 08

03, 06, 11은 다른 이용자의 기존 점유를 표현할 뿐, 하드코딩된 보관번호나 사용자로
찾을 수 없습니다.

## 인증된 FastAPI API

모든 경로는 기존 서명 세션을 확인합니다. SQLite 파일 자체는 정적으로 노출하지 않습니다.

| Method | Path | 역할 |
|---|---|---|
| GET | `/api/kiosk/lockers` | 만료 예약 정리 후 공유 보관함 상태 조회 |
| POST | `/api/kiosk/store/reserve` | AVAILABLE 보관함 원자적 예약 |
| POST | `/api/kiosk/store/cancel` | RESERVED 거래 취소/보관함 해제 |
| POST | `/api/kiosk/store/complete` | RESERVED → STORED 또는 같은 STORED 결과의 멱등 재응답 |
| GET | `/api/kiosk/transactions/{transaction_id}` | 해당 내부 거래 ID의 복구용 현재 상태 조회 |
| POST | `/api/kiosk/retrieve/start` | 보관번호 거래를 STORED → RETRIEVING으로 원자적 잠금 |
| POST | `/api/kiosk/retrieve/recover` | 응답 유실 시 bearer 보관번호로 STORED/RETRIEVING 상태를 좁게 복구 |
| POST | `/api/kiosk/retrieve/cancel` | RETRIEVING → STORED, 보관함은 OCCUPIED 유지 |
| POST | `/api/kiosk/retrieve/complete` | RETRIEVING → RETRIEVED, 보관함을 AVAILABLE로 해제 |

존재하지 않거나 다른 키오스크가 먼저 예약한 보관함에는 HTTP 409와
`LOCKER_NOT_AVAILABLE`을 반환합니다. 유효하지 않거나 이미 찾는 중/찾기 완료된 보관번호는
관련 거래 정보를 노출하지 않고 모두 일반적인 `RETRIEVAL_UNAVAILABLE`로 응답합니다.

## STORE 거래

1. 얼굴 확인 성공 후 서버 보관함 목록을 조회합니다.
2. AVAILABLE 보관함만 선택할 수 있습니다.
3. 선택 완료 시 서버가 내부 UUID를 만들고, 한 SQLite 쓰기 트랜잭션 안에서
   `AVAILABLE → RESERVED`와 `RESERVED` 거래 생성을 수행합니다.
4. 두 브라우저가 같은 보관함을 요청하면 `BEGIN IMMEDIATE`, 조건부 UPDATE,
   활성 거래 unique index 때문에 정확히 하나만 성공합니다.
5. 2,000원 모의 결제가 성공하면 `/api/kiosk/store/complete`가 거래/보관함 연결을
   다시 검증하고 `RESERVED → STORED`, 보관함 `RESERVED → OCCUPIED`를 적용합니다.
6. 이때 8자리 `retrieval_code`와 `PAY-XXXXXX` 형식 `mock_payment_id`를 생성합니다.
7. 문 열림은 모의 처리하고 완료 화면에서 보관번호를 가장 크게 표시합니다.

`/api/kiosk/store/complete`는 같은 `transactionId`에 대해 멱등입니다. 첫 요청이 SQLite
커밋까지 성공했지만 HTTP 응답이 유실되어 브라우저가 같은 요청을 반복하면 새 번호나 새 거래를
만들지 않고 이미 저장된 `retrievalCode`, `mockPaymentId`, 금액, 보관함과 `STORED` 상태를
그대로 반환합니다. `CANCELLED`, `EXPIRED`, `RETRIEVED` 같은 잘못된 상태에서는 이 동작을
허용하지 않습니다.

`retrieval_code != mock_payment_id`입니다.

- `retrieval_code`: 고객이 물품을 찾을 때 입력하는 거래 식별 번호
- `mock_payment_id`: 실제 카드 승인이 아닌 데모 결제 참조
- `transaction_id`: 내부 UUID이며 고객에게 기억하거나 입력하도록 요구하지 않음

SQLite의 `UNIQUE` 제약과 재생성 루프가 보관번호/데모 결제번호 충돌을 처리합니다.

## 예약 취소와 만료

예약 기본 유효 시간은 120초입니다. 백그라운드 스케줄러는 두지 않습니다. 보관함 목록,
예약, STORE 완료, RETRIEVE 시작, 거래 상태 조회 등 관련 DB 작업 시작 시 만료된 예약을
정리합니다.

```text
transaction: RESERVED → EXPIRED
locker:      RESERVED → AVAILABLE
active_transaction_id → NULL
```

사용자가 처음 화면으로 돌아가거나, 유휴 초기화가 일어나거나, 예약 뒤 보관함을 다시
선택하는 정상 경로에서는 서버 취소 성공 또는 상태 조회로 안전한 종료 상태를 확인한 뒤에만
브라우저의 `transactionId`를 지웁니다. 취소 요청과 상태 조회가 모두 모호하게 실패하면
`거래 상태를 정리하지 못했습니다`와 `다시 시도`를 표시하고 메모리의 거래 상태를 유지합니다.
이미 STORED인 거래는 초기화가 취소하거나 삭제하지 않습니다.

페이지 종료의 `sendBeacon` 취소는 계속 best-effort일 뿐 성공을 보장하지 않습니다. 종료 중
동기 요청을 강제하지 않습니다. 전송에 실패한 RESERVED 거래는 예약 만료가 회수하고,
RETRIEVING 거래는 아래 찾기 lease 만료가 회수합니다.

## RETRIEVE 거래

1. 얼굴 확인 성공 후 8자리 `retrieval_code`를 입력합니다.
2. `/api/kiosk/retrieve/start`는 코드와 `STORED` 상태, OCCUPIED 보관함, 활성 거래 관계를
   한 쓰기 트랜잭션에서 검증합니다.
3. 성공 시 `STORED → RETRIEVING`으로 변경하여 다른 키오스크의 동시 찾기를 막습니다.
4. 확인 화면은 찾을 보관함 번호만 보여 줍니다.
5. 데모 기본 추가 정산 금액은 0원이며 “추가 결제 없이 보관함을 열 수 있습니다”라고
   표시합니다. 정산 어댑터는 향후 유료 응답을 받을 수 있도록 별도 경계로 유지합니다.
6. 모의 문 열림 뒤 `/api/kiosk/retrieve/complete`가 관계를 다시 확인하고
   `RETRIEVING → RETRIEVED`, 보관함 `OCCUPIED → AVAILABLE`, 활성 거래 `NULL`을 적용합니다.
7. 취소 시 거래만 `RETRIEVING → STORED`로 되돌리고 보관함은 OCCUPIED로 유지합니다.

`KIOSK_RETRIEVAL_SECONDS`(기본 120초)는 `retrieval_started_at`부터 계산하는 찾기 lease입니다.
관련 DB 작업에서 `RETRIEVING` lease가 만료된 것을 발견하면 다음처럼 복구합니다.

```text
transaction: RETRIEVING → STORED
retrieval_started_at → NULL
locker:      OCCUPIED 유지
active_transaction_id: 같은 거래 유지
```

따라서 RESERVED 만료는 빈 보관함을 AVAILABLE로 돌려놓지만, RETRIEVING 만료는 물품이 여전히
들어 있다고 보고 보관함을 OCCUPIED로 유지한다는 차이가 있습니다. 복구 뒤 같은 보관번호로
찾기를 다시 시작할 수 있습니다.

완료 뒤 같은 FastAPI 서버에 접속한 다른 브라우저의 다음 목록 조회에도 해제된 보관함이
AVAILABLE로 보입니다.

## Phase 1.2 거래 신뢰성 복구

브라우저가 STORE 완료 응답을 받지 못하면 즉시 실패로 단정하지 않고
`GET /api/kiosk/transactions/{transaction_id}`로 상태를 확인합니다. `STORED`이면 SQLite에
저장된 동일 보관번호와 데모 결제번호를 복구하여 정상 완료 화면을 계속합니다. RESERVED 등
완료가 확인되지 않거나 상태 조회도 실패하면 거래 ID를 유지한 채 재시도 화면을 표시하므로
새 STORE 예약을 만들지 않습니다.

RETRIEVE 시작 응답이 모호하게 유실되면 입력한 보관번호로
`POST /api/kiosk/retrieve/recover`를 호출합니다. 서버 상태가 RETRIEVING이면 기존 거래 ID와
보관함만 반환해 같은 흐름을 계속합니다. STORED이면 내부 거래 정보는 반환하지 않고 브라우저가
정상 시작을 다시 요청할 수 있음을 알립니다. 이 경로는 일반 `/retrieve/start`를 멱등한 거래
claim으로 바꾸지 않으므로 동시 찾기 보호는 유지됩니다.

거래 상태 API는 요청한 단일 `transaction_id`만 parameterized query로 조회하며 상태에 맞는
필드만 반환합니다. 예를 들어 RESERVED에는 보관번호/결제번호가 없고, RETRIEVING에는
보관번호를 다시 내보내지 않습니다. 존재하지 않는 ID는 일반적인 404입니다.

보관번호는 현재 안정적인 사용자 신원이 아니라 **bearer 방식의 데모 비밀값**입니다. 따라서
보관번호를 가진 사람은 찾기 시작/복구를 요청할 수 있으며, 이 복구 API도 그 보안 모델을
강화하지 않습니다. 운영 환경에서는 안정적인 사용자 신원과 재인증을 거래에 결합해야 합니다.

## 보관번호 검증과 최소 rate limit

서버는 `retrievalCode`가 ASCII 숫자로만 된 정확히 8자리인지 조회 전에 검증합니다. 짧거나,
길거나, 영문/기호/유니코드 숫자가 포함된 값은 안전한 HTTP 400으로 거부합니다. 새 복구용
`transaction_id`도 빈 값과 128자를 넘는 값을 거부합니다.

정상 형식이지만 실패한 보관번호 조회는 인증된 브라우저 세션별로 60초 동안 최대 5회까지
기록합니다. 5회 실패 뒤 다음 요청은 HTTP 429, `RETRIEVAL_RATE_LIMITED`, `Retry-After`로
일시 거부하며 UI에는 `보관번호 확인 요청이 많습니다. 잠시 후 다시 시도해 주세요.`만
표시합니다. 유효한 시작/복구가 성공하면 해당 세션 실패 기록을 지웁니다.

이 limiter는 메모리에만 있는 **단일 FastAPI 프로세스용 최소 해커톤 보호**입니다. 프로세스
재시작 또는 여러 worker/host 사이에 기록을 공유하지 않으며 분산 공격 방어 수단이 아닙니다.
rate limit에는 얼굴/신분증/생체 데이터를 저장하지 않습니다.

## 본인 확인의 현재 한계

현재 AWS 비교가 확인하는 것은 다음뿐입니다.

> 지금 제시한 ID 사진의 얼굴과 지금 촬영한 얼굴이 일치하는가?

현재 얼굴 비교는 안정적인 사용자 ID나 identity-provider subject ID를 반환하지 않습니다.
따라서 `current face verification != stable user identity`입니다.

이 데모에서:

- 얼굴 확인은 현재 제시한 ID와 현재 사람의 일치만 확인합니다.
- `retrieval_code`가 저장된 보관함 거래를 식별합니다.
- 최초 STORE 당시 신원과 나중 RETRIEVE 당시 신원을 암호학적으로 결합하지 않습니다.
- SQLite가 얼굴만으로 같은 사람의 재방문을 식별한다고 주장하지 않습니다.

운영 시스템은 승인된 모바일 ID/identity-verification 서비스가 제공하는 안정적인 subject ID를
사용하고, 거래를 그 subject에 안전하게 결합해야 합니다.

## Phase 2A: AWS 얼굴 프레임 정확 상관관계

이전 키오스크는 다음처럼 전역 최신 프레임을 선택했습니다.

```text
capture → Kinesis → ImageProcessor → 전역 /enrichedframe timestamp polling
→ target 없는 /face-compare → 전역 최신 frame
```

따라서 키오스크 A가 촬영한 뒤 키오스크 B 프레임이 더 최신으로 처리되면 A의 ID 사진이 B의
카메라 프레임과 비교될 수 있었습니다. Phase 2A의 흐름은 다음과 같습니다.

```text
browser capture
→ FastAPI가 canonical UUIDv4 captureId 생성
→ Kinesis CaptureId
→ ImageProcessor frame_id
→ /face-compare targetFrameId
→ DynamoDB GetItem(frame_id=targetFrameId)
→ 그 item의 S3 frame만 Rekognition TargetImage로 비교
```

아래 네 식별자의 의미는 서로 다릅니다.

| 이름 | 의미 |
|---|---|
| `transactionId` | SQLite 보관함 거래 UUID |
| `captureId` / `targetFrameId` | 카메라 1회 촬영 UUID; 같은 값을 서로 다른 API 이름으로 전달 |
| `retrievalCode` | 고객이 입력하는 8자리 보관번호 |
| `mockPaymentId` | 실제 승인이 아닌 `PAY-XXXXXX` 데모 결제 참조 |

`POST /api/capture-frame` 요청은 기존 `imageBase64`, `frameCount`를 유지합니다. FastAPI가
소문자 16진수와 하이픈으로 된 canonical UUIDv4를 생성하고, 기존 pickle 필드 세 개를
삭제하거나 바꾸지 않은 채 `CaptureId`를 추가합니다.

```python
{
    "ApproximateCaptureTime": 178...,
    "FrameCount": 0,
    "ImageBytes": bytearray(...),
    "CaptureId": "7f5fd075-e711-4fdd-8d2a-ffcd5c6f65b1",
}
```

응답의 `captureId`는 Kinesis가 레코드를 수락했음을 뜻할 뿐 ImageProcessor 완료를 뜻하지
않습니다. `client/video_cap.py`처럼 `CaptureId`가 없는 legacy producer는 계속 지원하며,
ImageProcessor가 이전과 같이 새 UUIDv4 `frame_id`를 생성합니다. `CaptureId`가 있으면 canonical
UUIDv4인지 검증한 후 같은 문자열을 `frame_id`와 S3 파일명에 사용합니다. 잘못된 값은 임의의
DynamoDB 키로 저장하거나 새 ID로 조용히 대체하지 않고 해당 stream record를 거부합니다.

시민 키오스크의 `/face-compare` 요청은 항상 다음 필드를 포함합니다.

```json
{
  "imageBase64": "...",
  "filename": "id.jpg",
  "contentType": "image/jpeg",
  "similarityThreshold": 90,
  "targetFrameId": "7f5fd075-e711-4fdd-8d2a-ffcd5c6f65b1"
}
```

`EnrichedFrame` 테이블의 partition key가 이미 string `frame_id`이므로 exact mode는 GSI나 scan을
사용하지 않고 `GetItem(Key={frame_id: targetFrameId}, ConsistentRead=True)`로 조회합니다. 응답의
`targetFrameId`와 `target.frame_id`로 실제 선택된 프레임을 확인할 수 있으며, exact mode 응답은
S3 bucket/key 같은 내부 AWS 위치를 키오스크에 제공하지 않습니다.

유효한 ID가 아직 DynamoDB에 없으면 HTTP 200 business result
`TARGET_FRAME_NOT_READY`를 반환합니다. 이는 **이 정확한 프레임의 비동기 처리가 아직 끝나지
않았다**는 의미이며 최신 프레임을 대신 사용하라는 의미가 아닙니다. 키오스크는 약 1.5초
간격, 최대 약 15초 동안 동일한 `currentCaptureId`로만 `/face-compare`를 반복합니다. 처리 순서와
관계없이 A는 CAP-A, B는 CAP-B만 조회합니다. 시간 초과 시
`촬영한 얼굴 처리 시간이 초과되었습니다. 다시 촬영해 주세요.`를 표시하고 재촬영을 허용합니다.

`currentCaptureId`는 현재 Vue 메모리에만 있으며 재촬영, 취소/도움 요청, 성공, 거래/유휴 초기화
시 지웁니다. localStorage, sessionStorage, IndexedDB, SQLite에는 저장하지 않습니다. ID/얼굴
이미지와 base64도 SQLite에 저장하지 않습니다.

`TARGET_FRAME_NOT_READY` 반복과 처리 시간 초과는 얼굴 인증 시도 횟수를 소비하지 않습니다.
유사도 통과/미달 또는 얼굴 미검출처럼 실제 Rekognition 비교가 완료된 결과만 1회로 계산하며
최대 실제 비교는 계속 3회입니다. `/face-verify`는 호출하지 않습니다.

`targetFrameId`를 **생략**한 `/face-compare`는 기존 운영자 대시보드와 도구를 위해 현재/이전 달
GSI의 전역 최신 프레임을 선택합니다. 이 경로는 LEGACY 호환 모드이며 시민 키오스크에서는
사용하면 안 됩니다. exact mode에서 ID가 잘못되거나 아직 준비되지 않아도 legacy latest로
fallback하지 않습니다.

`captureId`는 한 촬영을 상관시키는 메타데이터일 뿐 사용자 ID가 아니며, 방문 간 동일 인물을
식별하지 않습니다.

## 의도적으로 SQLite에 저장하지 않는 데이터

- ID 이미지 또는 base64
- 라이브 얼굴 이미지 또는 base64
- Rekognition 원본 얼굴 이미지/응답
- API Gateway 키
- AWS 자격 증명
- 비밀번호/신분증 원문

SQLite에는 UUID, 보관번호, 보관함 번호/상태, 거래 상태, 시각, 금액, 데모 결제 참조 같은
운영 데모 메타데이터만 저장합니다. 이미지 base64 payload를 로그에도 남기지 않습니다.

## 모의 경계

- STORE 결제: 2,000원, 카드 정보 없음
- RETRIEVE 추가 정산: 기본 0원
- 물리 보관함 열림: 시간 지연과 화면 애니메이션만 제공
- HELP: 역무원 요청 화면만 제공

실제 결제, 카드 승인, 보관함 하드웨어, 역무원 호출 API는 추가하지 않았습니다.

## SQLite 단일 서버 범위

SQLite가 공유 상태를 제공하는 범위는 **같은 FastAPI 호스트와 같은 DB 파일을 사용하는
여러 키오스크 브라우저**입니다. 이는 다중 서버 분산 데이터베이스 구조가 아니며,
서로 다른 EC2/컨테이너의 로컬 파일은 상태를 공유하지 않습니다.

수평 확장 시 이 계층을 PostgreSQL/RDS 또는 DynamoDB 같은 공유 데이터베이스로 이전하고,
동일한 조건부 상태 전이와 uniqueness 의미를 보존해야 합니다. SQLite를 다중 호스트 운영
환경에 적합하다고 주장하지 않습니다.

## 테스트

`tests/test_kiosk_store.py`와 `tests/test_app_kiosk.py`는 임시 SQLite 파일만 사용하며 실제
키오스크 DB에 접근하지 않습니다. 초기화/seed, 중복 예약 경쟁, 취소/예약 만료, STORE 멱등
완료와 코드 생성, RETRIEVE 잠금/lease 복구/취소/완료, 상태 API 필드 제한, 서버 입력 검증,
세션 인증과 429 rate limit, 재열기 지속성, 생체 데이터 열 부재를 검증합니다.
`test_imageprocessor_correlation.py`, `test_facecompare_correlation.py`,
`test_kiosk_javascript_correlation.py`는 실제 AWS 없이 capture UUID 전달, legacy producer fallback,
exact DynamoDB lookup, not-ready 무-fallback, A/B 프레임 분리, 동일 ID retry와 시도 횟수를 검증합니다.

```bash
python3 -m pytest -q tests/test_kiosk_store.py
python3 -m pytest -q tests/test_app_kiosk.py
python3 -m pytest -q
node --check web-ui/src/kiosk.js
python3 -m py_compile web-ui/backend/app.py web-ui/backend/config.py web-ui/backend/kiosk_store.py \
  lambda/imageprocessor/imageprocessor.py lambda/facecompare/facecompare.py
git diff --check
```

## 다음 단계

Phase 2A가 해결하지 않은 항목은 다음과 같습니다.

1. 안정적인 사용자 신원(stable user identity)
2. 서버가 강제하고 거래에 결합하는 얼굴 검증 authorization
3. 실제 결제
4. 물리 보관함 제어와 liveness/anti-spoofing
5. 분산/다중 호스트 데이터베이스

권장 Phase 2B는 성공한 exact-frame 비교 결과를 서버 발급 단기 authorization으로 만들고
해당 locker `transactionId`와 일회성으로 결합하는 것입니다. 그 뒤 승인된 모바일 ID 또는
identity provider의 안정적인 subject, liveness/anti-spoofing, 실제 결제와 보관함 하드웨어,
다중 호스트 DB를 각각 별도 단계에서 설계해야 합니다. Phase 2A에는 이 기능들을 추가하지
않았습니다.
