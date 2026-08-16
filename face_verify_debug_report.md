# STORE Face Verify Debug Report

Local working tree of `~/workspace/aws-face-detection` is the source of truth.
No AWS resources, IAM, CloudFormation, git commit, or git push were modified.

## 1. 증상

STORE 흐름: Locker 선택 → 신분증 이미지 선택 → 얼굴 촬영 → `POST /api/kiosk/store/face-verify`.

- Uvicorn access log: `POST /api/kiosk/store/face-verify HTTP/1.1 200 OK`
- UI (`FACE_RETRY`):
  - 제목: `본인 확인을 완료하지 못했습니다`
  - 본문: `선택한 신분증 사진을 사용할 수 없습니다. 다른 사진을 선택해 주세요.`
  - 하단: `인증 시도 0/3`
- `webui.face_quality` INFO 로그(`quality_gate_total` / `quality_gate_pass` / `quality_gate_fail_*`)는 터미널에 보이지 않음.

SQLite (`web-ui/backend/data/kiosk.sqlite3`)에는 2026-08-15에 locker 04/12 예약이 여러 번 생겼다가 모두 `CANCELLED`로 끝났다. 그날 STORE가 `STORED`까지 간 트랜잭션은 없다.

## 2. 실제 HTTP Response

Access log는 status만 남기고 body는 저장하지 않는다. 이미 끝난 요청의 raw JSON을 서버에서 회수할 수는 없었다.

그러나 UI 세 줄은 `kiosk.html` + `kiosk.js`에서 **결정적으로** 하나의 response shape에만 대응한다.

| UI | 코드 조건 |
|---|---|
| 제목 `본인 확인을 완료하지 못했습니다` | `lastFailureKind !== 'quality'` |
| 본문 `선택한 신분증 사진을 사용할 수 없습니다...` | `friendlyVerificationMessage('UNSUPPORTED_IMAGE_FORMAT')` 정확 일치 (`INVALID_IMAGE`는 "읽지 못했습니다") |
| `인증 시도 0/3` | `lastFailureKind !== 'quality'` 이고 `isCompletedVerificationOutcome(reason)`가 false라 `verificationAttempts` 미증가 |

해당 조건을 만드는 서버 응답은 다음뿐이다.

```json
{
  "success": false,
  "matched": false,
  "similarity": null,
  "threshold": 90,
  "reason": "UNSUPPORTED_IMAGE_FORMAT",
  "source": "input"
}
```

HTTP status는 200이다. `_face_input_error()`가 dict를 그대로 반환하기 때문이다. FastAPI는 이를 200으로 보낸다. Frontend는 `matched`/`success`를 보고 실패 처리한다.

`source`가 `quality_gate`였다면 제목은 `촬영을 다시 해 주세요`이고 하단은 `이번 촬영은 인증 실패로 세지 않습니다...`가 된다. 관측된 UI와 불일치하므로 **이 실패 요청은 Quality Gate에 도달하지 않았다.**

수정 전 `kiosk_store_face_verify`는 ID decode와 FRAME-A decode를 하나의 `try/except`로 묶고 `source="input"`만 넣었다. 그래서 body만 보면 어느 이미지가 실패했는지 구분할 수 없었다.

## 3. Frontend Request

`web-ui/src/kiosk.js`

신분증 (`DemoIdAdapter.read`):

- `FileReader.readAsDataURL`
- 허용: `image/jpeg`, `image/png` (확장자 jpg/jpeg/png 필수)
- 최대 5MiB
- payload: `{ imageBase64, filename, contentType, faceImageBase64, transactionId }`
- `contentType` = `file.type || inferredType`

카메라 FRAME-A (`captureFace`):

- `getUserMedia({ video: { facingMode: 'user', width: { ideal: 1080 }, height: { ideal: 1440 } } })`
- canvas scale: `min(1, 960/videoWidth, 1280/videoHeight)`
- `canvas.toDataURL('image/jpeg', 0.82)`
- `capturedFaceBase64 = dataUrl.split(',')[1]` (prefix 제거)
- `contentType`은 FRAME-A에 실리지 않음

`PipelineService.compareStoreFace`는 HTTP 200이든 4xx든 `response.data.reason`이 있으면 그 객체를 성공 결과처럼 반환한다. 그래서 200 + `matched=false`도 인증 실패 화면으로 간다.

## 4. ID Image Decode

경로: `kiosk_store_face_verify` → `reference_face.decode_face_image(body.imageBase64, content_type=body.contentType)`.

실패 reason:

| 조건 | reason |
|---|---|
| 빈 문자열 / 비문자 | `INVALID_IMAGE` |
| declared MIME가 jpeg/png가 아님 | `UNSUPPORTED_IMAGE_FORMAT` |
| base64 decode 실패 | `INVALID_IMAGE` |
| decoded empty | `INVALID_IMAGE` |
| decoded > `KIOSK_MAX_ID_IMAGE_BYTES` | `IMAGE_TOO_LARGE` |
| magic이 JPEG/PNG가 아님 | `UNSUPPORTED_IMAGE_FORMAT` |
| **declared MIME ≠ magic** (수정 전) | `UNSUPPORTED_IMAGE_FORMAT` |

ID에만 `contentType`이 전달된다. 따라서 **MIME/magic mismatch는 ID 전용 실패**다.

프론트는 확장자가 `.jpg`이면 `contentType=image/jpeg`를 보낸다. 실제 파일이 PNG(스크린샷을 jpg로 저장한 경우 등)이면 magic은 PNG → 수정 전 `UNSUPPORTED_IMAGE_FORMAT`. HEIC/WEBP/JPEG2000은 magic `UNKNOWN` → 같은 reason.

수정: 허용된 포맷(JPEG/PNG)끼리는 magic을 신뢰하고 mismatch를 통과시킨다. 로그:

`event=image_content_type_mismatch declared=image/jpeg detected=image/png used=image/png`

HEIC-like bytes (`ftypheic`)는 여전히 `UNSUPPORTED_IMAGE_FORMAT`이다.

## 5. FRAME-A Decode

경로: `decode_face_image(body.faceImageBase64)` — `contentType` 없음.

canvas JPEG는 prefix `/9j/` = magic `FFD8`. 로컬 재현:

```
content_type=None
decoded_bytes=3386
magic=JPEG
decode=PASS
```

FRAME-A에서 `UNSUPPORTED_IMAGE_FORMAT`이 나오려면 decoded bytes의 magic이 JPEG/PNG가 아니어야 한다. `toDataURL('image/jpeg', 0.82)` 정상 경로에서는 발생하지 않는다. 빈/깨진 base64는 `INVALID_IMAGE`다.

따라서 관측된 `UNSUPPORTED_IMAGE_FORMAT` + ID 문구는 FRAME-A보다 **ID decode 실패**와 맞다.

## 6. YuNet Runtime

실패 당시 실행 중 프로세스 (`pid 78245`, venv `web-ui/backend/.venv`):

- `import cv2` → `ModuleNotFoundError`
- `/proc/<pid>/maps`에 opencv 없음
- `requirements.txt`에는 `opencv-python-headless>=4.8`이 있으나 **venv에 미설치**
- 모델 파일은 이미 존재했고 SHA256도 일치

```
web-ui/backend/models/face_detection_yunet_2023mar.onnx
SHA256 8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4  MATCH
```

`face_quality.resolve_yunet_model_path()`는 이 파일을 찾는다. 그러나 cv2가 없어 `build_detector()`는 `UnavailableDetector`를 반환하고, `evaluate()`는 `cv2.imdecode` 전에 import 실패로 `INVALID_IMAGE`를 냈다.

수정 후 (같은 venv에 `opencv-python-headless==5.0.0.93` + numpy 설치):

```
cv2.__version__ = 5.0.0
hasattr(cv2, "FaceDetectorYN") = True
cv2.FaceDetectorYN.create(...) = success
build_detector() name=yunet trusted=True
model_path = .../web-ui/backend/models/face_detection_yunet_2023mar.onnx
```

YuNet 미로드는 **이번 실패 요청의 직접 원인이 아니다** (Quality Gate 미도달). 다음 단계 잠복 장애였다.

## 7. OpenCV Quality Gate

`kiosk_store_face_verify` 순서:

1. hold 검사
2. ID decode
3. FRAME-A decode
4. hold 재검사
5. `face_quality.evaluate(face_bytes)`
6. hold 재검사
7. `logger.info("event=rekognition_call_after_quality_gate operation=store_compare")`
8. `compare_id_to_face_bytes`

단계별 실패:

| 단계 | exception / 조건 | reason | source (수정 전 → 후) | HTTP |
|---|---|---|---|---|
| A. body parse | Pydantic | FastAPI 422 | - | 422 |
| hold | 세션/RESERVED 아님 | `LOCKER_HOLD_*` | `hold` | 200 |
| B. ID decode | `ReferenceFaceError` | `INVALID_IMAGE` / `UNSUPPORTED_IMAGE_FORMAT` / `IMAGE_TOO_LARGE` | `input` → `id_image` | 200 |
| C. FRAME-A decode | `ReferenceFaceError` | 동일 | `input` → `frame_a` | 200 |
| D. OpenCV evaluate | `ok=False` | `NO_FACE`, `MULTIPLE_FACES`, `FACE_TOO_SMALL`, `FACE_OFF_CENTER`, `TOO_DARK`, `TOO_BRIGHT`, `TOO_BLURRY`, `INVALID_IMAGE`, `QUALITY_CHECK_FAILED`, `QUALITY_DETECTOR_UNAVAILABLE` | `quality_gate` | 200 |
| E. hold re-check | 만료 | `LOCKER_HOLD_*` | `hold` | 200 |
| F. Rekognition | `ReferenceFaceError` | `NO_FACE_IN_SOURCE_OR_TARGET`, `INVALID_IMAGE`, `ACCESS_DENIED`, `THROTTLED`, `AWS_API_ERROR`, … | `rekognition` | 200 |
| G. compare result | similarity | `SIMILARITY_ABOVE_THRESHOLD` / `SIMILARITY_BELOW_THRESHOLD` | `rekognition` | 200 |

이번 실패 요청은 B에서 종료. D/F/G 미실행.

`INVALID_IMAGE` 발생 위치:

1. 신분증 decode — `reference_face.decode_face_image` (빈/깨진 base64)
2. FRAME-A decode — 동일 함수, contentType 없음
3. OpenCV frame decode — `face_quality.evaluate` (`imdecode` 실패, tiny frame)
4. Collection helper — `face_collection.index_verified_face` / `search_faces_by_image` (STORE complete / RETRIEVE, face-verify 아님)
5. Rekognition helper — `compare_id_to_face_bytes` 빈 bytes, 또는 `InvalidImageFormatException`

수정 전 frontend는 1–5의 `INVALID_IMAGE`/`UNSUPPORTED_IMAGE_FORMAT`을 모두 신분증 문구로 매핑했다.

## 8. Quality Metrics

이번 실패 요청은 Quality Gate에 들어가지 않았다. 메트릭 없음.

로컬에서 동일 decode 경로의 canvas-like 빈 JPEG(얼굴 없음, AWS 호출 없음)로 gate만 돌린 결과 (수정 후, YuNet 로드됨):

```
event=quality_gate_debug
reason=NO_FACE
detector=yunet
trusted=True
face_count=0
face_ratio=None
brightness=None
sharpness=None
offset_x=None
offset_y=None
elapsed_ms=24.32
image_width=360
image_height=480
```

임계값(`FACE_MIN_*` 등)은 변경하지 않았다. 실제 실패가 gate 이전이므로 낮출 근거가 없다.

## 9. Rekognition 호출 여부

결론 **A. Quality Gate 이전 실패 → CompareFaces 호출 0**.

근거:

- `event=rekognition_call_after_quality_gate operation=store_compare`는 quality PASS 후에만 찍힌다.
- 해당 로그가 실패 요청에서 관측되지 않았다.
- UI reason이 `UNSUPPORTED_IMAGE_FORMAT`이고, 이 reason은 decode에서만 나오며 Rekognition mapper는 같은 AWS 코드를 `INVALID_IMAGE`로 바꾼다.
- 코드상 decode except는 evaluate / compare 전에 return한다.

## 10. Frontend Message Mapping

수정 전 `friendlyVerificationMessage(reason)`는 `source`를 무시했다.

```
UNSUPPORTED_IMAGE_FORMAT / INVALID_IMAGE / IMAGE_TOO_LARGE
  → 항상 신분증 메시지
```

그래서 가정이 아니라 실제 버그다:

- `{ reason: "INVALID_IMAGE", source: "quality_gate" }` → "선택한 신분증 사진을 읽지 못했습니다"
- `{ reason: "UNSUPPORTED_IMAGE_FORMAT", source: "input" }` → "선택한 신분증 사진을 사용할 수 없습니다"  ← **이번 증상**

`isQualityGateFailure` 목록에 `INVALID_IMAGE`가 없었다. `source === 'quality_gate'`이면 kind는 quality로 처리되지만, 문구는 여전히 신분증이었다.

수정 후:

| source | reason | 메시지 |
|---|---|---|
| `id_image` / `input` | `INVALID_IMAGE` | 선택한 신분증 사진을 읽지 못했습니다. |
| `id_image` / `input` | `UNSUPPORTED_IMAGE_FORMAT` | 선택한 신분증 사진을 사용할 수 없습니다. |
| `quality_gate` / `frame_a` / `frame_b` | `INVALID_IMAGE` / `UNSUPPORTED_IMAGE_FORMAT` | 카메라 촬영 이미지를 처리하지 못했습니다. 다시 촬영해주세요. |
| `quality_gate` | `NO_FACE` | 얼굴을 카메라 화면 안에 맞춰주세요. |
| `quality_gate` | `FACE_TOO_SMALL` | 카메라에 조금 더 가까이 와주세요. |
| `quality_gate` | `TOO_DARK` / `TOO_BRIGHT` / `TOO_BLURRY` | 각각 조명/흔들림 안내 |
| `quality_gate` | `QUALITY_DETECTOR_UNAVAILABLE` | 얼굴 촬영 시스템을 준비하지 못했습니다. |

품질/입력 오류는 인증 시도 횟수에 넣지 않는다.

## 11. Primary Root Cause

**ID_IMAGE_DECODE_FAILURE**

`reason=UNSUPPORTED_IMAGE_FORMAT`, 수정 전 `source=input`.

가장 유력한 세부 원인: ID `contentType`(대개 `image/jpeg`)과 실제 magic 불일치, 또는 JPEG/PNG가 아닌 파일(HEIC 등)을 `.jpg`로 선택한 경우. 둘 다 같은 reason을 냈다.

FRAME-A canvas JPEG는 magic 검사만 하고 정상 JPEG를 만든다.

## 12. Secondary Issues

1. **FRONTEND_MESSAGE_MAPPING_BUG** — `source` 무시, 모든 `INVALID_IMAGE`/`UNSUPPORTED_IMAGE_FORMAT`을 신분증 오류로 표시.
2. **decode source 미분리** — ID와 FRAME-A가 같은 `source=input`.
3. **OpenCV 미설치 (`YUNET_NOT_LOADED` 잠복)** — 모델 파일은 맞지만 `cv2` 없음. decode가 통과했다면 gate가 `INVALID_IMAGE`로 신분증 문구를 냈을 것.
4. **missing cv2 → `INVALID_IMAGE`** — `QUALITY_DETECTOR_UNAVAILABLE`이어야 한다.
5. **Quality 로그 미출력**
   - 이번 요청: gate 미도달이 주원인.
   - 구조: `webui.face_quality` level=NOTSET. root가 WARNING이면 INFO가 `isEnabledFor`에서 버려진다. uvicorn default config는 root를 건드리지 않는다. 사용자가 `logging.basicConfig(INFO)`를 쓰면 root=INFO라 gate에 들어가면 보여야 한다.

## 13. 수정 내용

로컬 코드만, 최소 변경.

1. ID / FRAME-A decode를 분리하고 `source`를 `id_image` / `frame_a`로 설정. RETRIEVE FRAME-B decode는 `frame_b`.
2. 허용 포맷끼리 MIME/magic mismatch는 magic을 신뢰.
3. `describe_image_payload` + inspect 로그 (`content_type`, `decoded_bytes`, `magic`만. base64/이미지 없음).
4. `webui` logger를 INFO로 고정. handler가 없을 때만 stderr handler 추가.
5. `face_quality`: cv2 없으면 `QUALITY_DETECTOR_UNAVAILABLE`. `event=quality_gate_debug`에 metrics 기록.
6. Frontend: `source`+`reason` 메시지 분리. quality/`id`는 시도 횟수 미증가.
7. venv에 `opencv-python-headless` 설치. YuNet `name=yunet trusted=True`.
8. FastAPI를 같은 `0.0.0.0:8080`으로 재시작해 코드+cv2 반영.

임계값 변경 없음. AWS/IAM/CloudFormation 변경 없음.

## 14. Test Results

```
python3 -m pytest -q \
  tests/test_face_quality.py \
  tests/test_face_collection.py \
  tests/test_app_kiosk.py \
  tests/test_kiosk_store.py \
  tests/test_kiosk_javascript_correlation.py
```

**124 passed / 0 failed**

추가 regression:

- ID decode 실패 → `source=id_image`, quality/Rekognition 0
- FRAME-A decode 실패 → `source=frame_a`, quality/Rekognition 0
- quality `INVALID_IMAGE` → Rekognition 0
- PNG bytes + declared `image/jpeg` → decode PASS
- JS: ID `INVALID_IMAGE` ≠ quality_gate `INVALID_IMAGE` 메시지
- quality 실패 reason은 `isCompletedVerificationOutcome`이 아님 (시도 횟수 증가 안 함)
- YuNet 없으면 `QUALITY_DETECTOR_UNAVAILABLE`
- quality 로그에 image/base64 없음

## 15. 최종 상태

- Primary root cause 확정: ID image decode `UNSUPPORTED_IMAGE_FORMAT`.
- Frontend가 카메라/quality `INVALID_IMAGE`를 신분증 오류로 보이던 매핑 버그 수정.
- Quality 로그가 터미널에 보이도록 logger 수정.
- 로컬 venv YuNet 로드됨 (`yunet` / `trusted=True`).
- 서버 재시작됨 (`0.0.0.0:8080`).
- 실제 얼굴 이미지로 Rekognition CompareFaces는 호출하지 않음.
- 다음 브라우저 재시도 시 inspect 로그에서 ID/FRAME-A magic을 확인할 수 있다. 파일이 진짜 HEIC/WEBP이면 여전히 ID 메시지로 거절하는 것이 맞다.
