# OpenCV Face Quality Gate

키오스크 STORE/RETRIEVE 카메라 프레임을 AWS Rekognition CompareFaces에 보내기
전에, FastAPI 호스트에서 OpenCV로 품질을 검사한다. 이 문서는 **현재 local
working tree 구현**을 기준으로 한다.

현재 기본 threshold는 Strict Kiosk Profile이다. 변경 이유·적용값·정면성 검사·
calibration 방법은 `opencv_strict_quality_gate.md`를 본다.

## 1. 목적

- 얼굴이 없거나, 여러 명이거나, 너무 작거나, 화면에서 벗어나거나, 너무 어둡거나,
  너무 밝거나, 흐린 프레임을 **로컬에서 차단**한다.
- 불필요한 Rekognition 호출과 얼굴 이미지의 AWS 전송을 줄인다.
- 저품질 촬영으로 인한 인증 실패를 줄이고, 사용자에게 즉시 재촬영 안내를 준다.
- 품질 실패는 **본인인증 실패 횟수에 포함하지 않는다.**

## 2. 기존 인증 흐름

현재 키오스크는 Kinesis / ImageProcessor / facecompare Lambda를 쓰지 않는다.
FastAPI가 Rekognition CompareFaces를 직접 호출한다.

**STORE (변경 전)**

```text
CONSENT → ID 파일 선택(형식/크기 검증) → Camera FRAME-A
        → POST /api/kiosk/store/face-verify
        → Rekognition CompareFaces (ID bytes ↔ FRAME-A bytes)
        → 유사도 ≥ 90 이면 세션에 face hash 바인딩
        → 보관함 선택 → 예약 → 데모 결제 → store/complete
        → 검증된 동일 FRAME-A를 locker-references/<tx>/reference.jpg 로 저장
```

**RETRIEVE (변경 전)**

```text
8자리 보관번호 → SQLite STORED 조회
        → Camera FRAME-B
        → POST /api/kiosk/retrieve/start
        → S3 HeadObject(reference.jpg) + CompareFaces (S3 ↔ FRAME-B)
        → 유사도 ≥ 90 이면 STORED → RETRIEVING → 보관함 개방
```

운영자 UI의 `/api/capture-frame` → Kinesis 경로는 그대로 두었다. 키오스크
본인인증과 분리되어 있다.

## 3. 변경 후 인증 흐름

**STORE**

```text
Locker 선택 → 서버 Hold (RESERVED)
        → 신분증 Local Validation
        → Camera FRAME-A JPEG bytes
        → OpenCV Local Quality Gate  (같은 bytes)
              FAIL → 재촬영, Hold 유지, Rekognition = 0
              PASS → Hold 재확인
        → Rekognition CompareFaces (ID ↔ FRAME-A)
        → 결제/최종확정
        → 동일 FRAME-A bytes → IndexFaces → FaceId
        → SQLite STORED (reference.jpg 신규 생성 없음)
```

**RETRIEVE**

```text
8자리 코드 형식 검증
        → SQLite STORED / FaceId 또는 legacy S3 참조 확인
              FAIL → 즉시 종료, AWS = 0
        → Camera FRAME-B JPEG bytes
        → OpenCV Local Quality Gate  (같은 bytes)
              FAIL → 재촬영 안내, Search/Compare = 0, STORED 유지
              PASS → 동일 FRAME-B bytes
        → Collection: SearchFacesByImage + expected FaceId
           또는 Legacy: S3 CompareFaces
        → 기존 후속 처리
```

검사한 프레임과 인증에 쓰는 프레임이 달라지지 않도록, 서버는 decode된
`face_bytes` 한 객체를 Quality Gate와 CompareFaces에 모두 넘긴다. 검사 후
다시 촬영하지 않는다.

## 4. Quality Gate 검사 항목

비용이 싼 순서:

1. 이미지 decoding (JPEG/PNG magic은 `reference_face.decode_face_image`,
   픽셀 decode는 `cv2.imdecode`)
2. 최소 가로/세로 (`FACE_MIN_IMAGE_WIDTH` / `FACE_MIN_IMAGE_HEIGHT`)
3. 얼굴 검출 (다운스케일 후 박스를 원본 좌표로 환원)
4. `face_count == 0` → `NO_FACE`
5. `face_count > 1` → `MULTIPLE_FACES`
6. 얼굴 면적 비율 → `FACE_TOO_SMALL`
7. 얼굴 중심 위치 → `FACE_OFF_CENTER`
8. YuNet landmark pose(있을 때만) → `FACE_TILTED` / `FACE_POSE_INVALID`
9. 얼굴 ROI crop
10. ROI 평균 밝기 → `TOO_DARK` / `TOO_BRIGHT`
11. ROI Laplacian 분산 → `TOO_BLURRY`
12. PASS 후에만 AWS Rekognition

구현: `web-ui/backend/face_quality.py` 의 `evaluate(image_bytes)` /
`FaceQualityGate.evaluate(frame_bytes)`.

## 5. 얼굴 검출 방식

현재 키오스크 백엔드에는 기존 face detector가 없었다. capture client
(`client/video_cap.py`)의 OpenCV는 카메라 JPEG 인코딩용이며 재사용하지 않는다.

선택 순서 (프로세스당 1회 생성):

1. **`cv2.FaceDetectorYN` (YuNet)** — `FACE_DETECTOR_MODEL_PATH` 또는
   `web-ui/backend/models/face_detection_yunet_2023mar.onnx` 가 있을 때.
   bounding box, score, 눈/코/입 landmark를 읽는다.
2. **`cv2.CascadeClassifier` Haar** — OpenCV 4.x wheel이
   `haarcascade_frontalface_default.xml` 을 bundling한 경우.
3. **Contour/skin-blob fallback** — 모델 파일이 없을 때. YCrCb 피부색 마스크와
   Otsu contrast contour. YuNet보다 부정확하다.

운영 기본값 `FACE_QUALITY_ALLOW_UNTRUSTED_DETECTOR=false` 이면 contour
fallback은 얼굴을 통과시키지 않고 `QUALITY_DETECTOR_UNAVAILABLE`을 반환한다
(AWS 호출 없음). 로컬 데모에서만 true로 켠다.

이 환경의 OpenCV는 5.0.x 이며 Haar XML과 `CascadeClassifier`가 없다. 따라서
YuNet ONNX가 없으면 fallback detector가 쓰인다.

YuNet 모델은 git에 넣지 않는다. 설치 방법:

```text
공식 OpenCV model zoo:
  https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet
파일 예: face_detection_yunet_2023mar.onnx
저장: web-ui/backend/models/face_detection_yunet_2023mar.onnx
또는 FACE_DETECTOR_MODEL_PATH=/절대경로/face_detection_yunet_2023mar.onnx
```

신뢰할 수 없는 출처에서 바이너리를 받지 말 것. 배포 스크립트가 인터넷에서
자동 다운로드하지 않는다.

## 6. 밝기 계산 방식

전체 프레임이 아니라 **검출된 얼굴 ROI**만 사용한다.

```text
FRAME → Face bbox → clamp crop → BGR→GRAY → cv2.meanStdDev → brightness
```

- `brightness < FACE_MIN_BRIGHTNESS` → `TOO_DARK`
- `brightness > FACE_MAX_BRIGHTNESS` → `TOO_BRIGHT`
- 단위: 8-bit grayscale 평균 (0–255)

## 7. Blur 계산 방식

같은 얼굴 ROI grayscale에 Laplacian variance를 쓴다.

```text
gray = cv2.cvtColor(face_roi, cv2.COLOR_BGR2GRAY)
laplacian = cv2.Laplacian(gray, cv2.CV_64F)
sharpness = laplacian.var()
```

`sharpness < FACE_MIN_SHARPNESS` → `TOO_BLURRY`

## 8. 얼굴 크기/위치 검사

```text
face_ratio = (face_width * face_height) / (frame_width * frame_height)
face_center = (x + width/2, y + height/2)
offset_x = |center_x / frame_width  - 0.5|
offset_y = |center_y / frame_height - 0.5|
```

- `face_ratio < FACE_MIN_AREA_RATIO` → `FACE_TOO_SMALL`
- `offset_x` 또는 `offset_y` > `FACE_CENTER_TOLERANCE` → `FACE_OFF_CENTER`

중앙 검사는 가이드 타원을 벗어난 경우만 거르도록 느슨한 기본값을 쓴다.

## 9. Threshold 설정

모두 `web-ui/backend/config.py` / 환경변수. **실제 키오스크 카메라·조명에서
calibration이 필요하다.** 아래 숫자는 보수적 초기값이며 표준값이 아니다.

| 변수 | 단위 | 기본값 | 높이면 | 낮추면 |
|---|---|---|---|---|
| `FACE_DETECTION_SCORE_THRESHOLD` | detector confidence 0–1 | 0.75 | 더 많은 `NO_FACE` | 오검출 증가 |
| `FACE_MIN_AREA_RATIO` | 얼굴면적/프레임면적 | 0.08 | 더 가까이 와야 함 | 작은 얼굴 허용 |
| `FACE_CENTER_TOLERANCE` | \|center−0.5\| | 0.20 | 중앙에서 더 멀리 허용 | 더 엄격한 중앙 |
| `FACE_MIN_BRIGHTNESS` | ROI 평균 gray 0–255 | 55 | 어두운 얼굴 더 거름 | 어두운 환경 허용 |
| `FACE_MAX_BRIGHTNESS` | ROI 평균 gray 0–255 | 205 | 밝은 얼굴 더 허용 | 과노출 더 거름 |
| `FACE_MIN_SHARPNESS` | Laplacian 분산 | 30 | 더 선명해야 함 | 흔들림 더 허용 |
| `FACE_MAX_EYE_TILT_DEGREES` | 수평 대비 기울기 ° | 20 | 더 기울여도 허용 | 더 정면 |
| `FACE_MAX_YAW_RATIO` | \|nose−eye mid\| / IOD | 0.38 | 더 돌아도 허용 | 더 정면 |
| `FACE_MIN_IMAGE_WIDTH` / `_HEIGHT` | px | 80 | 작은 프레임 거름 | 작은 프레임 허용 |
| `FACE_DETECT_MAX_SIDE` | px | 320 | 검출 해상도↑(느림) | 검출 비용↓ |
| `FACE_DETECTOR_MODEL_PATH` | 파일 경로 | (비움) | — | — |

Calibration 팁: 키오스크 실조명에서 정상/실패 샘플 각 수십 장을 모아
`brightness`, `sharpness`, `face_ratio` 로그를 보고 경계를 조정한다. 로그에는
이미지/base64를 남기지 않는다.

## 10. Error Code

Quality Gate 내부 reason (브라우저 `reason`과 동일):

| reason | 의미 |
|---|---|
| `NO_FACE` | 검출된 얼굴 0 |
| `MULTIPLE_FACES` | 얼굴 2명 이상 |
| `FACE_TOO_SMALL` | 면적 비율 미달 |
| `FACE_OFF_CENTER` | 중앙 이탈 |
| `TOO_DARK` | ROI 평균 밝기 하한 미달 |
| `TOO_BRIGHT` | ROI 평균 밝기 상한 초과 |
| `TOO_BLURRY` | ROI Laplacian 분산 미달 |
| `FACE_TILTED` | YuNet 두 눈 기울기(roll) 과다 |
| `FACE_POSE_INVALID` | YuNet nose 좌우 치우침(yaw) 과다 |
| `INVALID_IMAGE` | decode 실패 또는 프레임이 너무 작음 |
| `QUALITY_CHECK_FAILED` | detector/ROI 예외 |

AWS/Rekognition 결과와 구분:

| reason | 출처 |
|---|---|
| `SIMILARITY_ABOVE_THRESHOLD` / `SIMILARITY_BELOW_THRESHOLD` | Rekognition |
| `NO_FACE_IN_SOURCE_OR_TARGET` / `NO_FACE_IN_TARGET` / `NO_FACE_IN_REFERENCE` | Rekognition |
| `ACCESS_DENIED` / `THROTTLED` / `AWS_API_ERROR` | AWS |

응답에 `source` 필드를 넣는다. `quality_gate` 또는 `rekognition`.

하나의 `BAD_IMAGE`로 뭉개지 않는다.

## 11. Frontend 사용자 메시지

`web-ui/src/kiosk.js` `friendlyVerificationMessage`:

| reason | 안내 |
|---|---|
| `NO_FACE` | 얼굴을 카메라 화면 안에 맞춰주세요. |
| `MULTIPLE_FACES` | 한 분만 카메라 앞에 서주세요. |
| `FACE_TOO_SMALL` | 카메라에 조금 더 가까이 와주세요. |
| `FACE_OFF_CENTER` | 얼굴을 화면 중앙에 맞춰주세요. |
| `TOO_DARK` | 얼굴이 너무 어둡습니다. 밝은 곳에서 다시 촬영해주세요. |
| `TOO_BRIGHT` | 얼굴이 너무 밝게 촬영되었습니다. 위치를 조정해주세요. |
| `TOO_BLURRY` | 사진이 흔들리거나 흐립니다. 잠시 멈춘 상태에서 다시 촬영해주세요. |
| `FACE_TILTED` | 고개를 바로 세우고 정면을 바라봐주세요. |
| `FACE_POSE_INVALID` | 카메라 정면을 바라봐주세요. |

품질 실패는 `FACE_RETRY`에서 **"촬영을 다시 해 주세요"** 로 표시하고
"인증 시도 n / 3" 대신 "이번 촬영은 인증 실패로 세지 않습니다"를 보여 준다.
Rekognition 불일치는 기존 **"본인 확인을 완료하지 못했습니다"** + 시도 횟수.

## 12. STORE 적용 위치

`web-ui/backend/app.py` → `kiosk_store_face_verify`

```text
decode ID + decode FRAME-A
    → face_quality.evaluate(face_bytes)
    → FAIL: _face_input_error(reason, source="quality_gate")  // Rekognition 없음
    → PASS: compare_id_to_face_bytes(_rekognition, id_bytes, face_bytes)
```

프론트: 촬영 버튼 1회 → 같은 `capturedFaceBase64`를
`POST /api/kiosk/store/face-verify`에 보낸다. 매 비디오 프레임마다 DNN을
돌리지 않는다.

## 13. RETRIEVE 적용 위치

`web-ui/backend/app.py` → `kiosk_retrieve_start`

```text
validate_retrieval_code
    → rate-limit / SQLite STORED 조회
    → reference_face_deleted_at 이면 REFERENCE_FACE_MISSING (AWS 없음)
    → decode FRAME-B
    → face_quality.evaluate(face_bytes)
    → FAIL: reason + status=STORED, S3 Head / CompareFaces 없음
    → PASS: reference_object_exists + compare_reference_to_face_bytes(같은 bytes)
```

품질 실패는 `RetrievalAttemptLimiter`를 증가시키지 않는다.

## 14. AWS 호출 절감 원리

```text
저품질 프레임 → OpenCV FAIL → Rekognition 호출 0
정상 프레임   → OpenCV PASS → 동일 bytes → CompareFaces 1회
```

STORE 품질 실패는 Kinesis / S3 / DynamoDB에도 프레임을 올리지 않는다.
RETRIEVE 품질 실패는 참조 S3 HeadObject도 하지 않는다.

운영자 파이프라인(`/api/capture-frame`)은 그대로다.

로그 이벤트(이미지 없음):

- `quality_gate_total`
- `quality_gate_pass`
- `quality_gate_fail_no_face` / `_multiple_faces` / `_too_small` /
  `_off_center` / `_dark` / `_bright` / `_blur` / `_invalid_image` /
  `_check_failed`
- `rekognition_call_after_quality_gate`

나중에 `차단율 = fail / total` 을 계산할 수 있다.

## 15. 개인정보 최소화 효과

품질 실패 프레임은 AWS로 전송되지 않는다. 로그에는 reason과
brightness/sharpness/face_count/face_ratio/elapsed_ms 만 남긴다. 이미지
bytes, base64, 얼굴 crop, 신분증 원본은 기록하지 않는다.

## 16. 테스트 방법

실제 Rekognition을 호출하지 않는다. 얼굴 원본 fixture를 저장소에 추가하지
않는다. mock detector + synthetic ndarray / stub Rekognition을 사용한다.

```bash
python3 -m pytest -q tests/test_face_quality.py tests/test_app_kiosk.py tests/test_kiosk_javascript_correlation.py
```

검증 항목:

1. 정상 얼굴 → PASS
2. 얼굴 없음 → `NO_FACE`
3. 얼굴 2명 → `MULTIPLE_FACES`
4. 너무 작음 → `FACE_TOO_SMALL`
5. 중앙 이탈 → `FACE_OFF_CENTER`
6. 어두움 → `TOO_DARK`
7. 밝음 → `TOO_BRIGHT`
8. Blur → `TOO_BLURRY`
9. Quality FAIL → Rekognition mock 호출 0
10. Quality PASS → Rekognition 호출 가능
11. 품질 실패는 보관번호 rate-limit / 인증 시도 횟수를 올리지 않음
12. Quality Gate와 CompareFaces가 동일 FRAME bytes를 사용

## 17. Calibration 방법

1. 키오스크 실카메라로 정상/실패 촬영을 수집하되, 원본을 git에 넣지 않는다.
2. 서버 로그의 `brightness`, `sharpness`, `face_ratio`, `offset_*` 를 모은다.
3. 정상 샘플의 하한보다 조금 낮게 `FACE_MIN_*` 를, 상한보다 조금 높게
   `FACE_MAX_BRIGHTNESS` 를 둔다.
4. YuNet ONNX를 설치한 뒤 `FACE_DETECTION_SCORE_THRESHOLD`를 0.5–0.8에서
   조정한다. fallback contour는 조명에 민감하므로 운영 키오스크는 YuNet을
   권장한다.
5. 변경 후 `tests/test_face_quality.py`를 다시 돌리고, 키오스크에서 재촬영
   안내가 과도하지 않은지 확인한다.

## 18. 현재 구현과 향후 Collection/Liveness 구조

### A. 현재 실제 구현

| 기능 | 상태 |
|---|---|
| Rekognition `CompareFaces` | 구현됨 (FastAPI 직접 호출) |
| STORE ID ↔ FRAME-A | 구현됨 |
| RETRIEVE S3 `reference.jpg` ↔ FRAME-B | 구현됨 |
| OpenCV Local Quality Gate | **이번 작업에서 구현** |
| S3 `locker-references/<tx>/reference.jpg` | 구현됨 (CompareFaces용 1장) |
| `IndexFaces` | **미구현** |
| Rekognition Collection | **미구현** |
| FaceId 저장 | **미구현** |
| `SearchFacesByImage` | **미구현** |
| Face Liveness | **미구현** |
| Rekognition `DeleteFaces` | **미구현** (찾기 후 S3 객체 삭제만) |

### B. 이번 작업에서 구현

- `web-ui/backend/face_quality.py` 공통 Quality Gate
- STORE/RETRIEVE에서 Rekognition 직전 차단 + 동일 bytes 재사용
- 프론트 재촬영 안내 / 실패 횟수 제외
- config/env threshold
- 로컬 mock 테스트와 `opencv_quality_gate.md`

### C. 아직 미구현 (향후 목표 구조)

```text
STORE
  신분증 → FRAME-A → OpenCV Quality Gate → CompareFaces
        → IndexFaces → FaceId → Transaction

RETRIEVE
  Retrieval Code → SQLite → FRAME-B → OpenCV Quality Gate
        → Face Liveness → SearchFacesByImage
        → FaceId + Similarity → Transaction FaceId 검증
        → Locker OPEN → DeleteFaces
```

이번 작업에서 AWS Collection/Liveness 리소스나 리전을 추가하지 않았다.
Quality Gate는 현재 CompareFaces 경로 앞에만 붙였다. 나중에 Collection을
넣더라도 같은 `evaluate(face_bytes)` 와 동일 프레임 재사용 규칙을 유지하면
된다.
