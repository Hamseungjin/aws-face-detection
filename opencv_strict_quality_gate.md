# OpenCV Strict Quality Gate

키오스크 STORE/RETRIEVE 카메라 프레임을 AWS Rekognition에 보내기 전에,
FastAPI 호스트에서 OpenCV YuNet으로 촬영 품질을 더 엄격히 검사한다.

이 문서는 **현재 local working tree 구현**을 기준으로 한다.
아래 숫자는 **키오스크 실카메라 calibration용 1차 후보**이며
산업 표준값이라고 주장하지 않는다.

## 1. 목적

기존 Quality Gate는 얼굴을 거의 아무 위치·조명에서나 통과시키기 쉬운
느슨한 초기값이었다. 그 결과 멀리 찍힌 얼굴, 한쪽으로 치우친 얼굴,
어둡거나 과노출된 얼굴, 흔들린 얼굴, 낮은 confidence 검출이
Rekognition CompareFaces / SearchFacesByImage까지 갈 수 있었다.

Strict Kiosk Profile의 목적:

1. 멀리 찍힌 얼굴 차단 강화
2. 옆으로 치우친 얼굴 차단 강화
3. 어둡거나 과노출된 얼굴 차단 강화
4. 흐린 얼굴 차단 강화
5. 낮은 confidence 얼굴 검출 차단
6. YuNet 2D landmark로 지나치게 기울거나 돌아간 얼굴 차단
7. 저품질 프레임이 Rekognition까지 가는 비율 감소

Quality Gate는 **촬영 품질**만 본다. 본인 여부와 살아있는지 여부는
각각 Rekognition Compare/Search와 Face Liveness의 역할이다.

## 2. 기존 검사

실제 코드 (`web-ui/backend/face_quality.py`) 기준 순서:

```text
Image decode (JPEG/PNG magic + cv2.imdecode)
→ OpenCV 사용 가능 여부
→ untrusted detector fail-closed
→ minimum dimensions (FACE_MIN_IMAGE_WIDTH / HEIGHT)
→ downscale (FACE_DETECT_MAX_SIDE) 후 YuNet detect
→ 원본 좌표로 박스/landmark 환원
→ detection confidence (앱에서 score >= FACE_DETECTION_SCORE_THRESHOLD)
→ face count == 0 → NO_FACE
→ face count > 1 → MULTIPLE_FACES
→ face size (face_ratio)
→ face position (offset_x / offset_y)
→ YuNet landmark pose (있을 때만)
→ face ROI clamp
→ brightness (face ROI grayscale mean)
→ sharpness (face ROI Laplacian variance)
→ PASS 후에만 AWS Rekognition
```

Detection confidence는 두 곳에서 적용된다.

1. `cv2.FaceDetectorYN.create(..., score_threshold=...)`
2. `evaluate_frame()`이 검출 결과 `face.score`를 다시 비교

2번이 있어야 Haar/contour/fake detector와 테스트 경로에서도
같은 기준이 적용된다.

## 3. 기존 Threshold

코드가 source of truth다. 강화 전 `config.py` / `FaceQualitySettings` 기본값:

| 변수 | 현재값 | 의미 | 높이면 | 낮추면 | 엄격하게 만드는 방향 |
|---|---:|---|---|---|---|
| `FACE_DETECTION_SCORE_THRESHOLD` | 0.6 | YuNet/Haar 최소 confidence | 더 많은 `NO_FACE` | 오검출 증가 | **높임** |
| `FACE_MIN_AREA_RATIO` | 0.05 | 얼굴면적 / 프레임면적 | 더 가까이 와야 함 | 작은 얼굴 허용 | **높임** |
| `FACE_CENTER_TOLERANCE` | 0.28 | \|face_center − 0.5\| | 중앙에서 더 멀리 허용 | 더 중앙이어야 함 | **낮춤** |
| `FACE_MIN_BRIGHTNESS` | 40 | 얼굴 ROI 평균 gray 하한 | 어두운 얼굴 더 거름 | 어두운 환경 허용 | **높임** |
| `FACE_MAX_BRIGHTNESS` | 220 | 얼굴 ROI 평균 gray 상한 | 밝은 얼굴 더 허용 | 과노출 더 거름 | **낮춤** |
| `FACE_MIN_SHARPNESS` | 15 | 얼굴 ROI Laplacian 분산 | 더 선명해야 함 | 흔들림 더 허용 | **높임** |
| `FACE_MIN_IMAGE_WIDTH` | 80 | 최소 가로 px | 작은 프레임 거름 | 작은 프레임 허용 | 이번 작업에서 유지 |
| `FACE_MIN_IMAGE_HEIGHT` | 80 | 최소 세로 px | 작은 프레임 거름 | 작은 프레임 허용 | 이번 작업에서 유지 |
| `FACE_DETECT_MAX_SIDE` | 320 | 검출용 최장변 | 검출 해상도↑(느림) | 검출 비용↓ | 이번 작업에서 유지 |

왜 기존 값이 느슨한가:

- score 0.6은 YuNet이 부분 얼굴·약한 검출도 남긴다.
- 면적 5%는 키오스크 프레임에서 멀리 선 얼굴도 통과한다.
- 중앙 허용 0.28은 가이드 타원을 상당히 벗어나도 통과한다.
- 밝기 40–220은 거의 모든 실내 조명(어두움·날림)을 통과한다.
- Laplacian 15는 약한 텍스처만 있어도 통과한다.

`FACE_CENTER_TOLERANCE`와 `FACE_MAX_BRIGHTNESS`는 값을 **낮출수록**
더 엄격하다.

## 4. Strict Threshold

검토한 초기 후보 구간 (표준값이 아님):

| 변수 | 기존 | 후보 구간 |
|---|---:|---|
| `FACE_DETECTION_SCORE_THRESHOLD` | 0.6 | 0.70–0.80 |
| `FACE_MIN_AREA_RATIO` | 0.05 | 0.07–0.10 |
| `FACE_CENTER_TOLERANCE` | 0.28 | 0.18–0.22 |
| `FACE_MIN_BRIGHTNESS` | 40 | 50–65 |
| `FACE_MAX_BRIGHTNESS` | 220 | 195–210 |
| `FACE_MIN_SHARPNESS` | 15 | 25–40 |

1차 적용값. 정상 사용자를 한꺼번에 탈락시키지 않으면서
기존보다 분명히 엄격한 쪽을 골랐다.

| 검사 | 기존 | 변경 | 더 엄격해지는 이유 |
|---|---:|---:|---|
| Detection score | 0.6 | **0.75** | 약한/부분 검출이 Rekognition으로 가지 않게. YuNet 정상 정면은 보통 이보다 높다. 0.80은 첫 calibration에서 과도할 수 있다. |
| Face area ratio | 0.05 | **0.08** | 멀리 선 얼굴을 `FACE_TOO_SMALL`로 더 자주 차단. 0.10은 키오스크 거리에서 답답할 수 있어 하단에 가깝게 둠. |
| Center tolerance | 0.28 | **0.20** | 가이드 타원 밖으로 치우친 얼굴을 차단. 0.18은 키가 커서 얼굴이 위로 가는 경우를 너무 빨리 자를 수 있다. |
| Min brightness | 40 | **55** | 얼굴 ROI가 어두운 프레임을 차단. 65는 실내 키오스크 조명 실측 없이 위험하다. |
| Max brightness | 220 | **205** | 과노출/날린 얼굴을 차단. 195는 창가 반사에서 정상 사용자 탈락 위험이 있다. |
| Sharpness | 15 | **30** | 흔들림·핀트 불량을 더 자주 `TOO_BLURRY`로 차단. Laplacian 분산은 ROI 크기/조명에 따라 달라 보편 표준이 아니다. |
| Pose | 없음 | **roll 20° / yaw 0.38** | YuNet 5점 landmark가 있을 때만. 심한 기울임·옆모습만 차단. |

이미지 최소 크기와 검출 다운스케일은 품질 엄격화와 별개라 80 / 80 / 320을 유지한다.

모든 값은 환경변수로 override 가능하다.

x/y 중앙 허용은 이번에도 동일 `FACE_CENTER_TOLERANCE`를 쓴다.
세로형 키오스크는 나중에 `FACE_CENTER_X_TOLERANCE` /
`FACE_CENTER_Y_TOLERANCE`로 나눌 수 있으나 이번 작업에서는 하지 않는다.

## 5. Detection Confidence

YuNet row의 score(보통 index 14)를 `DetectedFace.score`로 읽고,
`FACE_DETECTION_SCORE_THRESHOLD`와 **애플리케이션에서 다시 비교**한다.

미달 얼굴은 얼굴 목록에서 제거된다. 남는 얼굴이 없으면 `NO_FACE`.
로그에는 제거 전 최고점을 `detection_score`로 남겨, 실카메라에서
0.70대 탈락을 바로 볼 수 있다.

별도의 `FACE_LOW_CONFIDENCE` reason은 추가하지 않았다.
기존 `NO_FACE` 계약과 프론트 안내("얼굴을 카메라 화면 안에 맞춰주세요")를 유지한다.

## 6. Face Count

변경 없음.

- 0명 → `NO_FACE`
- 2명 이상 → `MULTIPLE_FACES`

둘 다 인증 시도 횟수에 넣지 않고 AWS를 호출하지 않는다.

## 7. Face Size

```text
face_ratio = (face_width * face_height) / (frame_width * frame_height)
face_ratio < FACE_MIN_AREA_RATIO → FACE_TOO_SMALL
```

0.05 → 0.08 이므로 같은 프레임에서 더 작은 얼굴이 `FACE_TOO_SMALL`로 떨어진다.

화면 안내: "카메라에 조금 더 가까이 와주세요."

## 8. Face Center

```text
offset_x = |center_x / frame_width  - 0.5|
offset_y = |center_y / frame_height - 0.5|
offset_x 또는 offset_y > FACE_CENTER_TOLERANCE → FACE_OFF_CENTER
```

0.28 → 0.20. 얼굴이 화면 중앙에 더 가까워야 PASS한다.

화면 안내: "얼굴을 화면 중앙에 맞춰주세요."

## 9. Brightness

전체 프레임이 아니라 **얼굴 ROI**만 사용한다.

```text
BGR → GRAY → cv2.meanStdDev → brightness
brightness < FACE_MIN_BRIGHTNESS → TOO_DARK
brightness > FACE_MAX_BRIGHTNESS → TOO_BRIGHT
```

허용 구간: 40–220 → **55–205**. `TOO_DARK`와 `TOO_BRIGHT`는 구분한다.

## 10. Sharpness

같은 얼굴 ROI grayscale에 Laplacian variance를 쓴다.

```text
gray = cv2.cvtColor(face_roi, cv2.COLOR_BGR2GRAY)
sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()
sharpness < FACE_MIN_SHARPNESS → TOO_BLURRY
```

15 → 30. 이 숫자는 ROI 크기·리사이즈·조명에 영향을 받으므로
보편적인 blur 표준으로 쓰지 않는다.

## 11. YuNet Landmarks / Pose

YuNet 결과는 이미 파싱되고 있었다.

- right eye `(values[4], values[5])`
- left eye `(values[6], values[7])`
- nose `(values[8], values[9])`
- right mouth corner
- left mouth corner

이전에는 landmark를 저장만 하고 쓰지 않았다. 이번 작업에서
**심한 기울임/옆모습만** 거르는 2D heuristic을 넣었다.

구현 (`pose_from_landmarks`):

```text
eye_angle_raw = atan2(right_eye_y - left_eye_y, right_eye_x - left_eye_x)
eye_angle     = tilt_from_horizontal(eye_angle_raw)   # [-90, 90]°
yaw_ratio     = (nose_x - eye_mid_x) / inter_ocular_distance
```

정면 YuNet 얼굴에서 right_eye는 이미지 왼쪽(사람 오른쪽)이라
raw angle이 약 180°가 된다. 수평에서의 편각만 쓰도록 fold 한다.

| 조건 | reason |
|---|---|
| `abs(eye_angle) > FACE_MAX_EYE_TILT_DEGREES` (기본 20) | `FACE_TILTED` |
| `abs(yaw_ratio) > FACE_MAX_YAW_RATIO` (기본 0.38) | `FACE_POSE_INVALID` |

landmark가 없거나(Haar/contour/테스트 fake), 두 눈 거리가 3px 미만이면
**검사를 건너뛴다.** 없는 landmark 때문에 fail-closed하지 않는다.
운영 키오스크는 YuNet만 trusted detector이므로 정상 검출에는 landmark가 있다.

`FACE_POSE_CHECK_ENABLED=false`이면 metric만 남기고 차단하지 않는다.

이 값은 OpenCV 2D landmark 추정이다. 정확한 3D head pose라고 주장하지 않는다.

## 12. Error Reasons

| reason | 의미 | 인증 횟수 | AWS |
|---|---|---|---|
| `NO_FACE` | 검출 0 또는 score 미달만 남음 | 증가 안 함 | 0 |
| `MULTIPLE_FACES` | 얼굴 2명 이상 | 증가 안 함 | 0 |
| `FACE_TOO_SMALL` | 면적 비율 미달 | 증가 안 함 | 0 |
| `FACE_OFF_CENTER` | 중앙 이탈 | 증가 안 함 | 0 |
| `TOO_DARK` | ROI 평균 밝기 하한 미달 | 증가 안 함 | 0 |
| `TOO_BRIGHT` | ROI 평균 밝기 상한 초과 | 증가 안 함 | 0 |
| `TOO_BLURRY` | ROI Laplacian 분산 미달 | 증가 안 함 | 0 |
| `FACE_TILTED` | 고개 기울임(roll) 과다 | 증가 안 함 | 0 |
| `FACE_POSE_INVALID` | 정면이 아닌 옆모습 추정(yaw) | 증가 안 함 | 0 |
| `INVALID_IMAGE` | decode 실패 또는 프레임이 너무 작음 | 증가 안 함 | 0 |
| `QUALITY_CHECK_FAILED` | detector/ROI 예외 | 증가 안 함 | 0 |
| `QUALITY_DETECTOR_UNAVAILABLE` | YuNet 사용 불가 (production fail-closed) | 증가 안 함 | 0 |

인증 실패 count 대상은 그대로 Rekognition 결과
(`SIMILARITY_BELOW_THRESHOLD` 등)만 해당한다.

## 13. Logging Metrics

요청당 한 줄:

```text
event=quality_gate_debug result=PASS reason=PASS detector=yunet trusted=true
detection_score=0.86 face_count=1 face_ratio=0.12 brightness=103.4
sharpness=58.2 offset_x=0.03 offset_y=0.06 eye_angle=1.2 yaw_ratio=0.05
elapsed_ms=21.5 image_width=640 image_height=480
```

실패 예:

```text
event=quality_gate_debug result=FAIL reason=TOO_BLURRY detector=yunet
trusted=true detection_score=0.88 ... sharpness=19.3 ...
```

로그에 남기는 값:

- detector, trusted, detection_score
- face_count, face_ratio, brightness, sharpness
- offset_x, offset_y, eye_angle, yaw_ratio
- elapsed_ms, image_width, image_height

절대 남기지 않는 값:

- image bytes, base64, face crop, ID image, 실제 얼굴 픽셀

## 14. AWS Call Prevention

```text
저품질 프레임 → OpenCV FAIL → Rekognition 호출 0
정상 프레임   → OpenCV PASS → 동일 bytes → CompareFaces/Search 1회
```

STORE 품질 실패는 Kinesis / S3 / DynamoDB에도 프레임을 올리지 않는다.
RETRIEVE 품질 실패는 S3 HeadObject / Search / Compare를 하지 않고
`RetrievalAttemptLimiter`도 올리지 않는다.

## 15. Frontend UX

`web-ui/src/kiosk.js` `friendlyVerificationMessage` /
`isQualityGateFailure`:

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

품질 실패는 `FACE_RETRY`에서 "촬영을 다시 해 주세요" +
"이번 촬영은 인증 실패로 세지 않습니다"를 보여 준다.
`verificationAttempts`는 올리지 않는다.

## 16. Tests

실제 얼굴 fixture를 repository에 넣지 않는다.
mock detector + synthetic ndarray / stub Rekognition을 사용한다.

```bash
python3 -m pytest -q tests/test_face_quality.py tests/test_app_kiosk.py tests/test_kiosk_javascript_correlation.py
```

검증 항목:

1. 정상 고품질 얼굴 → PASS
2. detection score 미달 → `NO_FACE`, metric에 `detection_score` 기록
3. 얼굴 없음 → `NO_FACE`
4. 여러 얼굴 → `MULTIPLE_FACES`
5. 얼굴 작음 → `FACE_TOO_SMALL` (기존 0.05~0.08 사이도 신규 기준에서 탈락)
6. 중앙 이탈 → `FACE_OFF_CENTER` (0.20~0.28 사이도 신규 기준에서 탈락)
7. 어두움 → `TOO_DARK`
8. 밝음 → `TOO_BRIGHT`
9. 흐림 → `TOO_BLURRY`
10. 기울임 → `FACE_TILTED`, 큰 yaw → `FACE_POSE_INVALID`
11. 모든 quality FAIL → Rekognition mock call count 0
12. quality FAIL → `verificationAttempts` 증가 안 함 (`isCompletedVerificationOutcome` false)
13. 정상 frame → Rekognition 호출 가능

## 17. Real Camera Calibration

1. 키오스크 실카메라로 정상/실패 촬영을 30–100회 한다. 원본은 git에 넣지 않는다.
2. 서버 로그에서 `event=quality_gate_debug` 한 줄을 모은다.
3. 정상 샘플의 하한보다 조금 낮게 `FACE_MIN_*`를, 상한보다 조금 높게
   `FACE_MAX_BRIGHTNESS`를 둔다.
4. `detection_score`, `face_ratio`, `offset_*`, `brightness`, `sharpness`,
   `eye_angle`, `yaw_ratio`를 보고 한 변수씩 조정한다.
5. 변경 후 테스트를 다시 돌리고, 재촬영 안내가 과도하지 않은지 확인한다.

환경변수 예 (`web-ui/backend/.env` 또는 `/etc/webui.env`):

```text
FACE_DETECTION_SCORE_THRESHOLD=0.75
FACE_MIN_AREA_RATIO=0.08
FACE_CENTER_TOLERANCE=0.20
FACE_MIN_BRIGHTNESS=55
FACE_MAX_BRIGHTNESS=205
FACE_MIN_SHARPNESS=30
FACE_POSE_CHECK_ENABLED=true
FACE_MAX_EYE_TILT_DEGREES=20
FACE_MAX_YAW_RATIO=0.38
```

## 18. 한계

- Laplacian variance는 해상도·ROI 크기·조명에 민감하다.
- 2D landmark yaw/roll은 대략적인 정면성이지 3D pose가 아니다.
- landmark가 없는 detector 경로에서는 정면성 검사를 건너뛴다.
- 최소 이미지 크기와 검출 다운스케일은 이번 엄격화 대상이 아니다.
- 실카메라 분포를 보기 전에는 값을 더 조이지 않는 것이 안전하다.

## 19. Face Liveness와의 역할 구분

| 단계 | 질문 |
|---|---|
| OpenCV Quality Gate | 얼굴 인증에 적합한 품질의 촬영인가? |
| Rekognition Compare / Search | 등록된 사람과 동일한 사람인가? |
| Face Liveness | 실제 살아있는 사람이 지금 카메라 앞에 있는가? |

Strict threshold만으로 프린트 사진이나 휴대폰 얼굴사진 spoof를
막았다고 표현하지 않는다. spoof 방어는 향후 Rekognition Face Liveness
영역이다. 이번 작업은 저품질 프레임을 AWS에 보내지 않는 것이 목적이다.
