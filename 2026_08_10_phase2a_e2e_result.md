# Phase 2A Exact-Frame Correlation AWS E2E 결과

- 검증일: 2026-08-10
- AWS 계정: `115019372648`
- 리전: `ap-northeast-2`
- 데이터 스택: `video-analyzer-stack`
- 최종 분류: **VERIFIED END-TO-END**
- 커밋: 없음

## 1. 결론

현재 복구된 AWS 인프라를 대상으로 Phase 2A의 핵심 불변식을 런타임에서
확인했다.

```text
captureId == DynamoDB frame_id == kiosk targetFrameId
```

A/B 동시성 테스트에서 B가 A보다 나중에 처리된 상태에서도
`targetFrameId=A` 요청은 A를, `targetFrameId=B` 요청은 B를 정확히 선택했다.
exact mode는 전역 최신 프레임으로 fallback하지 않았다.

검증 시작 시 imageprocessor 배포 ZIP의 누락 의존성 때문에 Kinesis 처리가
실패하는 실제 결함을 발견했다. Phase 2A 설계는 변경하지 않고 Python 3.12
표준 라이브러리 `zoneinfo`로 최소 수정한 뒤 imageprocessor 코드만 갱신했다.
IAM, CloudFormation, EC2, 세션 인증은 변경하지 않았다.

## 2. AWS 리소스 상태

| 리소스 | 결과 |
|---|---|
| `video-analyzer-stack` | `CREATE_COMPLETE` |
| Kinesis `FrameStream` | `ACTIVE`, open shard 1 |
| Lambda `imageprocessor` | `Active`, update successful |
| Lambda `facecompare` | `Active`, update successful |
| Lambda `framefetcher` | `Active`, update successful |
| DynamoDB `EnrichedFrame` | `ACTIVE`, PK=`frame_id` |
| FrameStream → imageprocessor mapping | `Enabled`, 수정 후 `LastProcessingResult=OK` |
| API Gateway stage | `development` |
| `GET /enrichedframe` | 존재 |
| `POST /face-compare` | 존재 |

API Gateway의 `POST /face-compare`는 AWS_PROXY integration으로 현재
`arn:aws:lambda:ap-northeast-2:115019372648:function:facecompare`를 호출한다.

배포 전 확인한 세 Lambda CodeSha256는 당시 로컬 `build/*.zip`과 일치했다.
imageprocessor 수정 후에는 새 배포 ZIP의 CodeSha256와 배포 함수의 값이 다시
일치함을 확인했다.

## 3. Phase 2A 코드 수명주기

### 3.1 `captureId` / `CaptureId`

생성 위치:

```text
web-ui/backend/app.py
POST /api/capture-frame
```

동작:

1. FastAPI가 `str(uuid.uuid4())`로 canonical UUIDv4를 한 번 생성한다.
2. 기존 Kinesis package 필드를 유지한다.
3. 동일 값을 package의 `CaptureId`에 추가한다.
4. `pickle.dumps(frame_package)`로 직렬화해 `FrameStream`에 PutRecord한다.
5. 동일 값을 응답의 `captureId`로 브라우저에 반환한다.

Kinesis package 필드:

```text
ApproximateCaptureTime
FrameCount
ImageBytes
CaptureId
```

원본 이미지 bytes와 직렬화된 레코드는 출력하거나 보고서에 기록하지 않았다.

### 3.2 `frame_id`

소비/저장 위치:

```text
lambda/imageprocessor/imageprocessor.py
```

`_resolve_frame_id()` 동작:

- `CaptureId`가 존재하면 canonical UUIDv4인지 검사하고 그대로 반환한다.
- `CaptureId`가 없는 legacy producer 레코드는 기존처럼 새 UUIDv4를 생성한다.
- 잘못된 `CaptureId`는 다른 ID로 바꾸지 않고 해당 레코드를 거부한다.

동일 `frame_id`는 다음 위치에 사용된다.

- DynamoDB `EnrichedFrame.frame_id`
- S3 객체 파일명 `<frame_id>.jpg`
- 구조화 로그의 안전한 correlation ID

### 3.3 `targetFrameId`

생성/전달 위치:

```text
web-ui/src/kiosk.js
```

키오스크는 `/api/capture-frame` 응답의 `capture.captureId`를
`currentCaptureId`에 보관한 뒤 같은 값을
`waitForExactComparison(idImage, currentCaptureId, ...)`에 전달한다.

`PipelineService.compareFace()`는 API Gateway 요청에 다음 필드를 포함한다.

```text
targetFrameId: targetFrameId
```

### 3.4 facecompare exact lookup

소비 위치:

```text
lambda/facecompare/facecompare.py
```

`targetFrameId` 필드가 존재하면 exact mode가 활성화된다.

```text
DynamoDB GetItem(
  Key={frame_id: targetFrameId},
  ConsistentRead=True
)
```

exact mode에서는 `query_latest_frame()`을 호출하지 않는다. 항목이 없으면
`TARGET_FRAME_NOT_READY`를 반환한다. 필드가 생략된 경우에만 legacy 운영자
호환을 위해 latest-frame GSI query를 사용한다.

## 4. 최초 제어 캡처

실제 code-server `/proxy/8080` 인증 경로에서 저장소의 비생체 PNG를 사용해
제어 캡처 한 건을 수행했다.

| 항목 | 값 |
|---|---|
| HTTP | 200 |
| captureId | `bd664ad6-c5c4-4b8d-80c4-a22965c51643` |
| shardId | `shardId-000000000000` |
| sequenceNumber | `49677250565632519654093988836363660523843281975567712258` |

## 5. 발견한 실제 런타임 결함과 최소 수정

최초 상태에서 이벤트 소스 매핑은 `Enabled`였지만 마지막 처리 결과가
`PROBLEM: Function call failed`였고 제어 captureId의 DynamoDB 항목이 생성되지
않았다.

CloudWatch `FilterLogEvents`는 현재 역할에 읽기 권한이 없어 사용하지 못했고,
지시대로 IAM은 변경하지 않았다. 대신 이미지 레코드를 포함하지 않는
`Records=[]` 진단 호출을 실행했다.

수정 전 결과:

```text
FunctionError=Unhandled
Runtime.ImportModuleError
Unable to import module 'imageprocessor': No module named 'pytz'
```

원인:

- Lambda runtime은 Python 3.12이다.
- imageprocessor가 모듈 import 시 외부 `pytz`를 요구했다.
- 배포 ZIP에는 `pytz` 패키지가 없었다.

최소 수정:

- `pytz` import 제거
- Python 3.12 표준 라이브러리 `zoneinfo.ZoneInfo` 사용
- `convert_ts()`의 UTC → configured timezone 변환만 동일 의미로 교체

새 imageprocessor ZIP만 배포했으며 다음은 변경하지 않았다.

- IAM
- CloudFormation
- EC2
- Lambda 환경변수
- 이벤트 소스 매핑
- facecompare/framefetcher 코드
- 세션 인증
- Phase 2A ID 계약

수정 후 동일 빈 이벤트 진단:

```text
StatusCode=200
FunctionError=null
```

이후 이벤트 소스 매핑은 기존 제어 Kinesis 레코드를 재처리하고
`LastProcessingResult=OK`가 되었다.

## 6. Kinesis 및 imageprocessor 상관관계

### CODE-CONFIRMED

- FastAPI가 응답 `captureId`와 Kinesis package `CaptureId`에 동일 변수를 쓴다.
- package는 pickle로 한 번 직렬화되어 `FrameStream`으로 전송된다.
- imageprocessor는 `CaptureId`를 검증한 뒤 변경 없이 `frame_id`로 사용한다.

### RUNTIME-CONFIRMED

- 제어 캡처는 Kinesis PutRecord에서 shard/sequence number를 받았다.
- imageprocessor 수정 후 매핑 결과가 `OK`가 되었다.
- 제어 응답 captureId와 정확히 같은 DynamoDB PK가 생성되었다.
- 동일 ID를 포함하는 S3 객체가 생성되었다.

Kinesis raw record는 이미지 bytes를 포함하므로 직접 dump하지 않았다.
하류의 고유 UUID 일치로 안전하게 propagation을 확인했다.

## 7. 최초 제어 캡처 DynamoDB/S3 결과

DynamoDB exact strongly-consistent read:

| 필드 | 값 |
|---|---|
| `frame_id` | `bd664ad6-c5c4-4b8d-80c4-a22965c51643` |
| `s3_key` | `frames/2026/08/10/20/bd664ad6-c5c4-4b8d-80c4-a22965c51643.jpg` |
| `labels_disabled` | false |

S3 HEAD 결과:

| 항목 | 값 |
|---|---|
| object exists | true |
| content length | 6631 bytes |
| last modified | `2026-08-10T11:46:23Z` |

S3 객체는 다운로드하지 않았다.

## 8. `TARGET_FRAME_NOT_READY` 및 fallback 방지

유효하지만 존재하지 않는 UUID를 deployed `/face-compare`에 전달했다.

```text
targetFrameId=00000000-0000-4000-8000-000000000001
```

결과:

```text
HTTP 200
reason=TARGET_FRAME_NOT_READY
response targetFrameId=요청 UUID
resolved_frame_id=null
success=false
```

이 시점에 DynamoDB에는 이미 다른 최신 프레임이 존재했다. 그럼에도 최신
프레임을 반환하거나 비교하지 않았으므로 exact mode의 전역 latest fallback
부재가 런타임에서 확인되었다.

`TARGET_FRAME_NOT_READY`는 코드와 응답 계약에서 다음 결과와 별도 reason으로
구분된다.

- `BAD_REQUEST`
- `NO_LATEST_FRAME`
- `NO_RECENT_FRAME`
- `NO_FACE_IN_SOURCE_OR_TARGET`
- `SIMILARITY_BELOW_THRESHOLD`
- `TARGET_FRAME_TOO_OLD`

최초 제어 ID는 처리 결함 복구 후 지연 때문에 5분 horizon을 초과했지만,
`targetFrameId`와 `resolved_frame_id`가 동일한 상태에서
`TARGET_FRAME_TOO_OLD`로 정확히 구분되었다.

## 9. 키오스크 retry 동작

현재 `kiosk.js` 값:

| 항목 | 값 |
|---|---|
| poll interval | 1500 ms |
| total wait timeout | 15000 ms |
| max completed verification attempts | 3 |
| similarity threshold | 90 |

`TARGET_FRAME_NOT_READY`일 때:

- 같은 함수 인자의 immutable `targetFrameId`를 다시 사용한다.
- 1500ms 뒤 다시 요청한다.
- 최대 15초 동안 기다린다.
- 자동 재촬영하지 않는다.
- `verificationAttempts`를 증가시키지 않는다.

attempt가 증가하는 completed outcome은 다음 세 가지뿐이다.

- `SIMILARITY_ABOVE_THRESHOLD`
- `SIMILARITY_BELOW_THRESHOLD`
- `NO_FACE_IN_SOURCE_OR_TARGET`

기존 JavaScript 상관관계 테스트는 두 번의 `TARGET_FRAME_NOT_READY` 뒤에도 같은
target ID를 세 번째 요청까지 유지함을 확인한다.

## 10. A/B 동시성 테스트

실제 인증된 `/proxy/8080/api/capture-frame`을 연속 두 번 호출했다.

### Capture A

| 항목 | 값 |
|---|---|
| HTTP | 200 |
| captureId | `4cc340cb-6f69-4d06-b4f3-bfab3aa42488` |
| shardId | `shardId-000000000000` |
| sequenceNumber | `49677250565632519654093988978659065195763621636815716354` |
| DynamoDB exists | yes |
| S3 exists | yes |

### Capture B

| 항목 | 값 |
|---|---|
| HTTP | 200 |
| captureId | `5bf1cdd2-abc1-4681-ad50-4eb5ee19fe52` |
| shardId | `shardId-000000000000` |
| sequenceNumber | `49677250565632519654093988978867000436737337854865178626` |
| DynamoDB exists | yes |
| S3 exists | yes |

A와 B는 서로 다른 UUID이며 B의 Kinesis sequence number가 A보다 뒤였다.
DynamoDB에서도 B의 processed timestamp가 A보다 최신이었다.

| 구분 | A | B |
|---|---|---|
| DynamoDB `frame_id` | A와 일치 | B와 일치 |
| processed timestamp | `1786362489.4207618...` | `1786362490.2205603...` |
| S3 key suffix | `/A.jpg`에 해당하는 실제 A UUID | `/B.jpg`에 해당하는 실제 B UUID |
| S3 object | 존재 | 존재 |

### B가 최신인 상태에서 A 요청

```text
requested targetFrameId=A
response targetFrameId=A
resolved frame_id=A
A_exact=true
```

### B 요청

```text
requested targetFrameId=B
response targetFrameId=B
resolved frame_id=B
B_exact=true
```

비생체 로고를 사용했으므로 Rekognition 결과는 양쪽 모두
`NO_FACE_IN_SOURCE_OR_TARGET`였다. 이것은 예상된 비교 결과이며, 응답의 target
metadata가 요청한 프레임을 정확히 resolve했으므로 상관관계 검증에는 영향을
주지 않는다.

## 11. 실제 kiosk.js → deployed API 검증

현재 `web-ui/src/kiosk.js`를 Node VM에 직접 로드하고 실제
`PipelineService.waitForExactComparison()`을 통해 capture A를 deployed API로
전송했다.

```text
requested_captureId=A
kiosk_sent_targetFrameId=A
response_targetFrameId=A
resolved_frame_id=A
all_ids_match=true
```

API key는 일시적인 프로세스 환경에서만 사용했고 출력하거나 파일에 기록하지
않았다.

## 12. 최종 불변식

최초 제어 캡처:

```text
FastAPI captureId
bd664ad6-c5c4-4b8d-80c4-a22965c51643
== DynamoDB frame_id
bd664ad6-c5c4-4b8d-80c4-a22965c51643
```

A/B 및 kiosk integration:

```text
FastAPI captureId A
== DynamoDB frame_id A
== kiosk targetFrameId A
== facecompare resolved frame_id A
```

```text
FastAPI captureId B
== DynamoDB frame_id B
== facecompare targetFrameId B
== facecompare resolved frame_id B
```

## 13. 테스트

```text
pytest -q \
  tests/test_app_kiosk.py \
  tests/test_imageprocessor_correlation.py \
  tests/test_facecompare_correlation.py \
  tests/test_kiosk_javascript_correlation.py

30 passed
```

추가 검사:

```text
python -m py_compile lambda/imageprocessor/imageprocessor.py
PASS

node --check web-ui/src/kiosk.js
PASS

git diff --check
PASS
```

전체 `pytest tests/`는 앞의 75개 테스트가 통과한 뒤 기존
`tests/test_session_auth.py` 진입 시 진행이 멈췄다. 이전 작업에서도 동일하게
재현된 테스트 하네스 정지 현상이며 실패 출력은 없었다. 제한된 대기 후
중단했고 Phase 2A 관련 테스트는 별도 실행으로 모두 통과했다.

## 14. 보안 및 비회귀 확인

- IAM 변경 없음
- CloudFormation 변경 없음
- EC2 재생성/교체 없음
- 세션 인증 변경 없음
- API key 출력/기록 없음
- AWS credentials 출력/기록 없음
- session cookie 출력/기록 없음
- imageBase64/ImageBytes/raw biometric data 출력/기록 없음
- 실제 개인 생체 이미지 사용 없음
- `/api/config` IAM 정책 변경 없음
- `/api/config`, `/api/me`, `/api/detect-labels`, `/api/capture-frame` 계약 변경 없음
- Phase 1.2 SQLite 로직 변경 없음
- legacy latest-frame facecompare 호환 유지
- similarity threshold 90 유지

## 15. 변경 파일

이번 Phase 2A 검증에서 변경한 애플리케이션 파일:

- `lambda/imageprocessor/imageprocessor.py`
  - 누락된 외부 `pytz` 의존성을 Python 3.12 표준 `zoneinfo`로 교체

보고서:

- `2026_08_10_phase2a_e2e_result.md`
- `2026_08_10_grok_report_01.md`

배포용 임시 빌드 산출물:

- `build/imageprocessor-phase2a.zip` (git ignore 대상)

커밋은 생성하지 않았다.

## 16. Phase 2A 분류 근거

| 필수 조건 | 결과 |
|---|---|
| 1. captureId generated | YES |
| 2. Kinesis CaptureId propagation | YES, code + downstream runtime evidence |
| 3. imageprocessor processed capture | YES |
| 4. DynamoDB frame_id == captureId | YES |
| 5. S3 output correlated | YES |
| 6. kiosk targetFrameId == captureId | YES |
| 7. facecompare exact lookup | YES |
| 8. no latest fallback in exact mode | YES |
| 9. A/B concurrency test | PASS |

최종 분류:

```text
VERIFIED END-TO-END
```
