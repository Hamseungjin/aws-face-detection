# Project Technical Index

> 분석 기준: **현재 로컬 working tree** (branch `devhsj`).
> 갱신: 2026-08-14 — locker-first STORE + Rekognition Collection/FaceId.
> 작성일: 2026-08-13.
> AWS 실환경은 read-only 조회로 검증했다. **데이터 스택·애플리케이션 스택은 모두 `DELETE_COMPLETE`이며 런타임 리소스는 현재 존재하지 않는다.** 아래 아키텍처/리소스 이름은 코드·IaC 기준이며, 실환경 존재 여부는 각 절에 별도 표기한다.
> 민감값(비밀번호, 세션 시크릿, API 키 값, 액세스 키, 쿠키, 실제 얼굴/신분증 바이트)은 포함하지 않는다.

---

## 1. 프로젝트 개요

현재 코드 기준으로 이 저장소는 **공공 사물함 키오스크 데모 + 실시간 카메라 프레임 분석 파이프라인**이다.

원래 기반은 Amazon Rekognition Video Analyzer 샘플(카메라 프레임 → Kinesis → Lambda → S3/DynamoDB)이다. 현재 working tree는 그 위에 **서울 AI 안심 보관함 키오스크**를 얹었다. 시민은 신분증(데모 파일)과 라이브 얼굴로 보관을 시작하고, 8자리 보관번호 + 보관 당시 참조 얼굴로 물품을 찾는다.

README 한 줄 요약은 “카메라 프레임을 분석하고 키오스크에서 신분증과 얼굴을 비교한다” 수준이다. **현재 코드 기준으로는** 그보다 구체적이다.

| 계층 | 실제 구현 | 역할 |
| ---- | --------- | ---- |
| 사용자용 Kiosk | `GET /kiosk` → `web-ui/kiosk.html` + `web-ui/src/kiosk.js` | 시민 STORE/RETRIEVE. 담당자 로그인 후에만 동작 |
| 운영자 UI | `GET /` → `web-ui/index.html` + `web-ui/src/app.js` | 웹캠 연속 캡처, 프레임 뷰어, 운영자 `/face-compare`, DetectLabels 토글 |
| 운영자 KPI Dashboard | `kpi-dashboard/` FastAPI `:8000` | CloudWatch/DynamoDB 읽기 전용 KPI. Incident DB 아님 |
| FastAPI backend | `web-ui/backend/app.py` | 세션 로그인, 프레임 ingest, 키오스크 거래, facecompare 호출, 참조 얼굴 수명 |
| Application DB | SQLite (`KioskStore`) | locker + transaction 메타만. 이미지/임베딩 없음 |
| AWS Data Processing | Kinesis → imageprocessor → S3 + DynamoDB | 프레임 JPEG 저장 + 메타데이터 |
| 얼굴 비교 | FastAPI → Rekognition CompareFaces + Collection FaceId | 신규 거래는 IndexFaces/SearchFacesByImage. Legacy S3 fallback 유지. Face Liveness **미구현** |
| 사물함 Transaction | SQLite 상태기계 | 예약·보관·찾기. 물리 사물함 API는 mock |
| 영상/이미지 저장 | S3 `frames/` + `locker-references/` | JPEG binary bytes. DynamoDB에는 이미지 없음 |
| 배포 인프라 | 두 개의 CloudFormation 템플릿 | Data Stack + Application Stack (1 EC2) |

현재 코드 기준 전체 흐름:

```text
담당자 로그인 (세션 쿠키)
        │
        ▼
┌───────────────────┐     ┌──────────────────────────┐
│ 시민 Kiosk /kiosk │     │ 운영자 UI /  + KPI /dash │
└─────────┬─────────┘     └────────────┬─────────────┘
          │ HTTPS (EC2 Nginx) 또는 localhost HTTP
          ▼
     FastAPI :8080
          │
          ├─ SQLite  lockers / transactions
          │
          ├─ POST /api/capture-frame ──► Kinesis FrameStream
          │                                    │
          │                                    ▼
          │                              imageprocessor
          │                              ┌─────┴──────┐
          │                              ▼            ▼
          │                             S3         DynamoDB
          │                          frames/*.jpg  EnrichedFrame
          │
          ├─ STORE: lockers → reserve(hold) → face-verify → complete
          │         │
          │         ├─ OpenCV Quality Gate (로컬)
          │         ├─ CompareFaces ID ↔ FRAME-A
          │         └─ complete 시 IndexFaces → FaceId (reference.jpg 신규 생성 없음)
          │
          ├─ RETRIEVE start
          │         ├─ OpenCV Quality Gate
          │         ├─ Collection: SearchFacesByImage + expected FaceId
          │         └─ Legacy: S3 reference.jpg CompareFaces
          │
          └─ RETRIEVE complete
                    ├─ DeleteFaces(FaceId) 또는 legacy S3 delete
                    └─ SQLite RETRIEVED
```

문서와 다른 점:

- README §8은 “EC2 #1 Web UI, EC2 #2 KPI”라고 한다. **현재 코드 기준으로는** `aws-infra/aws-infra-ec2-cfn.yaml`이 **ApplicationInstance 1대**에 Nginx + FastAPI + KPI + SQLite EBS를 같이 올린다.
- RUNBOOK §5는 키오스크 확인을 `/face-compare`로 적는다. **현재 키오스크는 API Gateway를 거치지 않고** FastAPI가 `facecompare`를 직접 invoke한다.
- `lambda/faceverifier/`는 저장소에 있으나 **CloudFormation에 없고 키오스크가 호출하지 않는다.**

근거: `web-ui/backend/app.py`, `web-ui/src/kiosk.js`, `aws-infra/aws-infra-ec2-cfn.yaml`, `aws-infra/nginx/application.conf`.

---

## 2. 전체 서비스 아키텍처

```text
                    ┌─────────────────────────────────────────┐
                    │ Browser                                 │
                    │  /kiosk  시민 키오스크                   │
                    │  /       운영자 프레임 뷰어              │
                    │  /dashboard/ KPI                        │
                    └──────────────────┬──────────────────────┘
                                       │ HTTPS :443 (EC2)
                                       │ 또는 http://127.0.0.1:8080
                                       ▼
                    ┌─────────────────────────────────────────┐
                    │ Nginx  (Application EC2, self-signed)   │
                    │  / /kiosk /api /src /healthz → :8080    │
                    │  /dashboard/             → :8000        │
                    └──────────────────┬──────────────────────┘
                                       │ loopback only
              ┌────────────────────────┼────────────────────────┐
              ▼                        ▼                        │
     FastAPI web-ui             KPI FastAPI                     │
     127.0.0.1:8080             127.0.0.1:8000                  │
              │                        │                        │
              │                        └─ CW Logs / Metrics     │
              │                           DynamoDB Query (읽기) │
              │                                                 │
              ├─ SQLite  /data/kiosk/kiosk.db (EC2)
              │          또는 web-ui/backend/data/kiosk.sqlite3
              │
              ├─ kinesis:PutRecord
              ├─ lambda:Invoke facecompare
              ├─ dynamodb:GetItem EnrichedFrame
              ├─ s3:Copy/Get/Delete locker-references/, frames/
              ├─ lambda:Get/UpdateFunctionConfiguration imageprocessor
              └─ cfn DescribeStackResource + apigateway GET (/api/config)

==================== Data Stack (서버리스) ====================

Kinesis FrameStream (1 shard)
        │ EventSourceMapping TRIM_HORIZON
        ▼
imageprocessor Lambda
        ├─ (optional) rekognition:DetectLabels   기본 ENABLE_DETECT_LABELS=false
        ├─ s3:PutObject   frames/YYYY/MM/DD/HH/<frame_id>.jpg
        ├─ dynamodb:PutItem EnrichedFrame
        └─ (optional) sns:Publish   현재 watch list 비어 있음 → 사실상 미사용

API Gateway RtRekogRestApi / development   (운영자 UI 전용)
        ├─ GET  /enrichedframe  + API Key  → framefetcher
        └─ POST /face-compare   + API Key  → facecompare (targetFrameId 없으면 전역 최신)

facecompare Lambda
        ├─ DynamoDB GetItem(frame_id) 또는 GSI latest
        └─ rekognition:CompareFaces
```

호출 주체 요약:

| 호출 | 주체 | 대상 |
| ---- | ---- | ---- |
| 웹캠 JPEG POST | 브라우저 | FastAPI `/api/capture-frame`만. AWS 직접 호출 없음 |
| Kinesis PutRecord | FastAPI 또는 `client/video_cap.py` | `FrameStream` |
| S3 프레임 저장 | imageprocessor | `frames/*.jpg` |
| S3 참조 얼굴 | FastAPI `reference_face.ensure_reference_copy` | `locker-references/<tx>/reference.jpg` |
| Rekognition CompareFaces | facecompare Lambda | Source(ID bytes 또는 reference S3) vs Target(frame S3) |
| Rekognition DetectLabels | imageprocessor (옵션) | 프레임 bytes |
| 운영자 프레임 이미지 표시 | 브라우저 `<img>` | framefetcher가 만든 **presigned GET URL** |

---

## 3. STORE 보관 워크플로우

실제 실행 순서 (`kiosk.js` + `app.py` + `kiosk_store.py` + `reference_face.py` + `imageprocessor.py` + `facecompare.py`).

1. 담당자가 `/kiosk`에서 `POST /api/login` 후 `GET /api/me`로 세션 쿠키가 수락됐는지 확인한다. 미인증이면 시민 화면이 열리지 않는다.
2. ATTRACT 화면 터치 → MODE_SELECT에서 **물품 보관** (`selectMode('STORE')`).
3. CONSENT: 체크박스 동의 후에만 ID_CAPTURE로 진행.
4. ID_CAPTURE: `DemoIdAdapter.read()`가 JPG/PNG 파일(최대 5MiB)을 읽어 브라우저 메모리의 `{filename, contentType, base64, previewUrl}`만 만든다. **서버에 아직 업로드하지 않는다.**
5. FACE_CAPTURE: `getUserMedia`로 정면 카메라. **촬영** 버튼 → canvas JPEG(quality 0.82) → `capturedFaceBase64`.
6. WAITING_FOR_FRAME: `POST /api/capture-frame` (`imageBase64`, `frameCount`).
7. FastAPI `capture_frame()`이 **서버에서** `capture_id = str(uuid.uuid4())`를 만들고 Kinesis pickle에 넣는다.

   ```text
   ApproximateCaptureTime, FrameCount, ImageBytes, CaptureId
   ```

   브라우저에 AWS 자격 증명은 없다. 응답: `{ok, captureId, sequenceNumber, shardId}`.
8. imageprocessor가 Kinesis 레코드를 unpickle하고 `_resolve_frame_id()`로 `CaptureId`를 검증한 뒤 **그대로 `frame_id`로 사용**한다. JPEG bytes를 `s3://<bucket>/frames/YYYY/MM/DD/HH/<frame_id>.jpg`에 `put_object(Body=img_bytes)`로 저장하고, EnrichedFrame에 메타를 `put_item`한다.
9. 키오스크는 같은 `captureId`를 `targetFrameId`로 `POST /api/kiosk/store/face-verify`를 호출한다. `reason == TARGET_FRAME_NOT_READY`이면 **동일 targetFrameId로** 1.5초 간격, 최대 15초 폴링한다 (`waitForExactComparison`).
10. FastAPI는 ID base64를 디스크에 쓰지 않고 `reference_face.compare_id_to_frame()` → `lambda.invoke(facecompare)`로 넘긴다.
11. facecompare는 `targetFrameId`로 EnrichedFrame `GetItem(ConsistentRead)` 후 Rekognition `CompareFaces`:
    - SourceImage = 업로드 ID `Bytes`
    - TargetImage = 해당 프레임 S3 JPEG
    - API `SimilarityThreshold` = 0.0 (필터 없음)
    - 업무 임계값 기본 **90** (`FACE_SIMILARITY_THRESHOLD` / `facecompare-params.json`)
12. 유사도 ≥ 90 이고 `reason == SIMILARITY_ABOVE_THRESHOLD`이면 FastAPI가 서명 세션에만 저장한다.

    ```text
    verified_store_frame_id = FRAME-A
    verified_store_at
    verified_store_similarity
    ```

    ID 바이트·base64는 세션에 넣지 않는다. 실패하면 세션 바인딩을 지운다.
13. 키오스크는 LOCKER_SELECT → `GET /api/kiosk/lockers` → AVAILABLE만 선택 → `POST /api/kiosk/store/reserve`.
14. `KioskStore.reserve_locker()`가 locker를 `RESERVED`로 바꾸고 `transactions`에 `status=RESERVED`, `transaction_id=uuid4`, `reservation_expires_at = now + 120s`를 만든다. **이 시점에는 retrieval_code가 없다.**
15. PAYMENT: `MockPaymentAdapter`가 1.4초 대기 후 `{success:true, mock:true}`. 실제 PG/카드 API 없음. 금액 고정 2000원.
16. STORE_COMPLETING: `POST /api/kiosk/store/complete` (`transactionId`만). 클라이언트가 frame id를 고를 수 없다.
17. FastAPI는 세션의 `verified_store_frame_id`를 읽는다. 없으면 **403 `STORE_FACE_NOT_VERIFIED`**, 거래는 RESERVED 유지.
18. `lookup_frame_item()`으로 EnrichedFrame에서 `s3_bucket`/`s3_key`를 읽고 `ensure_reference_copy()`가

    ```text
    CopyObject
      frames/.../<FRAME-A>.jpg
        → locker-references/<transaction_id>/reference.jpg
    ```

    를 수행한다. 대상이 이미 있으면 head_object 후 재사용(멱등).
19. `complete_store()`가 8자리 `retrieval_code`(`secrets.randbelow(100_000_000)` → `%08d`)와 `mock_payment_id`(`PAY-` + 6 hex)를 발급하고:

    ```text
    transaction: RESERVED → STORED
    locker:      RESERVED → OCCUPIED
    reference_frame_id, reference_face_s3_key, reference_face_created_at 기록
    ```

    이미 STORED면 검증/복사를 다시 하지 않고 동일 결과를 반환한다.
20. 세션 검증 필드를 지운다. 브라우저는 `retrievalCode`/`mockPaymentId`/`referenceFrameId`/`referencePresent`만 받는다. **`reference_face_s3_key`는 내려가지 않는다.**
21. `MockLockerControlAdapter.open()`이 1.8초 후 `{state:'OPEN'}`. **물리 사물함 제어는 없다.**
22. COMPLETE 화면에 보관함 번호와 8자리 보관번호를 표시. 12초 후 또는 이용 완료 시 `resetKiosk()`. 최종 Transaction status = **`STORED`** (찾기 전까지).

번호로 본 핵심 식별자:

```text
1. 사용자가 보관 시작 (MODE_SELECT → STORE)
2. 데모 신분증 파일 선택 (브라우저 메모리)
3. 라이브 얼굴 촬영
4. FRAME-A 생성 = FastAPI captureId UUIDv4
5. Kinesis → imageprocessor → S3 frames/ + DynamoDB frame_id=FRAME-A
6. ID vs FRAME-A CompareFaces ≥ 90
7. 세션에 verified_store_frame_id=FRAME-A 바인딩
8. locker reserve → Transaction RESERVED
9. mock 결제
10. FRAME-A JPEG를 locker-references/<tx>/reference.jpg 로 복사
11. Transaction STORED + retrieval_code 발급
12. mock 문 열림 → COMPLETE
```

ASCII workflow:

```text
[ID Image 파일, 브라우저 메모리]
          │
          │  (face-verify 때 1회 POST, 디스크 미저장)
          ▼
[Live FRAME-A = captureId]
          │
          │  Kinesis → S3 frames/<FRAME-A>.jpg
          ▼
     CompareFaces
     ID Bytes  vs  FRAME-A S3 JPEG
     threshold 90
          │
     ┌────┴────┐
     │         │
  match      fail
     │         │
     ▼         ▼
 Verified    FACE_RETRY (세션 바인딩 없음)
     │
     ▼
 locker RESERVED + Transaction RESERVED
     │
     ▼
 mock payment 2000
     │
     ▼
 CopyObject → locker-references/<tx>/reference.jpg
     │
     ▼
 Transaction STORED
 retrieval_code 8자리
     │
     ▼
 mock locker open
     │
     ▼
 COMPLETE (status 는 STORED)
```

---

## 4. RETRIEVE 찾기 워크플로우

1. MODE_SELECT에서 **물품 찾기**. CONSENT/ID 재촬영 없음. `RETRIEVAL_CODE` 화면.
2. 보관번호 입력: 프론트가 숫자만 남기고 8자리로 자른다. 서버 `validate_retrieval_code()`는 **정확히 8자리 ASCII digit**만 허용한다. 그 외는 400 `INVALID_RETRIEVAL_CODE`.
3. 코드만으로 문을 열지 않는다. **얼굴 촬영으로 이동**.
4. FACE_CAPTURE에서 현재 얼굴을 촬영 → `POST /api/capture-frame` → 새 `captureId` = **FRAME-B** (FRAME-A와 다른 UUID).
5. 키오스크가 `POST /api/kiosk/retrieve/start`에 `{retrievalCode, targetFrameId: FRAME-B}`를 보낸다. `targetFrameId` 없으면 **400 `TARGET_FRAME_ID_REQUIRED`**.
6. FastAPI는 세션 스코프 rate limiter를 확인한 뒤 `get_stored_by_retrieval_code()`로 **정확히 1건**을 찾는다.

   ```text
   retrieval_code = 입력값
   status = STORED
   locker.status = OCCUPIED
   locker.active_transaction_id = 해당 거래
   ```

   없거나 이미 RETRIEVING/RETRIEVED이면 409 `RETRIEVAL_UNAVAILABLE` (존재 여부를 구분하지 않음) + rate-limit 실패 1회.
7. `reference_face_s3_key`가 없거나 `reference_face_deleted_at`이 있으면 HTTP 200 + `reason=REFERENCE_FACE_MISSING`. 상태는 STORED 유지, 문 안 염.
8. `compare_reference_to_frame()`이 facecompare를 호출한다.

   ```text
   referenceS3Bucket + referenceS3Key   (브라우저가 고르지 않음)
   targetFrameId = FRAME-B
   similarityThreshold = 90
   ```

9. facecompare exact mode: EnrichedFrame `GetItem(FRAME-B)`. 없으면 `TARGET_FRAME_NOT_READY`. 키오스크는 같은 FRAME-B로 최대 15초 폴링. **실패 횟수/rate-limit에 넣지 않음.** Transaction은 STORED.
10. Rekognition CompareFaces:
    - SourceImage = S3 `locker-references/<tx>/reference.jpg` (STORE 때 복사한 FRAME-A)
    - TargetImage = S3 `frames/.../<FRAME-B>.jpg`
11. 불일치/`NO_FACE_*`/threshold 미달: HTTP 200, `matched=false`, `status=STORED`. locker 미개방. backend rate-limit 미소비.
12. 일치(`SIMILARITY_ABOVE_THRESHOLD`): `start_retrieval()` → `STORED → RETRIEVING`, `retrieval_started_at` 기록. locker는 **OCCUPIED 유지**.
13. 응답: `transactionId`, `lockerId`, `additionalFee=0`, `status=RETRIEVING`, 유사도. S3 키 없음.
14. 키오스크는 RETRIEVE_CONFIRM/SETTLEMENT를 **기본 경로에서 건너뛰고** 바로 LOCKER_OPENING. mock open 후 `POST /api/kiosk/retrieve/complete`.
15. `complete_retrieval()`: `RETRIEVING → RETRIEVED`, locker `OCCUPIED → AVAILABLE`, `active_transaction_id = NULL`.
16. `KIOSK_REFERENCE_DELETE_ON_RETRIEVE=true`(기본) 이고 retention=0(기본)이면 FastAPI가 S3 reference를 `delete_object`하고 `reference_face_deleted_at`을 찍는다. 삭제 실패해도 거래는 이미 RETRIEVED (best-effort).
17. COMPLETE 후 로컬 화면 데이터 초기화.

`RETRIEVING`에서 재요청: 같은 코드로 `/retrieve/start`를 다시 치면 더 이상 STORED가 아니므로 409 `RETRIEVAL_UNAVAILABLE`. 이미 열린 lease는 `/retrieve/recover`로만 복구한다. recover가 STORED를 보면 얼굴 재검증을 요구한다.

실패 시 상태: 얼굴 실패·프레임 미준비·참조 없음은 **STORED 유지**. 잘못된 코드는 거래 정보를 노출하지 않는다.

ASCII workflow:

```text
Retrieval Code (8 ASCII digits)
        ↓
Transaction Lookup  (STORED + OCCUPIED only)
        ↓
Reference S3 = locker-references/<tx>/reference.jpg
   (STORE 때 복사한 FRAME-A JPEG)
        │
        ├────────────┐
        │            │
        │         FRAME-B
        │     (새 captureId)
        │            │
        └─────┬──────┘
              ↓
         CompareFaces
         reference S3  vs  FRAME-B S3
              ↓
       Similarity ≥ 90 ?
         /          \
      Success       Fail / NOT_READY / NO_FACE
        ↓             ↓
   STORED→          STORED 유지
   RETRIEVING       locker 미개방
        ↓
   mock locker open
        ↓
   RETRIEVING→RETRIEVED
   locker AVAILABLE
        ↓
   delete reference.jpg (기본)
```

---

## 5. FRAME-A / FRAME-B / Capture ID 구조

| 이름 | 무엇인가 | 사람 ID인가? |
| ---- | -------- | ------------ |
| `captureId` | FastAPI가 **촬영 요청마다** 만드는 UUIDv4. Kinesis `CaptureId` | 아님. 촬영 이벤트 ID |
| `frame_id` | imageprocessor가 DynamoDB/S3에 쓰는 키. CaptureId가 유효하면 **그와 동일** | 아님. 처리된 프레임 ID |
| `targetFrameId` | facecompare exact mode 입력. 키오스크는 방금 받은 `captureId`를 그대로 넣음 | 아님 |
| FRAME-A | STORE 때 검증 통과한 라이브 `captureId`/`frame_id`. 이후 거래 참조로 복사 | 아님. 그 거래의 참조 프레임 |
| FRAME-B | RETRIEVE 때 새로 찍은 `captureId`/`frame_id` | 아님 |
| `transaction_id` | SQLite 거래 UUID | 사람이 아니라 거래 |
| `retrieval_code` | 8자리 숫자 조회 키 | 소유 증명 아님 |
| Person ID / Rekognition Collection / embedding | **코드에 없음** | 영구 사람 ID 없음 |

`frame_id`는 **사람의 ID가 아니라 촬영 이벤트(1프레임)의 ID**이다. 같은 이용자가 STORE와 RETRIEVE를 하면 FRAME-A와 FRAME-B는 서로 다른 UUID다. 동일인 판정은 Collection 검색이 아니라 **거래에 묶인 JPEG 두 장의 1:1 CompareFaces**다.

```text
STORE:   ID 이미지(일회성 Bytes)  vs  FRAME-A JPEG
보관:    FRAME-A JPEG ──CopyObject──► locker-references/<tx>/reference.jpg
RETRIEVE: reference.jpg            vs  FRAME-B JPEG
```

운영자 UI(`app.js` `compareFace`)는 여전히 API Gateway `POST /face-compare`를 **`targetFrameId` 없이** 호출할 수 있다. 그때만 facecompare가 GSI로 **전역 최신 프레임**을 고른다. 주석과 테스트가 이 경로를 “legacy operator only”로 명시한다. **시민 키오스크는 항상 targetFrameId를 보낸다.**

`client/video_cap.py`는 `CaptureId`를 넣지 않는다. imageprocessor는 그 경우 새 UUIDv4 `frame_id`를 만든다(레거시 호환).

근거: `app.capture_frame`, `imageprocessor._resolve_frame_id`, `facecompare.get_frame_by_id` / `query_latest_frame`, `reference_face.validate_frame_id`.

---

## 6. Transaction State Machine

코드에 존재하는 transaction `status`만:

```text
RESERVED ──complete_store──► STORED ──start_retrieval──► RETRIEVING ──complete_retrieval──► RETRIEVED
    │                           ▲                            │
    │ cancel_store              │ cancel_retrieval            │
    │ 또는 reservation 만료      │ 또는 retrieval lease 만료    │
    ▼                           │                            │
CANCELLED / EXPIRED             └────────────────────────────┘
```

locker `status`: `AVAILABLE` | `RESERVED` | `OCCUPIED` | `DISABLED`.

| 상태 | 의미 | 진입 조건 | 다음 가능한 상태 |
| ---- | ---- | --------- | ---------------- |
| `RESERVED` | 보관함 점유, 결제/완료 전. `retrieval_code` 없음 | `reserve_locker`가 AVAILABLE locker를 원자 UPDATE | `STORED`, `CANCELLED`, `EXPIRED` |
| `STORED` | 보관 완료. 코드·참조 얼굴 존재. locker OCCUPIED | `complete_store` 성공 (또는 멱등 재호출) | `RETRIEVING` |
| `RETRIEVING` | 얼굴 통과 후 찾기 lease. locker는 계속 OCCUPIED | `start_retrieval` | `RETRIEVED`, `STORED`(cancel 또는 lease 만료) |
| `RETRIEVED` | 찾기 완료. locker AVAILABLE | `complete_retrieval` | 없음 (종단) |
| `CANCELLED` | 예약 사용자 취소 | `cancel_store` (RESERVED만) | 없음 |
| `EXPIRED` | 예약 TTL(기본 120초) 초과 | 다음 DB 작업의 `_release_expired` | 없음 |

실패 시 rollback:

- 얼굴 불일치 / 프레임 미준비 / 참조 없음: **상태 변경 없음** (STORE는 세션 바인딩만 제거, RETRIEVE는 STORED 유지).
- `complete_store` 중 UNIQUE 충돌: retrieval_code/payment_id를 최대 25회 재생성. 실패 시 `CODE_GENERATION_FAILED`, 트랜잭션 rollback.
- S3 Copy는 DB 커밋 **전**. DB가 실패하면 S3 객체가 남을 수 있으나 재시도 시 같은 dest key를 재사용한다.
- `complete_retrieval`이 먼저 RETRIEVED로 커밋한 뒤 S3 delete. delete 실패는 거래 rollback하지 않는다.
- 백그라운드 스케줄러 없음. 만료/stale 복구는 lockers 조회, reserve, complete, retrieve 등 **다음 SQLite 작업의 `_cleanup()`**에서만 돈다.

```text
RESERVED 만료:  transaction EXPIRED, locker AVAILABLE, active_transaction_id NULL
RETRIEVING 만료(기본 120초): transaction STORED, retrieval_started_at NULL, locker OCCUPIED 유지
```

부분 unique index `one_active_transaction_per_locker`가 locker당 `RESERVED|STORED|RETRIEVING` 거래를 1건으로 제한한다.

시드 locker: AVAILABLE `01,02,04,05,07,09,10,12` / OCCUPIED `03,06,11` (거래 없이 점유만) / DISABLED `08`.

---

## 7. SQLite 저장 데이터

| 항목 | 값 |
| ---- | -- |
| 로컬 기본 경로 | `web-ui/backend/data/kiosk.sqlite3` (`config.KIOSK_DB_PATH`) |
| EC2 배포 경로 | `/data/kiosk/kiosk.db` (전용 encrypted EBS, bootstrap이 `/data/kiosk/*`만 허용) |
| 스키마 정의 | `web-ui/backend/kiosk_store.py` `KioskStore.initialize` + `_migrate_transaction_columns` |
| Journal | `PRAGMA journal_mode = WAL` (로컬 파일도 WAL + `-wal`/`-shm` 확인됨) |
| 기타 PRAGMA | 연결마다 `foreign_keys=ON`, `busy_timeout=5000`, `isolation_level=None` + `BEGIN IMMEDIATE` |
| 마이그레이션 | 기존 DB를 드롭하지 않고 reference 4컬럼을 `ALTER TABLE ... ADD COLUMN` |

로컬 working tree의 `kiosk.sqlite3`는 스키마가 코드와 일치한다. 행 내용(실제 보관번호 등)은 문서에 적지 않는다.

### `lockers`

| Table | Column | Type | 의미 | 생성 시점 | 수정 시점 |
| ----- | ------ | ---- | ---- | --------- | --------- |
| lockers | `locker_id` | TEXT PK | `01`–`12` | `initialize` INSERT OR IGNORE | 불변 |
| lockers | `status` | TEXT | AVAILABLE/RESERVED/OCCUPIED/DISABLED | 시드 | reserve/complete/cancel/expire/retrieve complete |
| lockers | `active_transaction_id` | TEXT FK | 현재 활성 거래, 없으면 NULL | reserve | cancel/expire/retrieve complete 시 NULL |
| lockers | `updated_at` | TEXT | UTC ISO-8601 ms | 시드 | 상태 변경 시 |

### `transactions`

| Table | Column | Type | 의미 | 생성 시점 | 수정 시점 |
| ----- | ------ | ---- | ---- | --------- | --------- |
| transactions | `transaction_id` | TEXT PK | 내부 UUID | `reserve_locker` | 불변 |
| transactions | `retrieval_code` | TEXT UNIQUE | 고객 8자리 숫자 | `complete_store` | 이후 불변 |
| transactions | `locker_id` | TEXT FK | 거래 보관함 | reserve | 불변 |
| transactions | `status` | TEXT | 위 상태기계 | reserve=`RESERVED` | 각 전이 |
| transactions | `amount` | INTEGER | 데모 금액(원), complete 시 2000 | complete_store | 멱등 재조회만 |
| transactions | `mock_payment_id` | TEXT UNIQUE | `PAY-` + 6 hex. 실제 승인 아님 | complete_store | 불변 |
| transactions | `created_at` | TEXT | 생성 시각 | reserve | 불변 |
| transactions | `reserved_at` | TEXT | 예약 시각 | reserve | 불변 |
| transactions | `stored_at` | TEXT | 보관 완료 시각 | complete_store | 불변 |
| transactions | `retrieval_started_at` | TEXT | 찾기 lease 시작 | start_retrieval | cancel/만료 시 NULL |
| transactions | `retrieved_at` | TEXT | 찾기 완료 시각 | complete_retrieval | 불변 |
| transactions | `reservation_expires_at` | TEXT | 예약 만료 | reserve | 불변 |
| transactions | `reference_frame_id` | TEXT | STORE 검증 FRAME-A | complete_store | 불변 |
| transactions | `reference_face_s3_key` | TEXT | `locker-references/<tx>/reference.jpg` | complete_store | 불변 (삭제는 별도 컬럼) |
| transactions | `reference_face_created_at` | TEXT | 참조 복사 시각 | complete_store | 불변 |
| transactions | `reference_face_deleted_at` | TEXT | S3 삭제 시각 | `mark_reference_deleted` (RETRIEVED 후) | 한 번 |

**없는 컬럼:** person_id, embedding, image BLOB, id_image, base64, 실패 횟수, 세션 토큰. 실패 횟수는 프로세스가 메모리 `RetrievalAttemptLimiter`로만 가진다.

인덱스: `retrieval_code` UNIQUE, `mock_payment_id` UNIQUE, `idx_transactions_retrieval_code_status`, partial unique `one_active_transaction_per_locker`.

---

## 8. DynamoDB 저장 데이터

| 항목 | 코드/IaC 값 |
| ---- | ----------- |
| Table | `EnrichedFrame` (`DDBTableNameParameter`, `ENRICHED_FRAME_TABLE`) |
| PK | HASH `frame_id` (String) |
| GSI | `processed_year_month-processed_timestamp-index` (HASH `processed_year_month`, RANGE `processed_timestamp`) |
| Billing | `PAY_PER_REQUEST` |
| TTL | `expire_at` enabled |
| 작성 주체 | imageprocessor `put_item`만 (키오스크 FastAPI는 `GetItem`만) |

| Field | 의미 | 데이터 출처 |
| ----- | ---- | ----------- |
| `frame_id` | 프레임 PK. 키오스크면 CaptureId와 동일 | `_resolve_frame_id` |
| `processed_timestamp` | Lambda 처리 epoch (Decimal) | `time.time()` |
| `approx_capture_timestamp` | 촬영 epoch | pickle `ApproximateCaptureTime` |
| `rekog_labels` | DetectLabels 결과(Decimal 변환). 끄면 `[]` | Rekognition 또는 빈 리스트 |
| `rekog_orientation_correction` | 기본 `ROTATE_0` | DetectLabels 또는 기본값 |
| `labels_disabled` | DetectLabels를 건너뛰었는지 | `not enable_detect_labels` |
| `processed_year_month` | GSI 파티션 `YYYYMM` (Asia/Seoul) | `convert_ts` |
| `s3_bucket` | 프레임 버킷 | `imageprocessor-params.json` `s3_bucket` |
| `s3_key` | `frames/YYYY/MM/DD/HH/<frame_id>.jpg` | 코드 조립 |
| `expire_at` | epoch 초. 기본 now + 30일 | `ddb_ttl_days` |

이미지 저장 여부:

- 얼굴 JPEG 자체: **저장하지 않음**
- Base64: **저장하지 않음**
- embedding / face vector: **저장하지 않음**

framefetcher가 조회 시 응답 아이템에 `s3_presigned_url`을 **추가**하지만 테이블에 persist하지 않는다.

AWS 실환경 (2026-08-13, `ap-northeast-2`): 테이블 **없음** (`ResourceNotFoundException`). 위는 IaC/코드 기준.

---

## 9. S3 저장 데이터와 실제 이미지 형식

코드/파라미터의 프레임 버킷 이름: `aws-face-detection-frames-115019372648-apne2`.

아티팩트/소스 zip 버킷: `aws-face-detection-lambda-115019372648-apne2` (`apps/`, `lambda/*.zip`).

| Prefix | 저장 내용 | 데이터 형식 | 생성 주체 | 사용 목적 | 삭제 시점/Lifecycle |
| ------ | --------- | ----------- | --------- | --------- | ------------------- |
| `frames/YYYY/MM/DD/HH/<frame_id>.jpg` | 카메라 프레임 | **JPEG binary bytes** (`put_object(Body=img_bytes)`). Base64/JSON/커스텀 암호문 아님 | imageprocessor | facecompare Target, 운영자 뷰어, STORE 참조 원본 | CFN `ExpireFrames` 기본 30일 |
| `locker-references/<transaction_id>/reference.jpg` | STORE 검증 FRAME-A 사본 | JPEG bytes (`copy_object`, MetadataDirective=COPY) | FastAPI `ensure_reference_copy` | RETRIEVE SourceImage | **lifecycle 없음**. 기본 `KIOSK_REFERENCE_DELETE_ON_RETRIEVE=true`면 RETRIEVED 직후 `delete_object` |
| `logging/imageprocessor/YYYY/MM/DD/HH/<request_id>.log` | 구조화 JSON Lines | 텍스트. 이미지 바이트 금지 | imageprocessor `log_util.flush_to_s3` | 디버그 | CFN `ExpireImageProcessorLogs` 기본 14일. **기본 env `ENABLE_S3_LOGGING=false`라 평소 안 씀** |

CFN에 `AbortIncompleteMultipartUpload` 7일도 있다.

### 실제 얼굴 이미지 저장 형태

현재 코드 기준 **원시 JPEG 바이트**다.

```text
imageprocessor: s3_client.put_object(Bucket=..., Key=..., Body=img_bytes)
reference:      s3_client.copy_object(... MetadataDirective="COPY")
```

`ContentType`을 명시하지 않는다. SSE-KMS/`ServerSideEncryption` 인자도 없다. 애플리케이션 레벨 암호화 없음.

### reference 이미지

- 원본 FRAME을 계속 가리키지 않는다. **CopyObject로 별도 보존**한다.
- prefix: `locker-references/` (`KIOSK_REFERENCE_S3_PREFIX`).
- Transaction 연결: SQLite `reference_face_s3_key` + `reference_frame_id`.
- `frames/` lifecycle가 원본을 지워도 참조는 남도록 분리한 것이다.
- RETRIEVE 성공 후 기본 즉시 삭제. `KIOSK_REFERENCE_POST_RETRIEVAL_RETENTION_SECONDS > 0`이면 **로그만 남기고 실제 지연 삭제 worker는 없다** (부분 구현).

AWS 실환경: 해당 버킷 **없음** (`NoSuchBucket`). 위는 코드/IaC 기준.

---

## 10. AWS 서비스 아키텍처

실제로 코드·IaC가 연결하는 서비스만. (faceverifier 미배포, Rekognition Collection 없음, ALB/ACM/NAT/VPC endpoint 없음)

| AWS Service | Resource | 역할 | 호출 주체 | 연결 대상 |
| ----------- | -------- | ---- | --------- | --------- |
| EC2 | ApplicationInstance (`video-analyzer-app`) | Nginx+FastAPI+KPI 단일 호스트 | 사용자 HTTPS | EBS, EIP, IAM instance role |
| EBS | root gp3 + data gp3 (`video-analyzer-app-data`) | OS / SQLite. 둘 다 Encrypted=true | EC2 | `/`, `/data/kiosk` |
| Elastic IP | ApplicationElasticIp | 고정 공인 IP | Nginx :443 | Instance |
| IAM | ApplicationInstanceRole, Lambda execution roles | 런타임 권한 | EC2/Lambda | 아래 서비스 |
| SSM | `/video-analyzer/webui/*` + Session Manager | 로그인 해시/세션시크릿, SSH 없이 관리 | bootstrap `get-parameter` | `/etc/webui.env` |
| KMS | SSM SecureString용 (`kms:Decrypt` via SSM only) | 파라미터 복호화. **이미지 암호화 아님** | EC2 role | SSM |
| Nginx | 호스트 패키지 + `application.conf` | TLS 종료, 리버스 프록시 | Browser | FastAPI/KPI loopback |
| Kinesis Data Streams | `FrameStream` shard 1 | 프레임 pickle 전송 | FastAPI / video_cap | imageprocessor ESM |
| Lambda | `imageprocessor` | 프레임 persist + optional DetectLabels | Kinesis ESM | S3, DDB, Rekognition |
| Lambda | `framefetcher` | 최근 프레임 + presigned URL | API Gateway GET | DDB, S3 |
| Lambda | `facecompare` | 1:1 CompareFaces | API GW 또는 FastAPI invoke | DDB, Rekognition, S3 |
| DynamoDB | `EnrichedFrame` | 프레임 메타 | imageprocessor write, 그 외 read | S3 key 포인터 |
| S3 | frames bucket | JPEG + optional logs + references | imageprocessor, FastAPI, Rekognition | facecompare/framefetcher |
| S3 | artifact bucket | zip/tarball | `build.py` publish/deploy | EC2 userdata, Lambda Code |
| Rekognition | CompareFaces, (optional) DetectLabels | 얼굴 1:1 / 객체 라벨 | facecompare / imageprocessor | S3 또는 Bytes |
| API Gateway | `RtRekogRestApi` stage `development` | 운영자 `/enrichedframe`, `/face-compare` | 운영자 UI (로그인 후 apiKey) | Lambdas |
| CloudFormation | `video-analyzer-stack`, `video-analyzer-ec2-stack` | IaC | `build.py` | 위 리소스 |
| CloudWatch Logs | `/aws/lambda/{imageprocessor,framefetcher,facecompare}` | 구조화 로그 | Lambda stdout, KPI Insights | KPI |
| CloudWatch Metrics | Kinesis IteratorAge 등 | KPI | KPI `GetMetricData` | dashboard |
| Event Source Mapping | Kinesis → imageprocessor | 트리거, TRIM_HORIZON | CFN | FrameStream |
| EventBridge Scheduler | `video-analyzer-ec2-start/stop-*` | 평일 KST start/stop | **EnableSchedulerParameter=true 일 때만** | EC2 |
| SNS | imageprocessor watch-list | 라벨 알림 | imageprocessor | 현재 `label_watch_list=[]` → **비활성** |

포함하지 않음: Rekognition Collection, Cognito, RDS, SQS, ALB, ACM, Secrets Manager(로그인 시크릿은 SSM).

```text
┌──────────────────┐
│ Browser / Kiosk  │
└────────┬─────────┘
         │ HTTPS
         ▼
┌──────────────────┐
│ Nginx :443       │
└────────┬─────────┘
         │
    ┌────┴─────┐
    ▼          ▼
 FastAPI     KPI
    │
    ├──────── SQLite
    │
    ├──────── Kinesis ──► imageprocessor ──┬── S3 frames/
    │                                      └── DynamoDB
    │
    ├──────── invoke facecompare ── Rekognition CompareFaces
    │                 ▲
    │                 └── S3 frames/ 또는 locker-references/
    │
    └──────── S3 Copy/Delete locker-references/

운영자 UI만:
 FastAPI /api/config ──► API Gateway apiKey
 브라우저 ──► GET /enrichedframe, POST /face-compare
```

---

## 11. Application Stack

| 항목 | 값 |
| ---- | -- |
| stack name | `video-analyzer-ec2-stack` (`config/ec2-global-params.json`) |
| template | `aws-infra/aws-infra-ec2-cfn.yaml` |
| 배포 | `pynt createec2stack` / `updateec2stack` / `deleteec2stack` |
| 현재 AWS 상태 | **`DELETE_COMPLETE` (실환경 미배포).** 2026-08-13 `describe-stacks` ValidationError |

주요 resources:

- `ApplicationSecurityGroup` — :443/:80 ingress, SSH 없음
- `ApplicationInstanceRole` + `ApplicationInstanceProfile`
- `ApplicationInstance` — AL2023, 기본 t3.small, public subnet, root EBS encrypted gp3
- `ApplicationDataVolume` — encrypted gp3, `/dev/xvdf`, **DeletionPolicy: Snapshot**
- `ApplicationDataVolumeAttachment`
- `ApplicationElasticIp` + Association
- (조건) `SchedulerRole` + 4개 `AWS::Scheduler::Schedule`

Parameters (요지): VPC/Subnet, AMI SSM, instance type, ingress CIDR, volume sizes, loopback ports 8080/8000, artifact bucket/prefix, **DataStackName** `video-analyzer-stack`, API stage, DDB/GSI/Kinesis/Frame bucket/Lambda 이름, SSM prefix `/video-analyzer/webui`, `EnableSchedulerParameter` (템플릿 기본 `false`, example json은 `true`, 현재 `ec2-params.json`은 `false`).

Outputs: InstanceId, ElasticIp, `https://<EIP>/`, `https://<EIP>/dashboard/`, DataVolumeId, StopInstancesCommand, ScheduleSummary, ArchitectureNote.

dependency: **데이터 스택이 먼저 있어야** 런타임이 동작한다. 스택 생성 자체는 데이터 스택을 CFN DependsOn하지 않지만, 인스턴스 롤이 데이터 리소스 ARN을 파라미터로 참조한다.

삭제 시 주의:

- `deleteec2stack`은 데이터 스택/프레임 버킷을 건드리지 않는다.
- Data volume은 Snapshot 후 삭제된다. Snapshot 비용이 남을 수 있다.
- EIP는 스택과 함께 해제된다.
- SSM 파라미터는 이 스택이 만들지 않으므로 남는다.

런타임 부트스트랩 (`aws-infra/userdata/application-bootstrap.sh`):

- EBS XFS mount `/data/kiosk` (이미 파일시스템이면 포맷하지 않음)
- `apps/webui`, `apps/kpi`, `apps/application` 아티팩트
- SSM에서 username / password-hash / session-secret 로드 → `/etc/webui.env` (권한 600)
- self-signed TLS + `application-tls-refresh.service`
- systemd: `kiosk-fastapi`, `kpi-dashboard`, nginx

---

## 12. Data Stack

| 항목 | 값 |
| ---- | -- |
| stack name | `video-analyzer-stack` (`config/global-params.json`) |
| template | `aws-infra/aws-infra-cfn.yaml` |
| 배포 | `pynt createstack` / `updatestack` / `deletestack` |
| 현재 AWS 상태 | **`DELETE_COMPLETE`.** FrameStream, EnrichedFrame, imageprocessor, facecompare, frames 버킷 모두 조회 실패 |

```text
Data Stack
├─ Kinesis FrameStream
├─ DynamoDB EnrichedFrame + GSI + TTL
├─ S3 FrameS3Bucket (lifecycle frames/, logging/)
├─ Lambda imageprocessor + EventSourceMapping
├─ Lambda framefetcher
├─ Lambda facecompare
├─ API Gateway RestApi / Resource / Method / Deployment / Stage
├─ API Key + UsagePlan (Throttle + Daily Quota) + UsagePlanKey
├─ IAM roles/policies
└─ Rekognition 은 리소스가 아니라 Lambda 권한으로 호출
```

Parameters: source zip bucket/keys, Lambda 이름, API path (`enrichedframe`, `face-compare`), stream/table/GSI/bucket 이름, API 이름/stage/plan, frames 30일 / logs 14일, throttle 20/40, daily quota 5000.

Outputs: `VidAnalyzerApiEndpoint`, `VidAnalyzerApiKey`(키 **ID**), `FaceCompareLambdaArn`.

dependency: Application Stack의 런타임 전제. 키오스크 얼굴 비교는 이 스택 없이 동작하지 않는다.

삭제 시 주의:

- `pynt deletestack` / RUNBOOK: **S3 저장 프레임이 삭제될 수 있다.**
- `DevUsagePlan`은 `DeletionPolicy: Retain` — 스택 삭제 후에도 usage plan이 남을 수 있다.
- Rekognition에 persist되는 Collection은 없으므로 Collection 잔존 이슈는 없다.
- `locker-references/`는 버킷과 같이 사라진다.

`lambda/faceverifier`는 이 스택에 **없다.**

---

## 13. STORE 예외 처리

HTTP 본문 `detail` 또는 JSON `reason`은 코드에 있는 것만 적는다.

| 단계 | 조건 | Backend 처리 | HTTP Status / Error Code | Frontend 표시 | Transaction 영향 |
| ---- | ---- | ------------ | ------------------------ | ------------- | ---------------- |
| 로그인 | 잘못된 계정 | 세션 미발급 | 401 `invalid credentials` | 아이디/비밀번호 오류 | 없음 |
| 세션 | 미인증 API | `require_user` | 401 `authentication required` | 로그인 화면 | 없음 |
| ID 선택 | 형식/용량 (브라우저) | 업로드 전 거부 | (API 호출 없음) | JPG/PNG, 5MiB 메시지 | 없음 |
| face-verify | `targetFrameId`가 UUIDv4 아님 | `validate_frame_id` | 400 `INVALID_TARGET_FRAME_ID` | 촬영 정보 확인 실패, 재촬영 | 없음 |
| face-verify | contentType jpeg/png 아님 | 비교 안 함 | 200 `UNSUPPORTED_IMAGE_FORMAT` | 다른 사진 선택 | 없음 |
| face-verify | base64 깨짐/빈 이미지 | 비교 안 함 | 200 `INVALID_IMAGE` | 사진 읽기 실패 | 없음 |
| face-verify | ID > `KIOSK_MAX_ID_IMAGE_BYTES` | 비교 안 함 | 200 `IMAGE_TOO_LARGE` | 사진이 너무 큼 | 없음 |
| face-verify | Lambda/AWS 오류 | `ReferenceFaceError` | 200 + `reason`(ACCESS_DENIED, AWS_API_ERROR, SERVICE_ERROR 등) | 인증 서비스 문제 | 없음, 세션 바인딩 제거 |
| face-verify | 프레임 아직 DDB에 없음 | facecompare | 200 `TARGET_FRAME_NOT_READY` | 15초 동일 ID 폴링 | 없음 |
| face-verify | 프레임 너무 오래됨 | facecompare horizon 5분 | 200 `TARGET_FRAME_TOO_OLD` | 재촬영 | 세션 클리어 |
| face-verify | 얼굴 없음/불일치 | CompareFaces | 200 `NO_FACE_IN_SOURCE_OR_TARGET` / `SIMILARITY_BELOW_THRESHOLD` 등 | 재촬영 안내 | 세션 클리어 |
| face-verify | 메타 필드 누락 | facecompare | 200 `FRAME_METADATA_INVALID` | 재촬영 | 세션 클리어 |
| reserve | AVAILABLE 아님 | 원자 UPDATE 0행 | 409 `LOCKER_NOT_AVAILABLE` | 다른 보관함 선택 | 새 거래 없음 |
| complete | 세션에 검증 프레임 없음 | 거부 | 403 `STORE_FACE_NOT_VERIFIED` | recovery/재시도 경로 | **RESERVED 유지** |
| complete | DDB에 FRAME-A 없음 | 거부 | 409 `REFERENCE_SOURCE_MISSING` | recovery | RESERVED 유지 |
| complete | S3 원본 없음 | `REFERENCE_SOURCE_MISSING` | 502 | 서비스 오류 | RESERVED 유지 |
| complete | S3/IAM AccessDenied | `ACCESS_DENIED` | 502 | 서비스 오류 | RESERVED 유지 |
| complete | 기타 AWS | `AWS_API_ERROR` | 502 | 서비스 오류 | RESERVED 유지 |
| complete | 거래/락커 상태 불일치 | `StoreTransactionUnavailable` | 409 `STORE_TRANSACTION_UNAVAILABLE` | 거래 상태 확인 | 변경 없음 |
| complete | 잘못된 transactionId | validate | 400 `INVALID_TRANSACTION_ID` | — | 없음 |
| complete | 코드 25회 UNIQUE 실패 | rollback | 409 `CODE_GENERATION_FAILED` | 거래 상태 확인 | RESERVED 유지 |
| complete | 5xx 후 상태 조회 | 프론트 `getTransaction` | — | STORED면 복구, 아니면 재시도 | 서버가 이미 STORED면 멱등 |
| capture | 잘못된/빈 base64 | 거부 | 400 | 카메라 오류 | 없음 |
| capture | Kinesis 실패 | `_raise_aws_http_error` | 502 (credentials/access/resource/config/service) | 전송 실패 | 없음 |
| 예약 만료 | 120초 | `_release_expired` | (다음 DB 호출 시) | 다시 선택 | EXPIRED + locker AVAILABLE |
| 유휴 60초 | `resetKiosk` | 가능하면 cancel | — | ATTRACT | RESERVED면 CANCELLED 시도 |
| 도움 요청 | 3회 얼굴 실패 등 | mock help | — | HELP 화면 (실제 호출 없음) | 활성 거래 취소 시도 |

프론트가 시도로 세는 완료 결과: `SIMILARITY_ABOVE_THRESHOLD`, `SIMILARITY_BELOW_THRESHOLD`, `NO_FACE_IN_SOURCE_OR_TARGET`, `NO_FACE_IN_REFERENCE`, `NO_FACE_IN_TARGET`, `INVALID_RETRIEVAL_CODE`, `REFERENCE_FACE_MISSING`. `TARGET_FRAME_NOT_READY`와 `FRAME_TIMEOUT`은 시도로 세지 않는다.

---

## 14. RETRIEVE 예외 처리

| 단계 | 조건 | Backend 처리 | HTTP Status / Error Code | Frontend 표시 | Transaction 영향 |
| ---- | ---- | ------------ | ------------------------ | ------------- | ---------------- |
| 코드 형식 | 8자리 ASCII 숫자 아님 | 조회 전 거부 | 400 `INVALID_RETRIEVAL_CODE` | 보관번호 재확인 | 없음, rate-limit 미기록 |
| start | `targetFrameId` 누락 | 거부 | 400 `TARGET_FRAME_ID_REQUIRED` | (정상 UI는 항상 보냄) | 없음 |
| start | frame id 형식 | validate | 400 `INVALID_TARGET_FRAME_ID` | 재촬영 | 없음 |
| start | 코드 없음/이미 사용/상태 불일치 | 존재 여부 비공개 | 409 `RETRIEVAL_UNAVAILABLE` | 보관번호 확인 | 없음 + **rate-limit 실패 1** |
| start | 5회/60초 코드 실패 | limiter | 429 `RETRIEVAL_RATE_LIMITED` + Retry-After | 요청이 많음 | 없음 |
| start | 참조 키 없음/이미 삭제 | 비교 안 함 | 200 `REFERENCE_FACE_MISSING` | 역무원 도움 | **STORED 유지** |
| start | FRAME-B 아직 DDB 없음 | facecompare | 200 `TARGET_FRAME_NOT_READY` | 동일 FRAME-B 폴링 | STORED, rate-limit 미소비 |
| start | FRAME-B 5분 초과 | facecompare | 200 `TARGET_FRAME_TOO_OLD` | 재촬영 | STORED |
| start | 참조 S3 없음 | Rekognition InvalidS3 | 200 `REFERENCE_FACE_MISSING` | 역무원 도움 | STORED |
| start | 참조에서 얼굴 없음 | InvalidParameter (s3 source) | 200 `NO_FACE_IN_REFERENCE` | 역무원 도움 | STORED |
| start | 대상/소스 얼굴 없음 | | 200 `NO_FACE_IN_SOURCE_OR_TARGET` / `NO_FACE_IN_TARGET` | 재촬영 | STORED |
| start | 유사도 < 90 | | 200 `SIMILARITY_BELOW_THRESHOLD` | 등록 얼굴과 불일치 | **STORED, 문 안 염** |
| start | S3 대상 객체 문제 | | 200 `INVALID_S3_OBJECT` | 재촬영 | STORED |
| start | 권한/쓰로틀/AWS | | 200 `ACCESS_DENIED` / `THROTTLED` / `AWS_API_ERROR` / `SERVICE_ERROR` | 서비스 문제 | STORED |
| start | 얼굴은 맞았으나 start_retrieval 실패 (동시 RETRIEVING 등) | | 409 `RETRIEVAL_UNAVAILABLE` | 보관 정보 확인 | rate-limit 실패 |
| recover | 코드로 STORED/RETRIEVING 아님 | | 409 | 얼굴 재확인 또는 오류 | 없음 |
| complete | RETRIEVING 아님 | | 409 `RETRIEVAL_UNAVAILABLE` | recovery 재시도 | 변경 없음 |
| complete | 참조 delete 실패 | 로그만 | 200 RETRIEVED 유지 | 완료 화면 | **RETRIEVED**, `reference_face_deleted_at` 미설정 |
| 이미 RETRIEVED | 같은 코드 start | lookup 실패 | 409 `RETRIEVAL_UNAVAILABLE` | 보관번호 확인 | 그대로 RETRIEVED |
| RETRIEVING 재start | STORED가 아님 | | 409 | recover 경로 | RETRIEVING 유지 |
| lease 만료 | 120초 | `_recover_stale_retrievals` | — | 다시 얼굴 확인 | RETRIEVING→STORED, locker OCCUPIED |

명시적 error code (실제 존재):

```text
INVALID_RETRIEVAL_CODE
INVALID_TRANSACTION_ID
TARGET_FRAME_ID_REQUIRED
INVALID_TARGET_FRAME_ID
RETRIEVAL_UNAVAILABLE
RETRIEVAL_RATE_LIMITED
REFERENCE_FACE_MISSING
TARGET_FRAME_NOT_READY
TARGET_FRAME_TOO_OLD
FRAME_METADATA_INVALID
NO_FACE_IN_SOURCE_OR_TARGET
NO_FACE_IN_REFERENCE
NO_FACE_IN_TARGET
SIMILARITY_BELOW_THRESHOLD
SIMILARITY_ABOVE_THRESHOLD
INVALID_S3_OBJECT
ACCESS_DENIED
THROTTLED
AWS_API_ERROR
SERVICE_ERROR
BAD_REQUEST          (facecompare 내부; 키오스크 정상 경로는 거의 안 냄)
TRANSACTION_NOT_FOUND
STORE_FACE_NOT_VERIFIED   (STORE)
REFERENCE_SOURCE_MISSING  (STORE)
```

---

## 15. Retry 및 실패 횟수 정책

| 구분 | 현재 구현 |
| ---- | --------- |
| 프레임 미준비 | 프론트가 **같은 `targetFrameId`** 로 1.5s × 최대 15s 자동 재시도. 새 촬영 불필요 |
| 15초 타임아웃 | `FRAME_TIMEOUT` → 재촬영. 시도 횟수 미증가 |
| 얼굴 불일치/얼굴 없음 | 새 촬영(새 FRAME). 프론트 `verificationAttempts++`. 최대 **3** 후 mock HELP |
| 잘못된 보관번호 | backend `RetrievalAttemptLimiter`: 세션당 60초에 5회. 초과 429. **얼굴 실패는 여기 안 넣음** |
| `TARGET_FRAME_NOT_READY` vs 불일치 | 전자는 STORED + 폴링 + 시도/rate-limit 미소비. 후자는 STORED + 재촬영 + 프론트 시도 +1, backend limiter 미소비 |
| FastAPI store/complete 5xx | `getTransaction`으로 STORED면 복구, 아니면 사용자 재시도. complete는 멱등 |
| retrieve complete 모호 실패 | 상태 조회가 RETRIEVED면 완료로 간주 |
| backend AWS SDK retry | Kinesis 클라이언트 `retries.max_attempts=3`. facecompare/S3에는 별도 앱 루프 없음 |
| imageprocessor | 레코드 실패 시 해당 프레임 skip. 배치 전체 abort 없음. Kinesis 재처리 가능 |
| limiter 범위 | 프로세스 메모리. 재시작/멀티 워커에 공유 안 됨 |

```text
TARGET_FRAME_NOT_READY  → 같은 FRAME 재조회 (비동기 파이프라인 대기)
얼굴 불일치             → 새 FRAME 요구, Transaction 그대로
코드 오입력             → 별도 rate limit
```

---

## 16. 주요 FastAPI Endpoint

키오스크·인증·캡처는 모두 세션 필요 (`/api/login`, `/api/me`, `/healthz`, 정적 페이지 제외).

| Method | Path | 목적 | 주요 Request | 주요 Response | 호출 UI |
| ------ | ---- | ---- | ------------ | ------------- | ------- |
| POST | `/api/login` | 담당자 로그인 | username, password | `{ok, user}` + Set-Cookie | kiosk/operator 로그인 |
| POST | `/api/logout` | 세션 삭제 | — | `{ok}` | operator |
| GET | `/api/me` | 세션 확인 | cookie | `{authenticated, user}` | 양쪽 boot |
| GET | `/api/kiosk/lockers` | 보관함 목록 + cleanup | — | `{lockers:[{lockerId,status,updatedAt}]}` | LOCKER_SELECT |
| POST | `/api/kiosk/store/face-verify` | ID vs FRAME-A | imageBase64, contentType, filename, targetFrameId | success/matched/similarity/threshold/reason/targetFrameId | STORE WAITING_FOR_FRAME |
| POST | `/api/kiosk/store/reserve` | 원자 예약 | lockerId | transactionId, lockerId, status, reservationExpiresAt | LOCKER_RESERVING |
| POST | `/api/kiosk/store/cancel` | 예약 취소 | transactionId | `{transactionId, cancelled}` | 결제 취소/리셋 |
| POST | `/api/kiosk/store/complete` | 참조 복사 + STORED | transactionId | retrievalCode, mockPaymentId, amount, referenceFrameId, referencePresent | STORE_COMPLETING |
| GET | `/api/kiosk/transactions/{transaction_id}` | 복구용 상태 | path | 상태별 필드. S3 키 없음 | recovery |
| POST | `/api/kiosk/retrieve/start` | 코드+얼굴 후 RETRIEVING | retrievalCode, targetFrameId **필수** | 안전 얼굴 결과 + 성공 시 locker/transaction | RETRIEVE WAITING_FOR_FRAME |
| POST | `/api/kiosk/retrieve/recover` | RETRIEVING lease 복구 | retrievalCode | STORED 또는 RETRIEVING 페이로드 | 응답 유실 |
| POST | `/api/kiosk/retrieve/cancel` | RETRIEVING→STORED | transactionId | `{cancelled}` | 리셋/도움 |
| POST | `/api/kiosk/retrieve/complete` | RETRIEVED + 참조 삭제 | transactionId | lockerId, status | LOCKER_OPENING 후 |
| POST | `/api/capture-frame` | JPEG→Kinesis | imageBase64, frameCount | captureId, sequenceNumber, shardId | kiosk 촬영, operator 연속캡처 |
| GET | `/api/config` | API GW baseUrl+apiKey | — | 운영자 뷰어용. 실패 503 | operator `loadConfig` |
| GET/POST | `/api/detect-labels` | imageprocessor env 토글 | `{enabled}` | enabled, lastUpdateStatus | operator |
| GET | `/healthz` | liveness | — | `{status:ok}` | nginx/bootstrap |
| GET | `/` | 운영자 SPA | — | `index.html` | |
| GET | `/kiosk` | 키오스크 SPA | — | `kiosk.html` | |
| GET | `/{asset}` | 화이트리스트 정적 파일 | — | logo/favicon | |

KPI (`kpi-dashboard`, 포트 8000, Nginx `/dashboard/`):

| Method | Path | 목적 |
| ------ | ---- | ---- |
| GET | `/healthz` | liveness |
| GET | `/api/kpis` | CloudWatch/DDB 집계, 60초 캐시 |

API Gateway (데이터 스택, 키오스크 핵심 경로 아님):

| Method | Path | 목적 |
| ------ | ---- | ---- |
| GET | `/enrichedframe` | framefetcher, API Key 필요 |
| POST | `/face-compare` | facecompare, 운영자 레거시 최신 프레임 비교 |

민감 예시값(이미지, 시크릿, apiKey 값)은 적지 않는다.

---

## 17. 얼굴 인증 / Rekognition 처리

| 항목 | 현재 코드 |
| ---- | --------- |
| API | `CompareFaces`만. 키오스크 DetectFaces 없음 (`faceverifier`에 DetectFaces가 있으나 미연결) |
| STORE 비교 | Source = ID image Bytes / Target = FRAME-A S3 |
| RETRIEVE 비교 | Source = reference S3 / Target = FRAME-B S3 |
| 검색 형태 | **1:1**. 1:N SearchFaces / Collection **없음** |
| 업무 threshold | 90 (`FACE_SIMILARITY_THRESHOLD`, facecompare-params, kiosk.js `SIMILARITY_THRESHOLD`) |
| Rekognition API threshold | 0.0 — 매치를 버리지 않고 앱이 90과 비교 |
| QualityFilter | `NONE` |
| 최신 프레임 horizon | 5분 (`latest_frame_horizon_minutes`). exact mode 초과 시 `TARGET_FRAME_TOO_OLD` |
| DetectLabels | imageprocessor 옵션. CFN 기본 `ENABLE_DETECT_LABELS=false`. 운영자 UI가 런타임 토글 |
| 호출 경로 (키오스크) | FastAPI `lambda.invoke` RequestResponse. 브라우저→API GW 아님 |
| 호출 경로 (운영자) | 브라우저→API GW `/face-compare` (로그인 후 `/api/config`의 apiKey) |

facecompare reason → HTTP (API GW 직접 호출 시): 비교 결과는 대체로 200, 입력 오류 400, ACCESS_DENIED 500, THROTTLED 503, AWS_API_ERROR 502. FastAPI는 invoke 후 body의 `reason`을 읽어 키오스크에는 보통 200 JSON으로 감싼다.

---

## 18. 개인정보 및 보안 구조

| 항목 | 현재 구현 |
| ---- | --------- |
| 브라우저 AWS credential | **없음**. boto3는 FastAPI/Lambda/EC2 role |
| 브라우저 S3 직접 호출 | 키오스크 **없음**. 운영자 뷰어만 framefetcher **presigned GET** |
| ID 이미지 persist | **안 함**. 비교 요청 메모리/Lambda payload만 |
| SQLite 생체 데이터 | **안 함**. 키/프레임 id 포인터만 |
| DynamoDB 이미지/임베딩 | **안 함** |
| S3 이미지 | JPEG bytes, 앱 암호화 없음 |
| S3 Block Public Access | **CFN에 미설정**. 계정 기본값에 의존. 실버킷 현재 없음 |
| S3 SSE | 템플릿에 `BucketEncryption` 없음. put_object에 SSE 인자 없음. **명시적 SSE-KMS 없음** |
| Application-level encryption | **미구현** |
| KMS | SSM SecureString 복호화만. 이미지 envelope 없음 |
| HTTPS | EC2 Nginx self-signed TLS. 로컬은 HTTP + `WEBUI_SESSION_HTTPS_ONLY=false` |
| 세션 | Starlette signed HttpOnly cookie. code-server 프록시 `%3D` 호환 미들웨어 |
| 로그인 | PBKDF2-SHA256 해시만 저장 (SSM/env). 평문 비밀번호 저장 없음 |
| IAM runtime | EC2 role: Kinesis put, DDB GetItem, S3 frames/locker-references, facecompare invoke, imageprocessor config, CFN/API key 조회, KPI logs. Lambda별 분리 정책 |
| least privilege | 앱 스택은 prefix/리소스 한정. imageprocessor S3는 버킷 전체 Get/Put/Delete로 더 넓음 |
| S3 lifecycle | `frames/` 30일, `logging/` 14일, `locker-references/` 없음 |
| reference 삭제 | RETRIEVED 후 기본 즉시. 실패 시 객체 잔존 가능 |
| DynamoDB TTL | `expire_at` + 30일 |
| 로그 | frame_id, reason, sizes. 코드가 image bytes/base64/signed URL/쿠키 로깅을 금지. DetectLabels 라벨명·에러 문자열은 CloudWatch에 남을 수 있음 |
| API Key | 정적 파일로 배포하지 않음. 로그인 후 `/api/config`. 운영자 브라우저 메모리에만 |
| 키오스크 잠금 | 시민 이용 전 담당자 로그인 필요 |
| TLS 인증서 | self-signed. 브라우저 경고 정상. getUserMedia용 HTTPS 목적 |

```text
현재: 전송 구간 HTTPS(배포 시) + IAM + 비공개 버킷 전제 + SSE는 버킷/계정 기본값
      객체 본문은 평문 JPEG
미구현: Application-level encryption, 이미지용 SSE-KMS, 명시적 Block Public Access
향후 보안 개선안: KMS envelope, BPA 강제, locker-references 수명 정책, 구조화 audit trail
```

---

## 19. 데이터 생성 → 저장 → 사용 → 삭제 Lifecycle

```text
[ID 파일]
  생성: 키오스크 파일 선택 (브라우저)
  저장: 없음
  사용: STORE face-verify 1회
  삭제: 화면 reset / 페이지 종료 (서버에 잔존 없음)

[FRAME-A / FRAME-B JPEG]
  생성: canvas → POST /api/capture-frame → Kinesis pickle ImageBytes
  저장: S3 frames/.../<frame_id>.jpg + DDB 메타
  사용: CompareFaces Target; STORE 성공 시 FRAME-A가 참조 원본
  삭제: S3 lifecycle 30일; DDB TTL expire_at ≈ 30일

[reference.jpg]
  생성: STORE complete CopyObject
  저장: locker-references/<transaction_id>/reference.jpg + SQLite 포인터
  사용: RETRIEVE CompareFaces Source
  삭제: RETRIEVED 후 delete_object (기본). lifecycle 없음
        retention>0 이면 삭제되지 않음 (워커 없음)

[Transaction / locker]
  생성: reserve
  저장: SQLite WAL
  사용: 전 상태기계
  삭제: 코드상 DELETE 없음. 상태만 RETRIEVED/CANCELLED/EXPIRED

[운영자 presigned URL]
  생성: framefetcher 조회 시
  저장: 없음 (만료 초 단위, IAM 임시자격에도 묶임)
  사용: <img>
  삭제: 만료
```

---

## 20. 현재 구현 / 부분 구현 / 미구현 기능

| 기능 | 현재 구현 | 부분 구현 | 미구현/향후 개선 |
| ---- | --------- | --------- | ---------------- |
| STORE 본인 인증 (ID vs FRAME-A) | O | | |
| RETRIEVE 동일인 인증 (reference vs FRAME-B) | O | | |
| Exact Frame Correlation (`captureId`=`frame_id`=`targetFrameId`) | O (키오스크) | 운영자 UI는 전역 최신 경로 유지 | |
| Transaction state machine | O | | |
| S3 reference face | O | | |
| Reference cleanup | O (즉시 삭제 기본) | `POST_RETRIEVAL_RETENTION_SECONDS>0`은 로그만 | 지연 삭제 잡 |
| 얼굴 반복 실패 제동 | | 프론트 3회→HELP. backend limiter는 **코드 오입력**만 | 서버측 생체 lockout, 영속 카운터 |
| 보이스피싱 경고 | | | 코드 없음 |
| 역무원 호출 | | HELP 화면 + `MockHelpAdapter` | 실제 호출/티켓 |
| Incident DB | | | 테이블/API 없음 |
| Incident Dashboard | | KPI는 파이프라인 지표만 | 사건 보드 없음 |
| 다국어 | | 한국어 고정 | i18n 없음 |
| 큰 글씨 | | `convenienceMode` CSS | |
| 음성 안내 | | convenience 시 `speechSynthesis` ko-KR | 상시 음성/TTS 서비스 없음 |
| KMS image encryption | | SSM만 KMS | 객체 envelope/SSE-KMS |
| audit log | | CloudWatch JSON, SQLite 시각 컬럼 | 불변 audit store 없음 |
| CCTV 이상탐지 | | DetectLabels + watch list 코드 | 기본 꺼짐, watch list 빈 배열, SNS 미설정 |
| Culture CPTED | | | 발표 아이디어만 (`해커톤발표플로우.md`) |
| 물리 locker I/O | | mock delay | 하드웨어 API 없음 |
| 실제 결제 | | mock 2000원 / PAY- | PG 없음 |
| 모바일 신분증 | | 파일 업로드 데모 | 승인된 ID 연동 없음 |
| Rekognition Collection / Person ID | | | 의도적으로 없음 |
| faceverifier Lambda | | 소스+유닛테스트만 | CFN/키오스크 미연결 |
| 2대 EC2 (WebUI+KPI) | | | **폐기.** 1대 ApplicationInstance |
| Application / Data 스택 실배포 | | IaC·부트스트랩 완비 | **현재 계정 스택 DELETE_COMPLETE** |

---

## 21. 주요 소스코드 위치

| 영역 | 경로 | 심볼 |
| ---- | ---- | ---- |
| FastAPI 앱 | `web-ui/backend/app.py` | `capture_frame`, `kiosk_store_*`, `kiosk_retrieve_*`, `_load_gateway_config` |
| 거래 DB | `web-ui/backend/kiosk_store.py` | `KioskStore`, `RetrievalAttemptLimiter` |
| 참조 얼굴 | `web-ui/backend/reference_face.py` | `ensure_reference_copy`, `compare_id_to_frame`, `compare_reference_to_frame` |
| 설정 | `web-ui/backend/config.py` | env 12-factor |
| 인증 | `web-ui/backend/auth.py` | `hash_password`, `verify_password` |
| 프록시 쿠키 | `web-ui/backend/session_cookie_compat.py` | `CodeServerSessionCookieCompatMiddleware` |
| 키오스크 UI | `web-ui/kiosk.html`, `web-ui/src/kiosk.js` | `PipelineService`, `KioskTransactionService`, Vue state machine |
| 운영자 UI | `web-ui/index.html`, `web-ui/src/app.js` | capture loop, `/enrichedframe`, `/face-compare` |
| imageprocessor | `lambda/imageprocessor/imageprocessor.py` | `_resolve_frame_id`, `process_image` |
| facecompare | `lambda/facecompare/facecompare.py` | `run`, `get_frame_by_id`, `compare_faces` |
| framefetcher | `lambda/framefetcher/framefetcher.py` | `fetch_frames` |
| faceverifier (미배포) | `lambda/faceverifier/faceverifier.py` | `face_verify` |
| 레거시 캡처 | `client/video_cap.py` | CaptureId 없음 |
| Data CFN | `aws-infra/aws-infra-cfn.yaml` | |
| App CFN | `aws-infra/aws-infra-ec2-cfn.yaml` | |
| Nginx | `aws-infra/nginx/application.conf` | |
| Bootstrap | `aws-infra/userdata/application-bootstrap.sh` | |
| 빌드/배포 | `build.py` | `packagelambda`, `createstack`, `createec2stack`, `publishapps` |
| KPI | `kpi-dashboard/backend/{app,kpis,config}.py` | |
| 파라미터 | `config/*.json` | |
| 테스트 | `tests/test_app_kiosk.py`, `test_kiosk_store.py`, `test_facecompare_correlation.py`, `kiosk_correlation_test.js` | |

---

## 22. 핵심 요약

1. **맡기기:** 로그인 → 동의 → ID 파일 → 라이브 촬영 → FastAPI가 FRAME-A(`captureId`) 발급 → Kinesis/imageprocessor가 같은 id로 S3/DDB 기록 → ID vs FRAME-A CompareFaces ≥90 → 세션 바인딩 → locker RESERVED → mock 결제 → FRAME-A를 `locker-references/<tx>/reference.jpg`로 복사 → STORED + 8자리 코드 → mock 문 열림.
2. **찾기:** 8자리 코드 → 새 촬영 FRAME-B → 서버가 STORED 거래의 reference vs FRAME-B CompareFaces ≥90 → 그제야 RETRIEVING → mock 문 열림 → RETRIEVED → 기본 참조 삭제.
3. **FRAME-A / FRAME-B**는 사람 ID가 아니라 **각 촬영의 UUIDv4**. `frame_id` = 처리된 그 촬영.
4. **얼굴 JPEG는 SQLite/DynamoDB에 없다.** S3에만 JPEG bytes.
5. **SQLite:** locker + transaction 메타, 참조 키/프레임 id, 상태/시각. WAL.
6. **DynamoDB EnrichedFrame:** `frame_id` PK + S3 포인터 + 타임스탬프 + optional labels + TTL. 이미지/임베딩 없음.
7. **S3 `frames/`는 `put_object(Body=img_bytes)` JPEG.** Base64 문자열이 아니다.
8. **참조 얼굴**은 STORE complete 때 CopyObject로 생기고, RETRIEVED 직후 기본 삭제된다. `frames/` lifecycle과 분리.
9. **Rekognition**은 STORE에서 ID Bytes vs FRAME-A, RETRIEVE에서 reference.jpg vs FRAME-B. 둘 다 1:1 CompareFaces.
10. **사용 AWS:** EC2, EBS, EIP, IAM, SSM, (SSM용 KMS), Nginx(호스트), Kinesis, Lambda 3, DynamoDB, S3, Rekognition, API Gateway, CloudFormation, CloudWatch, Event Source Mapping. Scheduler는 옵션. SNS는 코드만.
11. **Application Stack** = 1 EC2 + Nginx + FastAPI + KPI + SQLite EBS. **Data Stack** = 프레임 파이프라인. 서로 독립 삭제.
12. **얼굴 불일치:** STORED 유지, 문 안 염, 프론트 재촬영(3회 후 mock 도움). 서버 생체 lockout 없음.
13. **프레임 처리 지연:** `TARGET_FRAME_NOT_READY` + 같은 id 폴링. 실패 횟수 아님.
14. **S3 오류:** STORE complete는 502, 거래 RESERVED. RETRIEVE는 200 reason, STORED. 삭제 실패는 RETRIEVED 유지.
15. **상태 꼬임 방지:** 원자 SQL, partial unique, complete 멱등, RETRIEVING 비재claim, 만료 cleanup, 프론트 recovery.
16. **개인정보 위치:** 브라우저 일시 메모리, Kinesis 일시 pickle, Lambda 메모리, S3 JPEG, (운영자) presigned URL. SQLite/DDB는 포인터만.
17. **보안 구현 vs 공백:** IAM+세션+비공개 전송+참조 분리/삭제는 있음. 이미지 KMS/앱 암호화, 명시적 BPA, 실 locker/결제/역무원/Incident/CPTED는 없음.
18. **실환경:** 2026-08-13 기준 `video-analyzer-stack`과 `video-analyzer-ec2-stack`은 **DELETE_COMPLETE**. 로컬 코드는 구현되어 있으나 AWS 데이터면은 다시 배포해야 동작한다.

```text
현재 코드 기준으로는
  키오스크 얼굴 비교 = FastAPI → Lambda invoke → CompareFaces
  배포 앱 호스트 = EC2 1대
  데이터/앱 스택 = 계정에 없음 (삭제됨)
문서(구 README)의 2대 EC2·키오스크 /face-compare 직접 호출은 구버전이다.
```
