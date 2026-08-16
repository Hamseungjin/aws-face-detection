# code-server Cookie 재인코딩 호환 수정 — 결과 보고

**날짜:** 2026-08-10  
**작업 범위:** code-server `/proxy/<port>` 세션 쿠키 percent-encoding 호환  
**커밋:** 없음 (Do not commit)

관련 문서:

- `2026_08_10_proxy_session_cookie_diagnosis.md` — 원인 진단
- `2026_08_10_session_auth_401_result.md` — 세션 401 후속 + §14
- `2026_08_10_grok_report_01.md` — 통합 이력 + Compatibility Fix 섹션 + Appendix D

---

## 1. 확정 원인

직접 FastAPI 세션 인증은 정상이다.

code-server 경로:

```text
https://54.116.158.29/proxy/8080/
```

에서는 프록시가 요청 `Cookie` 헤더를 재조립하면서 **앱 세션 쿠키 값**을 `encodeURIComponent` 스타일로 percent-encoding 한다.

| 원본 (Starlette 서명 세션) | code-server 변환 |
|---|---|
| base64 padding `=` | `%3D` |

### 수명 주기

| 단계 | 결과 |
|---|---|
| FastAPI `Set-Cookie` | OK |
| code-server `Set-Cookie` 전달 | OK |
| 브라우저 `webui_session` 저장 | OK |
| 브라우저 쿠키 전송 | OK |
| code-server 수신 | OK |
| **code-server Cookie 재조립** | **값 변조** |
| FastAPI 쿠키 **이름** 수신 | OK |
| `SessionMiddleware` 서명 복원 | **FAIL** |
| `request.session["user"]` | 없음 |
| `GET /api/me` | `authenticated=false` |

**원인이 아닌 것:** Secure, SameSite, Domain, localhost vs 127.0.0.1, AWS, CloudFormation, IAM.

---

## 2. 적용한 수정

### 2.1 아키텍처

```text
Browser request
    ↓
CodeServerSessionCookieCompatMiddleware   ← outermost
    ↓
SessionMiddleware
    ↓
FastAPI routes
```

Starlette `add_middleware()` 는 **마지막에 추가한 미들웨어가 outermost** (요청 시 먼저 실행).  
Starlette 1.6.0 에서 확인함.

| 순서 (요청 방향) | 클래스 |
|---|---|
| 1 (outer) | `CodeServerSessionCookieCompatMiddleware` |
| 2 (inner) | `SessionMiddleware` |

### 2.2 정규화 규칙

| 규칙 | 내용 |
|---|---|
| 대상 스코프 | HTTP only (websocket 등 비HTTP 불변) |
| 대상 쿠키 | `config.SESSION_COOKIE` 만 (기본 `webui_session`) |
| 동작 | 해당 **값**에 `urllib.parse.unquote` **정확히 1회** |
| 기타 쿠키 | 세그먼트 그대로 유지 (`code-server-session` 포함) |
| 직접 접속 | percent escape 없으면 no-op |
| 이중 인코딩 | 1회만 (`%253D` → `%3D`, `=` 까지 가지 않음) |
| 보안 | 서명 검증은 계속 `SessionMiddleware` 가 담당 |
| 로그 | Cookie 헤더/세션 값 **절대 기록 안 함** (DEBUG: `proxy_cookie_normalized=true` 만) |

### 2.3 보안 유지

- HttpOnly / SameSite / Secure (`WEBUI_SESSION_HTTPS_ONLY`) 유지
- 세션 서명 / `SESSION_SECRET` 유지
- 전역 Cookie decode 없음
- 인증 우회 없음
- Domain=공인 IP 하드코딩 없음
- AWS IAM / CloudFormation 변경 없음

### 2.4 프론트 메시지

`web-ui/src/app.js`, `web-ui/src/kiosk.js` 세션 확인 실패 문구를 중립적으로 교체:

> 로그인은 성공했지만 세션을 확인하지 못했습니다.  
> 브라우저 쿠키 또는 프록시 설정을 확인해 주세요.

---

## 3. 변경 파일

| 파일 | 내용 |
|---|---|
| `web-ui/backend/session_cookie_compat.py` | **신규** 정규화 함수 + ASGI 미들웨어 |
| `web-ui/backend/app.py` | 미들웨어 등록 + 순서 주석 |
| `tests/test_session_auth.py` | 케이스 A–G |
| `web-ui/src/app.js` | 중립 에러 메시지 |
| `web-ui/src/kiosk.js` | 동일 메시지 |
| `2026_08_10_grok_report_01.md` | Compatibility Fix 섹션 + Appendix D |
| `2026_08_10_session_auth_401_result.md` | §14 구현·검증 |
| `2026_08_10_codeserver_cookie_compat_result.md` | 본 결과 문서 |

---

## 4. 검증 결과

### 4.1 직접 FastAPI — `http://127.0.0.1:8080`

| 검사 | 결과 |
|---|---|
| 유효 세션 → `GET /api/me` | `authenticated=true` |
| code-server 스타일 percent-encoding 시뮬레이션 | `authenticated=true` (미들웨어 복원) |
| `GET /api/detect-labels` | **200** (NOT 401) |
| `POST /api/capture-frame` | **200**, `captureId` 생성 |
| 쿠키 없음 → `/api/me` | `authenticated=false` |

### 4.2 code-server `/proxy/8080` (권위 있는 경로)

실제 code-server `proxyReq` Cookie 재조립을 거침.

검증 URL (동일 호스트 로컬 프록시; 공개 IP와 동일 code-server 경로):

```text
https://127.0.0.1/proxy/8080/
```

| API | 수정 전 | 수정 후 |
|---|---|---|
| `GET .../api/me` | `authenticated=false` | **`authenticated=true`** |
| `GET .../api/detect-labels` | 세션 실패 | **200** |
| `POST .../api/capture-frame` | 세션 실패 | **200**, `captureId_present=true` |
| `GET .../api/config` | (세션과 별개) | **503** `AWS access denied` |

> 참고: 에이전트 환경에서 공인 IP(`54.116.158.29`) 로그인 자동화가 정책상 차단되어, 동일 프로세스의 `https://127.0.0.1/proxy/8080/` 로 live Cookie 재조립을 검증했다.

### 4.3 카메라 / 로그인 화면

프록시를 통과한 유효 세션에서 보호 API 가 더 이상 401 을 내지 않으므로, 카메라 시작 후 로그인 화면으로 튕기는 세션 실패 모드는 해소된 것으로 본다.  
브라우저 클릭 UI E2E 는 이 환경에서 자동화하지 않았다.

### 4.4 captureId / AWS 파이프라인

| 항목 | 결과 |
|---|---|
| `captureId` 생성 | **true** (직접·프록시 모두) |
| 이미지/base64 로그 | 없음 |
| Kinesis put | 성공 응답 (sequenceNumber/shardId 수신) |

### 4.5 자동화 검사

| 검사 | 결과 |
|---|---|
| `pytest tests/` | **91 passed** |
| `python -m py_compile` (변경 Python) | OK |
| `node --check web-ui/src/app.js` | OK |
| `node --check web-ui/src/kiosk.js` | OK |
| `git diff --check` | OK |
| 커밋 | **없음** |

### 4.6 테스트 커버리지 (A–G)

| ID | 내용 | 결과 |
|---|---|---|
| A | 정상 직접 쿠키 → authenticated=true | PASS |
| B | code-server 스타일 인코딩 쿠키 → authenticated=true | PASS |
| C | 호환 미들웨어 없이 인코딩 쿠키 → authenticated=false | PASS |
| D | 무관 쿠키 보존 | PASS |
| E | 세션 없음 → unauthenticated / 401 | PASS |
| F | 위조 세션은 유효 세션으로 바뀌지 않음 | PASS |
| G | 이중 인코딩은 1회만 복원 | PASS |

---

## 5. 남은 이슈 (`/api/config` IAM)

세션 인증 성공 후에도:

```text
GET /proxy/8080/api/config
→ 503
detail: config unavailable: AWS access denied
```

원인 후보: `cloudformation:DescribeStackResource` 등 인스턴스/호출자 IAM 권한 부족.

| 상태 | 의미 |
|---|---|
| `/api/me` = `authenticated=true` | **세션 인증 SUCCESS** |
| `/api/config` = 503 AccessDenied | **별도 IAM 이슈** (본 작업 범위 밖) |

IAM/CloudFormation 재배포는 수행하지 않았다.

---

## 6. 런타임

| 항목 | 값 |
|---|---|
| 프로세스 | uvicorn `app:app --host 127.0.0.1 --port 8080` |
| venv | `web-ui/backend/.venv` |
| 미들웨어 반영 | 코드 변경 후 uvicorn **재시작 완료** |
| EC2 리부트 | 없음 |
| AWS 인프라 재배포 | 없음 |

---

## 7. 최종 체크리스트 (18항)

| # | 항목 | 결과 |
|---|---|---|
| 1 | 변경 파일 | 위 §3 |
| 2 | 미들웨어 순서 | Compat → SessionMiddleware → routes |
| 3 | 정규화 동작 | session cookie 값 unquote 1회 |
| 4 | `webui_session` 만 수정 | **Yes** |
| 5 | 직접 `/api/me` | `authenticated=true` |
| 6 | 프록시 `/api/me` BEFORE | `authenticated=false` |
| 7 | 프록시 `/api/me` AFTER | `authenticated=true` |
| 8 | 프록시 `/api/detect-labels` | **200** |
| 9 | 프록시 `/api/capture-frame` | **200** + captureId |
| 10 | 카메라 → 로그인 리다이렉트 | 세션 401 원인 제거 (UI 수동 확인 권장) |
| 11 | captureId | 생성됨 |
| 12 | pytest | **91 passed** |
| 13 | py_compile | OK |
| 14 | node --check | OK |
| 15 | git diff --check | OK |
| 16 | `/api/config` IAM | 503 AccessDenied 잔존 |
| 17 | 리포트 갱신 | grok_report_01 + session_auth_401_result + 본 문서 |
| 18 | 커밋 | **없음** |

---

## 8. 운영 참고

브라우저 접속 시 호스트를 하나로 고정하는 것은 여전히 권장한다 (`localhost` 와 `127.0.0.1` 혼용 금지).  
다만 **code-server `/proxy` 세션 실패의 근본 원인은 호스트 혼용이 아니라 Cookie 값 재인코딩**이었다.

로컬 직접 접속:

```text
http://127.0.0.1:8080
```

code-server 프록시:

```text
https://<host>/proxy/8080/
```

`.env` / 세션 플래그 변경 후에는 uvicorn 재시작이 필요하다.

---

## 9. `/api/config` IAM 후속 해결 (2026-08-10)

이 문서의 §5 및 체크리스트 16번에 기록된 별도 IAM 이슈는 후속
least-privilege 작업으로 해결되었다. 세션/쿠키 코드는 변경하지 않았다.

### 확인된 원인

`AwsFaceDetectionKioskRole`의 기존 인라인 정책들은
`cloudformation:DescribeStacks` 등은 허용했지만, 실제 FastAPI 코드가 첫 번째로
호출하는 `cloudformation:DescribeStackResource`를 허용하지 않았다.

실패한 대상은 다음 한 스택이었다.

```text
arn:aws:cloudformation:ap-northeast-2:115019372648:stack/video-analyzer-stack/*
```

API Gateway의 실제 `GetApiKey(includeValue=True)` 호출은 기존 권한으로 이미
성공했으므로 API Gateway 권한은 추가하지 않았다. API 키 값은 출력하거나
기록하지 않았다.

### 적용한 최소 정책

라이브 커스텀 역할에 별도 인라인 정책
`AwsFaceDetectionWebUiConfigReadPolicy`를 자동 적용했다.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadVideoAnalyzerStackResourcesForWebUiConfig",
      "Effect": "Allow",
      "Action": "cloudformation:DescribeStackResource",
      "Resource": "arn:aws:cloudformation:ap-northeast-2:115019372648:stack/video-analyzer-stack/*"
    }
  ]
}
```

저장된 정책을 다시 읽어 정확한 action/resource 범위를 확인했으며, EC2 또는
`video-analyzer-stack` 교체/재생성은 없었다. 저장소의
`aws-infra/aws-infra-ec2-cfn.yaml`은 이미 별도의 CloudFormation 생성 역할에
의도한 권한을 정의하지만, 현재 호스트는 그 역할/스택이 아니라 외부 관리
`AwsFaceDetectionKioskRole`을 사용하므로 템플릿 변경만으로는 라이브 호스트가
수정되지 않는다.

### 후속 검증

| 항목 | 결과 |
|---|---|
| 두 `DescribeStackResource` 호출 | **성공** (`CREATE_COMPLETE` 리소스 확인) |
| 직접 authenticated `/api/config` | **200** |
| code-server `/proxy/8080/api/config` | **200** |
| `config_loaded` | **true** |
| API base URL | 배포 REST API + `development` stage와 일치 |
| API key presence | **true** (값 비공개) |
| `/proxy/8080/api/me` | **200**, `authenticated=true` |
| `/proxy/8080/api/detect-labels` | **200** |
| `/proxy/8080/api/capture-frame` | **200**, captureId 생성, Kinesis 수락 |
| `/enrichedframe` | `GET`, `OPTIONS` 존재 |
| `/face-compare` | `POST`, `OPTIONS` 존재 |

`app.js`는 `/api/config` 성공 시 `configError=null`로 설정하므로 노란 경고의
렌더링 조건은 해제된다. 실제 프록시 데이터 경로는 검증했으나 호스트에
headless browser가 없어 화면 픽셀 단위 시각 검증은 수행하지 않았다.

보안상 `AdministratorAccess`, `cloudformation:*`, `apigateway:*`, `iam:*`를
추가하지 않았고, 인증을 우회하거나 frontend AWS credentials/secrets를
추가하지 않았다. 이 후속 작업에서도 커밋은 생성하지 않았다.
