# 거래 바인딩 참조 얼굴 기반 물품 찾기 — 최종 결과 보고

**일자:** 2026-08-11  
**브랜치:** `devhsj`  
**커밋:** 없음 (명시적으로 커밋하지 않음)

---

## 요약

서버 강제 물품 찾기 흐름을 구현했습니다.

```text
retrieval_code
  → STORED 거래 1건
  → 거래 참조 얼굴 (FRAME-A 사본)
  → 현재 촬영 FRAME-B (exact)
  → Rekognition CompareFaces ≥ 90
  → 서버 인가 후 보관함 개방
```

| 항목 | 결과 |
|---|---|
| 코드 구현 | **완료** |
| SQLite 마이그레이션 | **완료** (비파괴 ALTER) |
| STORE 신뢰 얼굴 바인딩 | **완료** (`/api/kiosk/store/face-verify` + 세션) |
| 내구성 있는 S3 참조 객체 | **완료** (`locker-references/<tx>/reference.jpg`) |
| RETRIEVE 얼굴 게이트 | **완료** (`retrieve/start`에 code + targetFrameId 필수) |
| RETRIEVE UX (신분증 재확인/보관함 번호 입력 제거) | **완료** |
| facecompare 참조 S3 모드 | **완료** (기존 ID-bytes 모드 유지) |
| 단위/통합 테스트 | **108 passed** |
| 라이브 긍정/부정 E2E | **차단** — 데이터 스택 리소스 부재 |
| CloudFormation 변경 | **없음** (lifecycle가 이미 prefix 단위) |
| Git 커밋 | **없음** |

---

## 1. 변경 파일

### 이번 기능

| 영역 | 파일 |
|---|---|
| Backend | `web-ui/backend/kiosk_store.py`, `config.py`, `app.py`, **신규** `reference_face.py`, `.env.example` |
| Lambda | `lambda/facecompare/facecompare.py` |
| UI | `web-ui/src/kiosk.js`, `web-ui/kiosk.html` |
| Tests | `tests/test_kiosk_store.py`, `test_app_kiosk.py`, `test_facecompare_correlation.py`, `kiosk_correlation_test.js` |
| Docs | `docs/KIOSK_UI.md`, `2026_08_10_grok_report_01.md`, `2026_08_11_retrieval_reference_face_result.md`, 본 파일 |

이전 단계의 미커밋 작업(세션 인증, imageprocessor 등)은 되돌리지 않고 유지했습니다.

---

## 2. SQLite 마이그레이션

`KioskStore.initialize()`에서:

1. 신규 DB: `CREATE TABLE IF NOT EXISTS`에 참조 얼굴 컬럼 포함
2. 기존 DB: `_migrate_transaction_columns()`가 `PRAGMA table_info` 후 없는 컬럼만  
   `ALTER TABLE transactions ADD COLUMN ...`
3. 인덱스 `idx_transactions_retrieval_code_status` 보장
4. 테이블 drop/재생성 없음, 기존 행 유지

### 추가 컬럼

| 컬럼 | 용도 |
|---|---|
| `reference_frame_id` | STORE 시 검증 통과한 라이브 프레임 UUID (상관/감사) |
| `reference_face_s3_key` | 거래 범위 참조 얼굴 S3 키 |
| `reference_face_created_at` | 참조 객체 생성 시각 |
| `reference_face_deleted_at` | 찾기 완료 후 삭제 시각 |

### 저장하지 않음

- face embedding / face vector
- raw image BLOB
- ID image
- base64

### 조회 성능

- `retrieval_code` UNIQUE 유지
- `(retrieval_code, status)` 인덱스 추가  
- `WHERE retrieval_code = ? AND status = 'STORED'` 효율 유지

---

## 3. 최종 거래 스키마 (관련 필드)

| 필드 | 역할 |
|---|---|
| `transaction_id` | 내부 UUID |
| `retrieval_code` | 8자리 조회 식별자 (UNIQUE) |
| `locker_id` | 얼굴 검증 성공 후 열 수 있는 자원 |
| `status` | RESERVED / STORED / RETRIEVING / RETRIEVED / … |
| `reference_frame_id` | FRAME-A 상관 |
| `reference_face_s3_key` | 비교용 내구성 소스 |
| `reference_face_created_at` / `reference_face_deleted_at` | 생명주기 감사 |

---

## 4. STORE 검증 프레임 바인딩

1. 브라우저 촬영 → `captureId = FRAME-A` (`POST /api/capture-frame`)
2. `POST /api/kiosk/store/face-verify`  
   - 입력: ID 이미지 + `targetFrameId=FRAME-A`  
   - FastAPI → facecompare Lambda
3. 유사도 ≥ 90이면 서명 세션에만 저장:
   - `verified_store_frame_id`
   - `verified_store_at`  
   - **이미지/base64는 세션에 넣지 않음**
4. `POST /api/kiosk/store/complete`는 세션의 검증 프레임만 사용  
   - 클라이언트가 임의 `reference_frame_id`를 고를 수 없음  
   - 미검증 시 `403 STORE_FACE_NOT_VERIFIED`

---

## 5. 일반 프레임 TTL을 넘는 내구성

**예.**  

기존 CloudFormation S3 lifecycle는 **`frames/`**, **`logging/`** prefix만 만료합니다.  
`locker-references/`는 해당 prefix 밖이므로 일반 프레임 만료와 무관하게 유지됩니다.

- 새 버킷 생성 없음  
- CloudFormation 배포 없음 (prefix 분리가 이미 안전)

---

## 6. S3 참조 객체 전략

| 항목 | 값 |
|---|---|
| 버킷 | `FRAME_S3_BUCKET` (imageprocessor와 동일 frames 버킷) |
| 키 | `locker-references/<transaction_id>/reference.jpg` |
| 멱등성 | `head_object` 후 없으면 `copy_object`; 있으면 재사용 |
| 공개 접근 | 없음 (private; 브라우저에 키 미노출) |

STORE complete 재시도 시 참조 객체를 여러 개 만들지 않습니다.

---

## 7. RETRIEVE API 흐름

```http
POST /api/kiosk/retrieve/start
{
  "retrievalCode": "12345678",
  "targetFrameId": "<FRAME-B uuid>"
}
```

서버 처리:

1. Rate limit 확인  
2. 8자리 ASCII 숫자 + UUID 프레임 검증  
3. `SELECT … WHERE retrieval_code=? AND status='STORED'`  
4. `reference_face_s3_key` 필수  
5. facecompare **reference S3 모드**로 참조 vs exact FRAME-B 비교  
6. 매칭 시에만 `start_retrieval` → `RETRIEVING`  
7. 안전 JSON 반환

```http
POST /api/kiosk/retrieve/complete
{ "transactionId": "..." }
```

→ `RETRIEVED` + (설정 시) 참조 객체 삭제

### 상태 머신

```text
STORED
  + 올바른 코드 + 얼굴 일치
  → RETRIEVING
  → locker open
  → retrieve/complete
  → RETRIEVED

얼굴 불일치 / TARGET_FRAME_NOT_READY / 잘못된 코드 / 참조 없음
  → STORED 유지 (보관함 미개방)
```

기존 stale `RETRIEVING` lease 복구(`KIOSK_RETRIEVAL_SECONDS`)는 유지됩니다.

---

## 8. RETRIEVE UX

```text
[찾기 선택]
    ↓
[8자리 보관 코드 입력]
    ↓
[얼굴 촬영]
    ↓
[본인 확인 중]
    ↓
[본인 확인 완료]
    ↓
[07번 보관함을 엽니다]
```

| 항목 | 결과 |
|---|---|
| RETRIEVE 중 신분증 재확인 | **제거됨** |
| 수동 보관함 번호 입력 | **제거됨** (거래에서 결정) |
| STORE 최초 신원 확인 | **유지** (ID + 얼굴) |

진행 표시(RETRIEVE): `보관번호 → 얼굴 확인 → 찾기`

---

## 9. retrieval_code 조회

- 정확히 8 ASCII 숫자  
- 단일 `STORED` 거래만 조회  
- 생체 검증 전 `reference_face_s3_key` 등 내부 메타를 브라우저에 노출하지 않음  
- 실패 rate limit 유지 (세션당 기본 5회/60초)

보안 모델:

| 요소 | 역할 |
|---|---|
| `retrieval_code` | 거래 조회 식별자 |
| reference face | 생체 소유 검증자 |
| `locker_id` | 검증 성공 후에만 개방 가능한 자원 |

---

## 10. FRAME-A / FRAME-B

| 캡처 | 역할 |
|---|---|
| FRAME-A | STORE 검증 통과 라이브 얼굴 → `reference_frame_id` + 내구성 S3 사본 |
| FRAME-B | RETRIEVE 현재 촬영; exact target; **FRAME-A와 다름이 정상** |

비교는 `FRAME-A == FRAME-B`가 아니라 **FACE(참조) vs FACE(FRAME-B)** 입니다.  
Phase 2A: `TARGET_FRAME_NOT_READY` 시 **동일 FRAME-B**만 재시도 (전역 latest 금지).

---

## 11. 얼굴 비교 결과 계약 (브라우저 안전 필드)

반환 가능:

- `success`, `matched`, `similarity`, `threshold`, `reason`
- `targetFrameId`
- 인가 성공 후에만: `transactionId`, `lockerId`, `additionalFee`, `status`

reason 예:

- `SIMILARITY_ABOVE_THRESHOLD`
- `SIMILARITY_BELOW_THRESHOLD`
- `TARGET_FRAME_NOT_READY`
- `REFERENCE_FACE_MISSING`
- `NO_FACE_IN_REFERENCE` / `NO_FACE_IN_TARGET`
- `INVALID_RETRIEVAL_CODE`
- `RETRIEVAL_UNAVAILABLE`

**절대 반환하지 않음:** S3 키, raw image, base64, 세션 시크릿, API key

---

## 12. 서버 측 우회 방지

| 시도 | 결과 |
|---|---|
| 보관번호만으로 retrieve/start | `400 TARGET_FRAME_ID_REQUIRED` |
| 잘못된 코드 | `409 RETRIEVAL_UNAVAILABLE` (locker 정보 없음) |
| 얼굴 불일치 | `matched=false`, **STORED** 유지 |
| 프레임 미준비 | `TARGET_FRAME_NOT_READY`, **STORED** 유지 |
| 참조 없음 | `REFERENCE_FACE_MISSING`, **STORED** 유지 |
| 브라우저가 참조 얼굴 선택 | 불가 (서버가 SQLite에서 해석) |

코드 전용 보관함 개방 엔드포인트는 없습니다.

---

## 13. TARGET_FRAME_NOT_READY / 임계값 / 재시도

| 케이스 | 동작 |
|---|---|
| `TARGET_FRAME_NOT_READY` | STORED 유지; 동일 `targetFrameId` 재시도; 생체 실패 횟수 미소비; rate-limit 실패로 계산하지 않음 |
| `SIMILARITY_BELOW_THRESHOLD` | 실제 얼굴 검증 실패; STORED 유지 |
| 임계값 | **90** (`FACE_SIMILARITY_THRESHOLD`) |
| 최대 얼굴 시도 | 기존 브라우저 `MAX_VERIFICATION_ATTEMPTS=3` 유지 |

---

## 14. 참조 얼굴 정리

| 상태 | 참조 객체 |
|---|---|
| STORED / RETRIEVING | 유지 (삭제 금지) |
| RETRIEVED | 삭제 대상 |
| 데모 기본 | complete 직후 즉시 삭제 |

설정:

```text
KIOSK_REFERENCE_DELETE_ON_RETRIEVE=true
KIOSK_REFERENCE_POST_RETRIEVAL_RETENTION_SECONDS=0
```

법적 보존 기간을 임의로 정하지 않았습니다. 프로젝트 문서상 locker-reference 전용 법적 기간 충돌은 없었고, 일반 `frames/` lifecycle(예: 30일)과 분리된 애플리케이션 정리를 사용합니다.

---

## 15. 개인정보 / 보안

- face embedding 미저장  
- ID 이미지 미영속화  
- 로그에 이미지/base64/서명 URL/쿠키/자격증명 미기록  
- 안전 로그: `transaction_id`, `frame_id`, `matched`, `similarity`, `reason`, `reference_present`  
- retrieval_code 평문 로그 최소화  

---

## 16. facecompare 확장 (하위 호환)

신규 모드 (서버 전용, 브라우저 미사용):

```json
{
  "referenceS3Bucket": "...",
  "referenceS3Key": "locker-references/<tx>/reference.jpg",
  "targetFrameId": "<FRAME-B>",
  "similarityThreshold": 90
}
```

- `imageBase64`와 상호 배타  
- prefix 허용: `locker-references/`, `frames/`  
- 기존 ID 이미지 vs `targetFrameId` / legacy latest 모드 유지  

---

## 17. 설정 항목

```text
FRAME_S3_BUCKET=aws-face-detection-frames-...
ENRICHED_FRAME_TABLE=EnrichedFrame
FACECOMPARE_FUNCTION_NAME=facecompare
FACE_SIMILARITY_THRESHOLD=90
KIOSK_REFERENCE_S3_PREFIX=locker-references/
KIOSK_REFERENCE_DELETE_ON_RETRIEVE=true
KIOSK_REFERENCE_POST_RETRIEVAL_RETENTION_SECONDS=0
```

---

## 18. 테스트 결과

```text
Full suite: 108 passed (~2.7s)
python -m py_compile (변경 Python 파일): OK
node --check web-ui/src/kiosk.js: OK
node --check web-ui/src/app.js: OK
git diff --check: OK
Hanging tests: 없음
```

주요 검증:

- A. STORE 성공 검증 프레임 → `reference_frame_id`
- B. 미검증 임의 프레임 거부
- C. complete 재시도 시 S3 참조 1개
- D. 코드 → 단일 STORED 거래
- E. 올바른 코드 + 매칭 얼굴 → RETRIEVING + locker
- F. 올바른 코드 + 비매칭 → STORED
- G. 잘못된 코드 → 인가 없음
- H. TARGET_FRAME_NOT_READY 재시도 / STORED
- I. 참조 없음 안전 실패
- J–Q. cleanup, stale recovery, 8자리 검증, rate limit, STORE 멱등, Phase 2A, legacy compare

---

## 19. 라이브 E2E

**실행하지 못함.** 2026-08-11 읽기 전용 AWS 점검 결과:

| 리소스 | 상태 |
|---|---|
| `video-analyzer-stack` | 없음 |
| FrameStream | 없음 |
| EnrichedFrame | 없음 |
| frames S3 버킷 | 없음 |
| facecompare Lambda | 없음 |

이 작업 지시: 해당 스택을 재생성하지 말 것.  
단위 테스트는 DynamoDB/S3/Lambda를 전부 mock 합니다.

스택 복구 후 권장 E2E:

**긍정**

1. ID + 라이브 얼굴 검증 (FRAME-A)  
2. STORE complete → retrieval_code  
3. SQLite에 `reference_frame_id=FRAME-A`, `reference_face_s3_key` 존재  
4. S3 참조 객체 존재 (이미지 내용은 표시하지 않음)  
5. RETRIEVE: 코드 입력 + FRAME-B 촬영 (FRAME-B ≠ FRAME-A)  
6. 유사도 ≥ 90 → STORED → RETRIEVING → 올바른 locker  
7. complete → RETRIEVED → 참조 정리  

**부정**

- 올바른 코드 + 다른 얼굴 → `matched=false`, STORED 유지, 보관함 미개방  
- 임계값 90을 데모 통과용으로 낮추지 않음  

---

## 20. AWS / 인프라

| 변경 | 수행 여부 |
|---|---|
| CFN lifecycle로 `locker-references/` 제외 | **불필요** (이미 frames/logging만 만료) |
| 신규 버킷 | **없음** |
| IAM 확대 배포 | **없음** |

런타임 필요 권한 (스택 복구 후 FastAPI 역할):

- DynamoDB `GetItem` on EnrichedFrame  
- S3 Get/Put/Copy/Delete on frames 버킷 (`frames/*`, `locker-references/*`)  
- Lambda `Invoke` on facecompare  

현재 해커톤 역할은 프로젝트 리소스가 존재할 때 S3/DynamoDB/Lambda에 광범위 권한이 있습니다.

---

## 21. 문서

| 문서 | 내용 |
|---|---|
| `docs/KIOSK_UI.md` | STORE/RETRIEVE 신흐름, 스키마, API, 보안 모델 |
| `2026_08_10_grok_report_01.md` | `## Transaction-Bound Reference Face Retrieval` 섹션 추가 |
| `2026_08_11_retrieval_reference_face_result.md` | 상세 기술 결과 |
| `2026_08_11_retrieval_reference_face_final_report.md` | 본 최종 보고 |

---

## 22. 아키텍처 불변식 (최종)

```text
SQLite
  retrieval_code
      ↓
  transaction
      ↓
  locker_id
    + reference_frame_id
    + reference_face_s3_key
            ↓
  transaction-bound reference face (S3)

current camera
      ↓
  captureId = FRAME-B
      ↓
  DynamoDB/S3 exact current frame

reference face  vs  current exact frame
      ↓
  Rekognition CompareFaces
      ↓
  similarity >= 90
      ↓
  server authorization
      ↓
  locker open
```

**구현하지 않음**

- `retrieval_code` → 즉시 보관함 개방  
- 현재 얼굴 → 전역 face search  
- SQLite face embedding 저장  

---

## 23. 커밋

**아무것도 커밋하지 않았습니다.**

작업 트리에는 이전 단계 미커밋 변경과 이번 기능 변경이 함께 남아 있습니다.

---

## 24. 체크리스트 (요청 항목 매핑)

| # | 항목 | 결과 |
|---|---|---|
| 1 | 변경 파일 | 위 §1 |
| 2 | SQLite 마이그레이션 | 비파괴 ALTER 완료 |
| 3 | 최종 스키마 필드 | reference_* 4컬럼 |
| 4 | STORE 검증 프레임 바인딩 | face-verify + 세션 |
| 5 | 일반 프레임 TTL 이후 내구성 | locker-references/ 유지 |
| 6 | S3 참조 전략 | 결정적 키 + 멱등 copy |
| 7 | RETRIEVE API 흐름 | code + targetFrameId → 비교 → RETRIEVING |
| 8 | RETRIEVE ID 제거 | 예 |
| 9 | 수동 보관함 번호 제거 | 예 |
| 10 | retrieval_code 조회 | 단일 STORED |
| 11 | FRAME-A 참조 | 예 |
| 12 | FRAME-B 현재 촬영 | exact, Phase 2A |
| 13 | 결과 계약 | 안전 필드만 |
| 14 | 우회 방지 | 서버 강제 |
| 15 | TARGET_FRAME_NOT_READY | STORED + 동일 프레임 재시도 |
| 16 | 유사도 임계값 | 90 |
| 17 | 참조 정리 | RETRIEVED 후 데모 즉시 삭제 |
| 18 | 개인정보/보안 | embeddings 없음, 이미지 미로그 |
| 19 | 타깃 테스트 | 통과 |
| 20 | 전체 테스트 | 108 passed |
| 21 | 라이브 긍정 E2E | 스택 부재로 미실행 |
| 22 | 라이브 부정 E2E | 스택 부재로 미실행 |
| 23 | AWS/infra 변경 | 없음 |
| 24 | 문서 갱신 | 완료 |
| 25 | 커밋 없음 | 확인 |

---

*관련 상세 기술 노트: `2026_08_11_retrieval_reference_face_result.md`*
