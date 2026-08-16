# ID File Read Debug Report

Local working tree `~/workspace/aws-face-detection`가 source of truth다.
AWS / IAM / CloudFormation / Rekognition / git commit·push는 변경하지 않았다.

## 1. 현재 증상

ID_CAPTURE 화면에서 사진을 고르자마자:

`사진을 읽지 못했습니다. 다른 사진을 선택해 주세요.`

- “얼굴 촬영으로 이동” 비활성화 → `idImage`가 null
- access log: reserve까지 200, **`POST /api/kiosk/store/face-verify` 없음**

## 2. 오류 문구 발생 위치

정확 일치 문자열은 frontend에만 있다.

| 위치 | 함수 | 조건 | backend? |
|---|---|---|---|
| `web-ui/src/kiosk.js` `DemoIdAdapter.read` (구 151, 162행) | FileReader `onload`에서 DataURL에 `,` 없음, 또는 `onerror` | **frontend-only** |
| `friendlyVerificationMessage` 354행 | `선택한 신분증 사진을 읽지 못했습니다...` | backend `INVALID_IMAGE` | face-verify 이후 |

사용자가 본 문구는 **“선택한 신분증” 접두가 없다.**
따라서 backend 메시지 경로가 아니다.

## 3. Frontend-only 여부

**CASE A (증명됨).**

근거:

1. 문구가 FileReader reject 전용이다.
2. `idImage`가 없어서 촬영 버튼이 꺼진다. face-verify는 `idImage`가 있어야만 호출된다.
3. 해당 시각 Uvicorn에 face-verify가 없다.

CASE B(backend decode)면 화면이 FACE_RETRY이고 문구가 `선택한 신분증 사진을...`이며 access log에 face-verify가 있어야 한다.

## 4. File Input

```html
<input type="file" accept="image/jpeg,image/png,.jpg,.jpeg,.png"
       v-on:change="handleIdFile">
```

`accept`는 picker hint다. 최종 검증은 backend magic이 유지된다.

| Stage | Failure | Current UI |
|---|---|---|
| no File (picker cancel) | `NO_FILE` | 무시. 이전 state 유지. generation 증가 안 함 |
| size 0 | `ZERO_BYTE_FILE` | 읽지 못함 안내 |
| unsupported extension | `UNSUPPORTED_FILE` | JPG/PNG만 선택 안내 |
| unsupported MIME + 확장자 없음 | `UNSUPPORTED_FILE` | 위와 동일 |
| file.type empty + jpg/png 확장자 | 허용 | FileReader 진행 |
| FileReader.onerror | `FILE_READ_FAILED` | 읽지 못함. 최대 3회 재시도 |
| FileReader.onabort | `FILE_READ_ABORTED` | 읽기 중단 안내 |
| invalid DataURL | `INVALID_DATA_URL` | 읽지 못함 |
| stale generation | silent ignore | 오류 문구 없음 |
| preview | DataURL을 `img src`로만 사용 | read 실패를 만들지 않음 |

## 5. File Metadata

`file.type === ""`는 실패가 아니다. 확장자로 `image/jpeg` / `image/png`를 추론한다.
비표준 MIME(`image/heic` 등)이어도 확장자가 jpg/png이면 frontend는 읽고, backend가 magic으로 거른다.

0바이트는 FileReader 전에 막는다.

## 6. FileReader

이전 코드는 `onerror` / 잘못된 DataURL을 한 문구로 뭉갰고 `onabort`가 없었다.

지금은:

- `onload` / `onerror` / `onabort` 분리
- DataURL: `data:` prefix, comma, non-empty payload
- `NotReadableError` 등에서 **최대 3회** 재시도 (abort는 재시도 안 함)

콘솔 (이미지/이름 전체/base64 없음):

```text
[kiosk:id-file] { event, stage, extension, file_type, file_size, error_name, base64_length }
```

별도 backend diagnostic endpoint는 만들지 않았다.

## 7. idReadGeneration

증가 위치: `handleIdFile`(파일이 있을 때만), `retryIdCapture`, `performLocalReset`.

ID_CAPTURE에서 다른 UI 이벤트가 generation을 올리지 않는다.
stale resolve/reject는 **조용히 ignore**한다. “사진을 읽지 못했습니다”를 띄우지 않는다.

## 8. Same-file Reselect

이전: FileReader **종료 후** `input.value = ''`.

지금은:

1. `const file = input.files[0]`로 File을 먼저 잡음
2. `DemoIdAdapter.read(file)`로 FileReader를 **그 File 참조**에 시작
3. 그 다음 `input.value = ''`

읽기 전에 reset하지 않는다. FileReader가 잡은 File은 input reset 이후에도 유지된다.

cancel(빈 FileList)은 generation을 올리지 않아 진행 중인 읽기를 stale로 만들지 않는다.

## 9. DataURL Validation

`parseIdDataUrl`: string, comma, non-empty payload.
preview는 같은 DataURL이다. 별도 `Image()`/`createObjectURL` 없음.

## 10. Preview

Vue `img :src="idImage.previewUrl"`. preview 실패는 `idError`를 만들지 않는다.

## 11. Android / File Provider

추가 브라우저 실측 필요.

코드상 취약점은 Gallery/Photos가 주는 content File을 FileReader가
간헐적으로 `NotReadableError`로 거절하는 경우다. 이것이 관측된
문구와 로그(API 없음, 선택 즉시 실패)와 맞다.

완화: File 참조 고정, reset 타이밍, 3회 재시도, 오류 코드 분리.

## 12. Browser Cache

`/src/kiosk.js`는 FastAPI StaticFiles 기본 헤더다. Android가 옛 JS를
쓸 가능성은 있으나, **옛 JS에도 같은 FileReader 문구가 있었다.**
cache만으로 이 증상을 설명하지 않는다. no-cache는 넣지 않았다.

`KIOSK_JS_BUILD=id-file-read-1`로 로드된 스크립트를 구분할 수 있다.

`--reload`는 face-verify 전 FileReader 실패의 직접 원인이 아니다.

## 13. Root Cause

**Primary: A. FILEREADER_NOT_READABLE**

선택 직후 FileReader `onerror`(대개 `NotReadableError`).
backend 미호출. 간헐성 = Android content provider / Gallery File.

**Secondary:**

- FileReader 오류를 generic 문구로 뭉갬 (수정)
- `onabort` 없음 (수정)
- 0바이트를 generic read fail로 보낼 수 있음 (수정)
- cancel change가 generation을 올려 정상 read를 stale 처리할 수 있음 (수정)
- L. ANDROID_CONTENT_PROVIDER / M. CLOUD_BACKED_FILE — 실측 필요

해당 없음: O. BACKEND_ID_DECODE, D. generation이 이 문구를 띄움.

## 14. Secondary Issues

- 큰 파일은 이미 “5MiB 이하” 문구. generic read fail이 아님.
- `accept`에 `.jpg,.jpeg,.png`를 추가 (hint only).

## 15. Fix

1. File을 먼저 캡처한 뒤 FileReader 시작, 그 다음 input reset
2. 빈 선택은 no-op
3. FileReader 3회 재시도
4. `FILE_READ_FAILED` / `ABORTED` / `INVALID_DATA_URL` / `ZERO_BYTE` / `IMAGE_TOO_LARGE` / `UNSUPPORTED` 분리
5. stale generation silent ignore 유지
6. 안전한 console diagnostic
7. `file.type` 빈 값은 확장자 fallback

하지 않음: HEIC 변환, OpenCV/Rekognition threshold, backend validation 제거.

## 16. Tests

JS (`tests/kiosk_correlation_test.js`):

1. JPEG 선택 PASS
2. PNG 선택 PASS
3. `file.type="" + jpg` PASS
4. `file.type="" + png` PASS
5. capture 후 input reset, File 참조 유지
6. A 읽기 중 B → B만 적용 (generation)
7. stale generation → 오류 문구 없음
8. FileReader.onerror → `FILE_READ_FAILED` (재시도 후)
9. onabort → `FILE_READ_ABORTED`
10. size=0 → `ZERO_BYTE_FILE`
11. >5MiB → `IMAGE_TOO_LARGE`
12. invalid DataURL → `INVALID_DATA_URL`
13. preview는 read 결과 DataURL (별도 실패 경로 없음)
14. reset 후 captured File 유지
15. 이 실패 경로는 face-verify를 부르지 않음 (idImage 미생성)

## 17. Real Browser Verification

Unit/mock으로 FileReader 재시도와 generation은 확인했다.
Android Gallery / Google Photos / cloud 이미지는 **실측이 필요하다.**

권장 10~20회 (콘솔 `[kiosk:id-file]`):

```text
A. 로컬 JPEG
B. 로컬 PNG
C. Gallery
D. Google Photos/cloud
E. Screenshot PNG
F. 다운로드 JPEG
```

각 시도: selected / read / state. 이미지 원본은 저장하지 않는다.
`error_name=NotReadableError`가 재시도 후 PASS면 이번 수정이 맞은 것이다.
3회 모두 FAIL이면 provider가 File을 읽히지 않는 것이다. 그 경우 로컬 저장 후 다시 고른다.
