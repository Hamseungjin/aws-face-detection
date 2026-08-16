# OpenCV YuNet Deployment

## 1. 목적

Application FastAPI가 AWS Rekognition 전에 공식 OpenCV YuNet
(`cv2.FaceDetectorYN`)으로 얼굴 품질을 검사하도록 한다. 모델이 없거나
로드에 실패하면 production은 fail-closed (`QUALITY_DETECTOR_UNAVAILABLE`)
이고 Rekognition을 호출하지 않는다.

## 2. 모델

| 항목 | 값 |
|---|---|
| 파일 | `face_detection_yunet_2023mar.onnx` |
| 크기 | 232589 bytes |
| API | `cv2.FaceDetectorYN.create` |

## 3. 공식 Source

OpenCV Zoo (opencv/opencv_zoo):

https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet

Raw:

https://github.com/opencv/opencv_zoo/raw/refs/heads/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx

재확보:

```bash
python3 scripts/fetch_yunet_model.py
```

checksum mismatch면 fetch가 실패한다. 개인 mirror/CDN은 쓰지 않는다.

## 4. 파일 위치

Repository:

```text
web-ui/backend/face_quality.py
web-ui/backend/models/face_detection_yunet_2023mar.onnx
web-ui/backend/models/face_detection_yunet_2023mar.onnx.sha256
```

`face_quality.default_yunet_search_paths()`는 `__file__` 기준
`backend/models/`를 본다. systemd CWD와 무관하다.

`FACE_DETECTOR_MODEL_PATH`는 **기본 artifact path가 있으면 불필요**.

## 5. SHA-256

로컬에서 `sha256sum` / `hashlib`로 계산한 값:

```text
8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4
```

OpenCV 조직 Hugging Face 모델 카드
(`opencv/face_detection_yunet`, `face_detection_yunet_2023mar.onnx`)에
공개된 SHA256과 일치한다.

## 6. Packaging

`build.py` `publishapps`의 webui tar는 `_require_yunet_model()` 후
`_exclude_dev_files`로 `web-ui/`를 묶는다. S3 upload는 이번 작업에서
실행하지 않았다.

로컬 검증:

```text
build/web-ui.tgz
  ./backend/models/face_detection_yunet_2023mar.onnx
```

EC2에서는 `web-ui.tgz`가 `/opt/webui`에 풀리므로
`/opt/webui/backend/models/face_detection_yunet_2023mar.onnx`가 된다.

## 7. Runtime Path

탐색 순서:

1. `FACE_DETECTOR_MODEL_PATH` (설정된 경우)
2. `<face_quality.py dir>/models/face_detection_yunet_2023mar.onnx`
3. 같은 디렉터리의 `face_detection_yunet.onnx` / `yunet.onnx`

프로세스 CWD에 의존하지 않는다.

## 8. FaceDetectorYN Loading

`build_detector()`가 YuNet 파일을 찾으면 `FaceDetectorYN.create`로
한 번 로드한다. 성공 시 `name=yunet`, `trusted=True`.

이 환경: OpenCV 5.0.0, `FaceDetectorYN` 있음, 빈 프레임 `detect` crash 없음.

## 9. Production Fail-Closed

`FACE_QUALITY_ALLOW_UNTRUSTED_DETECTOR` 기본 `false`.

YuNet 없음 또는 load 실패 → `UnavailableDetector` →
`QUALITY_DETECTOR_UNAVAILABLE` → AWS Rekognition 0.

skin/contour fallback을 production에서 자동 사용하지 않는다.

## 10. Dev Fallback

`FACE_QUALITY_ALLOW_UNTRUSTED_DETECTOR=true` 일 때만 contour/skin fallback.

## 11. Tests

```bash
python3 -m pytest -q tests/test_face_quality.py tests/test_face_collection.py \
  tests/test_app_kiosk.py tests/test_kiosk_store.py \
  tests/test_kiosk_javascript_correlation.py
```

117 passed. 포함: 모델 SHA-256, FaceDetectorYN load, trusted YuNet 선택,
production unavailable, dev fallback, QUALITY_DETECTOR_UNAVAILABLE 시
Rekognition mock 0.

## 12. Deployment Artifact Verification

`build/web-ui.tgz`에 ONNX 존재. S3에는 올리지 않음.

## 13. 최종 상태

**OpenCV YuNet Deployment: READY** (로컬 artifact 기준)
