# Session Authentication / HTTP 401 진단·수정 결과

**일자:** 2026-08-10  
**저장소:** `/home/ubuntu/workspace/aws-face-detection`  
**브랜치:** `devhsj`  
**커밋:** 하지 않음 (요청에 따라 uncommitted 상태 유지)

---

## 1. 요약

| 항목 | 결과 |
|---|---|
| 문제 유형 | FastAPI **세션 쿠키 인증** (AWS IAM 아님) |
| 증상 | `POST /api/login` → 200 이후 보호 API → **401 Unauthorized** |
| 근본 원인 | 보호 라우트가 `request.session["user"]`를 요구하는데, 브라우저가 세션 쿠키를 저장·전송하지 않으면 401 |
| 수정 후 | curl cookie-jar로 로그인 → `/api/me` `authenticated=true` → 보호 API **401 아님** |
| 남은 이슈 | `/api/config` 는 세션 통과 후 **AWS AccessDenied (503)** 가능 (별도 IAM 문제) |

---

## 2. 관측된 증상 (수정 전)

```text
POST /api/login           → 200 OK
GET  /api/config          → 401 Unauthorized
POST /api/detect-labels   → 401 Unauthorized
GET  /api/me              → 200 OK  (세션 상태 조회 전용; 항상 200 가능)
```

### 중요

- `/api/me` 가 HTTP 200 인 것만으로 로그인 성공을 의미하지 **않습니다**.
- 쿠키가 없으면 본문은 다음과 같습니다.

```json
{"authenticated": false, "user": null}
```

- 이 401 은 FastAPI `require_user()` 가 AWS 호출 **이전**에 반환합니다.
- `cloudformation:DescribeStackResource` AccessDenied 와 **혼동하면 안 됩니다**.

---

## 3. 인증 구현 (코드 기준)

| 항목 | 내용 |
|---|---|
| 로그인 | `POST /api/login` — `WEBUI_AUTH_USERNAME` + PBKDF2 해시 검증 |
| 세션 기록 | `request.session["user"] = username` (+ `kiosk_rate_scope`) |
| 인증 판정 | `_current_user()` → `request.session.get("user")` |
| 보호 | `require_user()` — 없으면 `401 authentication required` |
| 미들웨어 | Starlette `SessionMiddleware` (서명된 쿠키) |
| 쿠키 이름 | `webui_session` |
| Max-Age | `28800` (8시간) |
| SameSite | `lax` (기본, `WEBUI_SESSION_SAME_SITE` 로 변경 가능) |
| Secure / https_only | `WEBUI_SESSION_HTTPS_ONLY` (로컬 HTTP 기본 **false**) |
| Path | `/` |
| Domain | 미설정 (host-only) |
| HttpOnly | 예 |
| 프론트엔드 | axios `withCredentials: true`, 상대 경로 `/api` (code-server 시 `/proxy/<port>/api`) |

보호 엔드포인트 예:

- `/api/config`
- `/api/detect-labels`
- `/api/capture-frame`
- `/api/kiosk/*`

---

## 4. 로컬 세션 설정 (민감값 제외)

| 설정 | 존재 여부 / 값 | 비고 |
|---|---|---|
| `WEBUI_SESSION_HTTPS_ONLY` | 존재 → **false** | plain HTTP 필수 |
| `WEBUI_SESSION_SECRET` | 존재 (비어 있지 않음) | 값 미공개 |
| `WEBUI_AUTH_USERNAME` | 존재 (`admin`) | |
| `WEBUI_AUTH_PASSWORD_HASH` | 존재 (비어 있지 않음) | 값 미공개 |
| `SESSION_SAME_SITE` | `lax` | |
| `.env` 로드 | `config.py` 가 **절대 경로**로 `web-ui/backend/.env` 로드 | CWD 의존 제거 |

EC2 배포(`webui-bootstrap.sh`)는 `WEBUI_SESSION_HTTPS_ONLY=true` 를 씁니다.  
로컬 `http://localhost` / `http://127.0.0.1` 에서는 **true 를 쓰면 안 됩니다**.

---

## 5. curl cookie-jar 재현 결과

제어용 진단 계정으로 동일 FastAPI 코드 경로를 검증했습니다. (운영 비밀번호·쿠키 값·시크릿은 기록하지 않음)

### 5.1 로컬 HTTP 모드 (`WEBUI_SESSION_HTTPS_ONLY=false`)

```text
POST /api/login
  → 200 OK
  → Set-Cookie: webui_session=<redacted>; path=/; Max-Age=28800; httponly; samesite=lax
  → Secure=false, HttpOnly=true, SameSite=lax, Path=/, Domain=unset
  → cookie_present=true

GET /api/me (쿠키 포함)
  → 200 {"authenticated": true, "user": "<diag-user>"}

GET /api/detect-labels (쿠키 포함)
  → 200 {"enabled": false, "lastUpdateStatus": "Successful"}   # 401 아님

GET /api/config (쿠키 포함)
  → 503 {"detail": "config unavailable: AWS access denied"}   # 401 아님
  → 세션 인증 통과 후 IAM 단계에서 실패

POST /api/capture-frame (쿠키 포함, 최소 JPEG)
  → 200 {"ok": true, "captureId": "...", ...}   # 401 아님

GET /api/config (쿠키 없음)
  → 401 authentication required
```

### 5.2 호스트 불일치 (localhost vs 127.0.0.1)

```text
Login + cookie jar  on http://127.0.0.1:8082
GET  http://localhost:8082/api/me  (동일 jar)
  → {"authenticated": false, "user": null}
```

쿠키는 **호스트 단위**입니다. `localhost` 와 `127.0.0.1` 은 서로 다른 쿠키 범위입니다.

### 5.3 Secure 쿠키 + plain HTTP (실패 모드 A)

`WEBUI_SESSION_HTTPS_ONLY=true` 이면 Set-Cookie 에 `Secure` 가 붙습니다.  
브라우저는 plain `http://localhost` 에서 해당 쿠키를 저장·전송하지 않아:

- login 본문 200
- 보호 API 401

이 패턴이 재현됩니다.

---

## 6. 근본 원인

보호 API 401 의 직접 원인:

> 서명된 세션 쿠키가 **없거나 전송되지 않아** `request.session["user"]` 가 비어 있음.

`POST /api/login` 200 은 **자격 증명 검증 성공**만 의미합니다.  
프론트는 기존에 login JSON 성공만으로 UI 를 인증 상태로 전환했고, 브라우저가 HttpOnly 쿠키를 실제로 유지하는지는 확인하지 않았습니다.

동일 증상을 만드는 구체 원인:

1. **Secure 쿠키 + plain HTTP** (`WEBUI_SESSION_HTTPS_ONLY=true` + `http://localhost`)
2. **호스트 불일치** (login 은 localhost, API 는 127.0.0.1 또는 반대)
3. **설정 미재기동** (`.env` 변경 후 uvicorn 미재시작; 세션 플래그는 프로세스 기동 시 고정)

로컬에서 `WEBUI_SESSION_HTTPS_ONLY=false` 이고 호스트를 하나로 고정하면, curl cookie-jar 로 세션 인증이 정상 동작합니다.

---

## 7. 적용한 수정 (최소·안전)

운영 HTTPS 보안을 전역으로 약화하지 않았습니다.

| 순번 | 내용 |
|---|---|
| 1 | `WEBUI_SESSION_HTTPS_ONLY` 환경 분리 유지 (로컬 false / 배포 HTTPS true) |
| 2 | `web-ui/backend/.env` 를 **config.py 옆 절대 경로**로 로드 (CWD 무관) |
| 3 | 기동 시 비민감 세션 플래그 로그 (`https_only`, `same_site`, cookie name, max_age 등). 시크릿·쿠키 값 미출력 |
| 4 | 프론트: login 200 후 `GET /api/me` 로 `authenticated=true` 확인 후에만 UI 인증 처리 |
| 5 | 세션 쿠키 전용 테스트 추가 (`tests/test_session_auth.py`) |
| 6 | `.env.example` 에 401/쿠키 트러블슈팅 문서화 |

### 변경 파일

| 파일 | 변경 요약 |
|---|---|
| `web-ui/backend/config.py` | 절대 경로 dotenv, `SESSION_SAME_SITE`, HTTPS_ONLY 문서 |
| `web-ui/backend/app.py` | `same_site` 설정 연결, 기동 로그/경고 |
| `web-ui/backend/.env.example` | Secure 쿠키·호스트 불일치 안내 |
| `web-ui/src/app.js` | login 후 `/api/me` 검증 |
| `web-ui/src/kiosk.js` | 동일 |
| `tests/test_session_auth.py` | 신규 세션 인증 테스트 |
| `2026_08_10_grok_report_01.md` | Follow-up 섹션 + Appendix C |
| `2026_08_10_session_auth_401_result.md` | 본 결과 문서 |

---

## 8. 수정 후 검증

### 8.1 런타임 (cookie jar)

| 검사 | 결과 |
|---|---|
| `POST /api/login` | 200 + Set-Cookie (로컬 모드 Secure 없음) |
| `GET /api/me` + cookie | `authenticated=true` |
| `GET /api/detect-labels` + cookie | **200** (401 아님) |
| `GET /api/config` + cookie | **503** AWS access denied (401 아님) |
| `POST /api/capture-frame` + cookie | **200** (파이프라인 정상 시) |

### 8.2 자동화 테스트 / 정적 검사

```text
pytest tests/           → 83 passed
py_compile (변경 Python) → OK
node --check app.js/kiosk.js → OK
git diff --check        → OK
```

### 8.3 브라우저 권장 확인

1. 호스트를 하나로 고정: `http://localhost:8080` **또는** `http://127.0.0.1:8080`
2. 하드 리프레시 후 로그인
3. DevTools:
   - login 응답에 `Set-Cookie`
   - `webui_session` 저장 (HttpOnly)
   - 이후 `/api/*` 요청에 `Cookie` 헤더 포함
4. 기대 로그:

```text
POST /api/login           200
GET  /api/config          != 401   (503 IAM 가능)
GET  /api/detect-labels   != 401
POST /api/capture-frame   != 401
```

---

## 9. 남은 AWS IAM 이슈 (세션과 분리)

세션 인증 통과 후에도 `/api/config` 는 다음을 반환할 수 있습니다.

```text
503 config unavailable: AWS access denied
```

서버 분류 로그 예:

```text
service=config operation=LoadGatewayConfig
error_code=AccessDenied category=access_denied
resource=video-analyzer-stack
```

원인 후보: 인스턴스 역할/호출자의 `cloudformation:DescribeStackResource` 및 API Gateway 키 조회 권한 부족.

**세션 401 과 무관**하며, 본 작업에서 IAM/CloudFormation 재배포는 수행하지 않았습니다.

---

## 10. 운영 체크리스트

- [x] login 성공 시 Set-Cookie 발급 확인
- [x] cookie jar 로 `/api/me` → `authenticated=true`
- [x] 보호 API 가 세션 때문에 401 나지 않음
- [x] 로컬 HTTP 는 `WEBUI_SESSION_HTTPS_ONLY=false`
- [x] 배포 HTTPS 는 Secure 유지 (`true`)
- [x] 시크릿·쿠키 값·비밀번호 미노출
- [x] 전체 pytest 통과
- [ ] 브라우저 DevTools 로 쿠키 전송 최종 확인 (운영자 환경)
- [ ] `/api/config` IAM AccessDenied 별도 조치 (필요 시)

---

## 11. 로컬 실행 참고

```bash
cd web-ui/backend
source .venv/bin/activate   # 있는 경우
# .env: WEBUI_SESSION_HTTPS_ONLY=false 확인
export AWS_DEFAULT_REGION=ap-northeast-2
uvicorn app:app --host 127.0.0.1 --port 8080
```

브라우저 접속 시 **한 가지 호스트만** 사용:

- `http://localhost:8080`  
  **또는**
- `http://127.0.0.1:8080`

`.env` 변경 후에는 반드시 uvicorn 을 재시작하세요.

---

## 12. 관련 문서

| 문서 | 설명 |
|---|---|
| `2026_08_10_grok_report_01.md` | 인프라 복구 이력 + Session Auth Follow-up + Appendix C 원문 프롬프트 |
| `web-ui/backend/.env.example` | 세션 쿠키 플래그·401 트러블슈팅 |
| `docs/LOCAL_RUN.md` | 로컬 실행 가이드 |

---

## 13. 최종 성공 조건

| 조건 | 상태 |
|---|---|
| `POST /api/login` → 200 | 충족 |
| `GET /api/me` → `authenticated=true` (쿠키 포함) | 충족 |
| 보호 엔드포인트가 **세션 누락으로 401** 나지 않음 | 충족 |
| AWS 오류는 세션 통과 후에만 평가 | 충족 (`/api/config` 503 IAM) |
| 커밋 없음 | 충족 |

---

## 14. code-server Cookie 재인코딩 호환 수정 (후속)

**날짜:** 2026-08-10  
**커밋:** 없음  
**진단 문서:** `2026_08_10_proxy_session_cookie_diagnosis.md`

### 14.1 확정 원인 (프록시 경로)

code-server `/proxy/<port>` 가 요청 `Cookie` 헤더를 재조립할 때 `encodeURIComponent` 스타일로 값을 percent-encoding 한다.  
Starlette 서명 세션의 base64 padding `=` 가 `%3D` 로 바뀌면 `SessionMiddleware` 검증이 실패하고 `authenticated=false` 가 된다.

직접 FastAPI 는 정상. Secure/SameSite/Domain/호스트 혼용/AWS 는 이 실패 모드의 원인이 아님.

### 14.2 미들웨어 순서 (Starlette 1.6.0)

`add_middleware()` 는 **마지막에 추가한 것이 outermost** (요청 시 먼저 실행).

| 순서 (요청 방향) | 클래스 |
|---|---|
| 1 outer | `CodeServerSessionCookieCompatMiddleware` |
| 2 inner | `SessionMiddleware` |

### 14.3 구현

| 항목 | 내용 |
|---|---|
| 모듈 | `web-ui/backend/session_cookie_compat.py` |
| 동작 | `config.SESSION_COOKIE` 값만 `urllib.parse.unquote` **1회** |
| 비대상 | `code-server-session` 등 기타 쿠키 불변 |
| 직접 접속 | percent escape 없으면 no-op |
| 보안 | 서명 검증은 계속 SessionMiddleware; 전역 Cookie decode 없음 |
| 로그 | 쿠키 값 미기록; DEBUG 시 `proxy_cookie_normalized=true` 만 허용 |

프론트 메시지 (`app.js` / `kiosk.js`) 를 중립 문구로 교체:

> 로그인은 성공했지만 세션을 확인하지 못했습니다. 브라우저 쿠키 또는 프록시 설정을 확인해 주세요.

### 14.4 검증

**직접 `http://127.0.0.1:8080`**

| API | 결과 |
|---|---|
| `/api/me` (세션 있음) | `authenticated=true` |
| 인코딩된 세션 쿠키 시뮬레이션 | `authenticated=true` (미들웨어 복원) |
| `/api/detect-labels` | 200 |
| `/api/capture-frame` | 200, `captureId` 생성 |

**code-server `/proxy/8080`** (동일 호스트 `https://127.0.0.1/proxy/8080/` — 공개 IP와 동일 code-server 경로)

| API | 수정 전 | 수정 후 |
|---|---|---|
| `/api/me` | `authenticated=false` | **`authenticated=true`** |
| `/api/detect-labels` | 세션 실패 | **200** |
| `/api/capture-frame` | 세션 실패 | **200**, captureId present |
| `/api/config` | (세션과 별개) | **503** AWS access denied |

### 14.5 자동화

| 검사 | 결과 |
|---|---|
| `pytest tests/` | **91 passed** |
| `py_compile` | OK |
| `node --check` app.js / kiosk.js | OK |
| `git diff --check` | OK |
| 커밋 | **없음** |

### 14.6 남은 이슈

`/api/config` → 503 AccessDenied (`cloudformation:DescribeStackResource` 등 IAM).  
세션 인증 성공과 분리. 본 작업에서 IAM 조치 없음.
