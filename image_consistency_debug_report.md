# Image Consistency Debug Report

Local working tree `~/workspace/aws-face-detection`가 source of truth다.
AWS / IAM / CloudFormation / Collection / S3 / git commit·push는 변경하지 않았다.
실제 Rekognition을 반복 호출하지 않았다.

## 1. 증상

STORE 본인확인에서 어떤 신분증은 되고, 어떤 신분증은
`UNSUPPORTED_IMAGE_FORMAT` / `INVALID_IMAGE`로 실패한다.
더 중요하게는 **전에 됐던 이미지가 나중에 실패**하는 것처럼 보인다.

질문은 “지원 포맷이 무엇인가”가 아니라,
**같은 입력이 요청마다 다른 결과를 내는가**이다.

## 2. 성공/실패 분류

코드상 STORE `POST /api/kiosk/store/face-verify` 실패는 단계가 다르다.

| 단계 | source | 대표 reason | 인증 횟수 | Rekognition |
|---|---|---|---|---|
| Hold | `hold` | `LOCKER_HOLD_*` | 증가 안 함 | 0 |
| ID decode | `id_image` | `UNSUPPORTED_IMAGE_FORMAT`, `INVALID_IMAGE`, `IMAGE_TOO_LARGE` | 증가 안 함 | 0 |
| FRAME-A decode | `frame_a` | 위와 동일 | 증가 안 함 | 0 |
| OpenCV Quality Gate | `quality_gate` | `NO_FACE`, `FACE_TOO_SMALL`, `TOO_BLURRY`, … | 증가 안 함 | 0 |
| CompareFaces | `rekognition` | `SIMILARITY_*`, `NO_FACE_IN_SOURCE_OR_TARGET` | 증가함 | 1 |

“이미지 실패”로 보이는 UI는 위 다섯 단계 중 어느 것이든 될 수 있다.
이제 요청마다 `request_id=store-face-verify-<8hex>`로 한 줄씩 묶인다.

## 3. ID File Metadata

`reference_face.decode_face_image()`는 순수 함수다.

- 입력: base64 문자열 + optional declared MIME + max_bytes
- 상태/난수/시각 없음
- JPEG magic `\xff\xd8`, PNG magic `\x89PNG\r\n\x1a\n`만 허용
- 허용 포맷끼리 declared MIME ≠ magic 이면 **magic을 신뢰** (이미 적용됨)

로그용 `describe_image_payload()`는 이제 다음만 남긴다.

- declared_content_type, detected_magic, decoded_bytes
- image_width, image_height (JPEG SOF / PNG IHDR)
- filename_extension

image bytes, base64, SHA-256은 서버 로그에 넣지 않는다.

HEIC/WEBP/AVIF/JPEG2000은 magic 이름만 진단하고 항상
`UNSUPPORTED_IMAGE_FORMAT`이다.

사용자 제공 신분증 원본은 저장소에 없었다. 로컬에서 읽은 파일은
문서/아이콘 PNG뿐이며 STORE ID 샘플이 아니다.

| File | Extension | Declared MIME | Magic | Bytes | Width | Height | Decode |
|---|---|---|---|---:|---:|---:|---|
| synthetic JPEG | jpg | image/jpeg | JPEG | (generated) | 240 | 320 | PASS ×20 |
| synthetic PNG | png | image/png | PNG | (generated) | 240 | 320 | PASS ×20 |
| PNG bytes + declared jpeg | jpg | image/jpeg | PNG | (generated) | 240 | 320 | PASS (magic 신뢰) |
| JPEG bytes + declared png | png | image/png | JPEG | (generated) | 240 | 320 | PASS (magic 신뢰) |
| HEIC-like `ftypheic` + .jpg | jpg | image/jpeg | HEIC | 56 | — | — | FAIL ×5 |
| random bytes + declared jpeg | jpg | image/jpeg | UNKNOWN | 16 | — | — | FAIL |
| JPEG + EXIF APP1 | jpg | image/jpeg | JPEG | (generated) | 240 | 320 | PASS |

## 4. Frontend File State

경로:

```text
#id-file change
→ handleIdFile
→ DemoIdAdapter.read(File)
   file.name / file.type / FileReader.readAsDataURL
→ idImage = { filename, contentType, base64, previewUrl }
→ captureFace
→ POST face-verify
   imageBase64=idImage.base64
   contentType=idImage.contentType
   filename=idImage.filename
   faceImageBase64=capturedFaceBase64
```

조사 결과:

| 시나리오 | 코드 사실 |
|---|---|
| A. ID A 선택 → 촬영 실패 → 얼굴 재촬영 | `idImage`는 유지된다. 의도된 동작. |
| B. ID A → 취소/처음으로 → STORE 재진입 | `performLocalReset()`이 `idImage=null`. payload는 새 선택이다. |
| C. 같은 파일 다시 선택 | **버그였다.** 성공 시 `input.value`를 비우지 않아 브라우저가 `change`를 안 낸다. |
| D. Hold timeout | `handleHoldExpired`는 locker로 돌아가고 `idImage`는 남긴다. Hold reason은 ID 포맷이 아니다. |
| File A 읽기 중 File B 선택 | **레이스였다.** 늦은 A resolve가 B를 덮을 수 있다. |
| ID decode 실패 후 FACE_RETRY | 문구는 “신분증을 다시 선택”인데 버튼은 “얼굴 다시 촬영”만 있었다. 같은 ID가 다시 나간다. |

같은 파일을 다시 골랐다고 생각해도, 입력이 무시되면 이전 `idImage` 또는 빈 state가 남는다.

## 5. ID Decode Reproducibility

Q1 답: **같은 신분증 bytes를 반복하면 decode 결과는 항상 같다.**

증거 (`tests/test_image_consistency.py`):

- 동일 JPEG 20회 → 20 PASS, 동일 detected type, 동일 raw bytes
- 동일 PNG 20회 → 20 PASS
- unsupported 20회 → 20 FAIL, reason 항상 `UNSUPPORTED_IMAGE_FORMAT`

비결정적 decode 버그(D)는 없다.

## 6. FRAME-A Decode Reproducibility

FRAME-A는 `canvas.toDataURL('image/jpeg', 0.82)`다.
같은 픽셀이면 JPEG magic `FFD8`이고 decode는 결정적이다.

그러나 **촬영마다 픽셀이 다르다.** 카메라는 같은 사람이라도
밝기/흔들림/거리/위치가 바뀐다. “같은 신분증”으로 보이는 실패의
상당수는 다른 FRAME-A다.

동일 FRAME-A bytes를 `evaluate_frame`에 20회 넣으면 quality
`(ok, reason)`은 1종류만 나왔다.

## 7. OpenCV Quality Gate

현재 기본값(이 프로세스, `.env`/`/etc/webui.env` override 없음):

| 변수 | 실제값 | 결정 소스 |
|---|---:|---|
| `KIOSK_MAX_ID_IMAGE_BYTES` | 5242880 | `config.py` default |
| `FACE_DETECTION_SCORE_THRESHOLD` | 0.75 | `config.py` default |
| `FACE_MIN_AREA_RATIO` | 0.08 | `config.py` default |
| `FACE_CENTER_TOLERANCE` | 0.20 | `config.py` default |
| `FACE_MIN_BRIGHTNESS` | 55 | `config.py` default |
| `FACE_MAX_BRIGHTNESS` | 205 | `config.py` default |
| `FACE_MIN_SHARPNESS` | 30 | `config.py` default |

dotenv는 `override=False`라 shell export가 `.env`보다 이긴다.
현재 셸에 FACE threshold export는 없다.
`FACE_QUALITY_ALLOW_UNTRUSTED_DETECTOR=false`만 프로세스 env에 있다.

동일 ID + 다른 FRAME-A:

| FRAME-A | ID decode | Quality | source |
|---|---|---|---|
| 정상 textured | PASS | PASS | (다음 단계) |
| 어두움 | PASS | `TOO_DARK` | `quality_gate` |
| blur | PASS | `TOO_BLURRY` | `quality_gate` |
| 작은 얼굴 | PASS | `FACE_TOO_SMALL` | `quality_gate` |
| 중앙 이탈 | PASS | `FACE_OFF_CENTER` | `quality_gate` |

ID decode는 모두 동일 PASS다. 실패 reason은 quality_gate에서만 바뀐다.

## 8. Runtime Environment

```text
which python3 = /home/ubuntu/workspace/aws-face-detection/.venv/bin/python3
python --version = 3.11.15
backend venv python = 3.11.15
opencv-python-headless = 5.0.0.93
cv2.__version__ = 5.0.0
FaceDetectorYN = True
YuNet path = web-ui/backend/models/face_detection_yunet_2023mar.onnx
YuNet SHA-256 = 8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4
build_detector().name = yunet
build_detector().trusted = True
```

`:8080`은 `--reload` 부모 + worker 한 쌍이다. 서로 다른 앱이 섞인
상태는 아니다. 이전 세션에서는 worker venv에 `cv2`가 없어 Quality Gate가
`INVALID_IMAGE`/`QUALITY_DETECTOR_UNAVAILABLE`로 죽은 적이 있다.
**지금 이 프로세스는 YuNet trusted로 일관된다.**

YuNet에 동일 검정 프레임 20회 → `(ok, reason)` 1종류.

## 9. Hold / Session

Hold가 없거나 만료되면 decode 전에 `source=hold`로 끝난다.
ID 포맷 메시지가 아니다.

Hold timeout 후 `idImage`는 남을 수 있다. 새 locker를 잡고 다시
촬영하면 **같은 ID + 새 FRAME-A**가 나간다. 이것이 “같은 사진이
나중에 실패”로 보이기 쉬운 경로다.

## 10. Rekognition Call Boundary

```text
ID decode FAIL        → quality NOT_REACHED, Rekognition NOT_CALLED
FRAME-A decode FAIL   → quality NOT_REACHED, Rekognition NOT_CALLED
quality FAIL          → Rekognition NOT_CALLED
quality PASS          → CompareFaces 1회, 동일 face_bytes
```

실패한 케이스에서 Rekognition에 도달하는 경우는
quality를 통과한 뒤 CompareFaces가 거절한 경우뿐이다.

## 11. Same Image Repeat Test

```text
face_verify_total     20
id_decode_pass        20
id_decode_fail         0
unsupported_fail      20
unsupported_pass       0
same JPEG/PNG quality 20/20 identical
YuNet same pixels     20/20 identical
```

## 12. Same ID / Different FRAME-A Test

동일 ID bytes는 항상 decode PASS.
FRAME-A 조건만 바꾸면 reason만 quality_gate에서 바뀐다.

Frontend가 이를 ID 오류로 보이면 버그다.
현재 매핑은 `source=quality_gate` + `TOO_BLURRY`를 신분증 문구로
쓰지 않는다. 과거에는 `source`를 무시해 그렇게 보였다.

남아 있던 UX 버그: ID decode 실패 화면에서 신분증을 다시 고를 수 없고
얼굴만 재촬영했다. 같은 불량 ID가 반복되어 “같은 사진이 또 실패”처럼 보였다.

## 13. Root Cause

Q1. 동일 ID bytes 반복 decode는 항상 같다. **YES, deterministic.**
Q2. “같은 사진”이 실제로는 다른 파일/확장자/HEIC/재저장본일 수 있다. **가능.**
Q3. 과거에는 quality/`INVALID_IMAGE`를 신분증 문구로 보여줬다. 메시지 분리는 되어 있다. ID 실패 후 재촬영 UX는 남아 있었다.
Q4. **YES.** 같은 ID + 다른 FRAME-A가 결과를 바꾼다. 가장 흔한 설명이다.
Q5. 동일 ID + 동일 FRAME-A를 backend에서 반복하면 결과는 같다.
Q6. Hold는 AWS 전에 실패할 수 있으나 reason이 다르다.
Q7. 지금은 YuNet/cv2가 일관된다. 어제/오늘 차이는 예전에 cv2 없는 worker가 있던 점이 후보였다.

**Primary Root Cause: E. FRAME_A_QUALITY**

촬영마다 FRAME-A가 다르다. ID는 그대로인데 Quality Gate / CompareFaces
결과가 바뀐다. 예전에 이 실패가 신분증 문구로 보여 “ID가 들쭉날쭉하다”고
오인됐다.

**Secondary Root Cause: C. FRONTEND_STALE_STATE + I. UI_MESSAGE_BUG (잔여 UX)**

- 같은 파일 재선택 시 `change` 미발생
- FileReader 레이스로 A/B payload 섞임
- ID 실패 후 신분증 재선택 경로 없음
- (과거) MIME≠magic JPEG/PNG를 `UNSUPPORTED_IMAGE_FORMAT`으로 거절 — 이미 수정됨
- (과거) 모든 `INVALID_IMAGE`를 신분증 오류로 표시 — 이미 수정됨

해당하지 않음:

- D. ID_DECODE_BUG — 동일 입력은 동일 결과
- H. 실패 케이스에서 Rekognition 도달 — decode/quality 실패는 0

## 14. Secondary Issues

- HEIC를 `.jpg`로 고르면 프론트가 jpeg로 보낼 수 있고, 서버는 항상 FAIL. 이건 비결정이 아니라 포맷 거부.
- EXIF orientation은 ID decode를 실패시키지 않는다. 얼굴 방향 문제는 별 이슈.
- 지원 포맷을 늘리거나 HEIC 변환, threshold 완화는 하지 않았다.

## 15. Fix

원인 확정 후 최소 변경만 했다.

1. `request_id=store-face-verify-<id>`로 hold / id_decode / frame_a_decode / quality_gate / rekognition 단계 로그.
2. 로그에 magic, decoded_bytes, width/height, filename_extension만. 원본/hash 없음.
3. 파일 input을 읽은 뒤 `value=''`로 비워 같은 파일 재선택 가능.
4. `idReadGeneration`으로 늦은 FileReader 결과가 새 선택을 덮지 않음.
5. ID 실패 시 `FACE_RETRY` → `ID_CAPTURE` (“신분증 다시 선택”).
6. cancel/reset 시 generation을 올려 이전 읽기를 버린다.

하지 않은 것: HEIC 변환, JPEG 재인코딩, OpenCV/Rekognition threshold 변경.

## 16. Tests

추가:

1. 동일 JPEG 20회 decode PASS
2. 동일 PNG 20회 decode PASS
3. unsupported 20회 FAIL
4. declared jpeg + actual png PASS
5. 동일 프레임 quality 결과 동일
6. 동일 ID + bad FRAME-A → ID decode PASS, source=quality_gate
7. retry 후 ID 유지 (얼굴 재촬영 경로)
8. cancel/reset 후 idImage 제거 + generation bump
9. ID A 읽기 중 B 선택 → payload B
10. 같은 파일 재선택 가능 (`input.value` reset)
11. quality 메시지가 신분증 문구가 아님
12. quality FAIL → Rekognition 0
13. ID decode FAIL → OpenCV 0 / Rekognition 0
14. face-verify stage 로그가 한 `request_id`를 공유
15. EXIF JPEG는 decode PASS
16. YuNet 동일 픽셀 20회 동일 reason

전체 로컬 스위트: **206 passed / 0 failed**

## 17. 최종 결론

같은 신분증 **bytes**는 요청마다 다르게 decode되지 않는다.

“됐던 사진이 나중에 안 된다”는 대부분:

1. 같은 ID + **다른 카메라 FRAME-A** (품질/유사도)
2. 같은 사진으로 보이는 **다른 파일**(HEIC, 재저장, 확장자만 jpg)
3. 프론트가 이전 실패를 신분증 문제로 보여 주거나, 신분증을 다시 고르지 못하고 같은 ID를 재전송

이제 로그에서 한 `request_id`로 어느 단계가 실패했는지 바로 볼 수 있다.
