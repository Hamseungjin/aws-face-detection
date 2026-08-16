# code-server `/proxy/8080` 세션 쿠키 생명주기 진단

**일자:** 2026-08-10  
**저장소:** `/home/ubuntu/workspace/aws-face-detection`  
**브랜치:** `devhsj`  
**범위:** 코드 변경 없음 / 커밋 없음 / SessionMiddleware 설정 변경 없음 / `WEBUI_SESSION_HTTPS_ONLY` 변경 없음 / AWS 변경 없음  

---

## 0. 배경

| 항목 | 내용 |
|---|---|
| 브라우저 URL | `https://54.116.158.29/proxy/8080/` |
| 관측 증상 | `POST /api/login` → 200, `GET /api/me` → 200 인데 UI에 세션 쿠키 미유지 메시지 표시 |
| UI 메시지 | `로그인은 됐지만 세션 쿠키가 유지되지 않습니다...` |
| 이전 결과 | **직접 FastAPI** 세션 테스트는 성공 |
| 가설 | `/api/me` 가 HTTP 200 이어도 `authenticated=false` 일 가능성 |

프론트엔드(`web-ui/src/app.js`)는 login 200 이후 `GET /api/me` 에서 `authenticated=true` 를 확인한 뒤에만 UI를 로그인 상태로 전환한다.  
HTTP 200 만으로 인증 성공으로 간주하면 안 된다.

---

## 1. `/api/me` JSON body (브라우저 로그인 직후)

**확인된 실패 본문 (HTTP 200):**

```json
{"authenticated": false, "user": null}
```

| 프로브 | 결과 |
|---|---|
| Live `GET https://127.0.0.1/proxy/8080/api/me` (webui 쿠키 없음) | `{"authenticated":false,"user":null}` HTTP 200 |
| Mirror 로그인 via proxy → `GET /proxy/8098/api/me` | 동일 본문 + `request_cookie_present=true`, `request_session_user_present=false` |

**결론:** 브라우저 로그인 직후 `/api/me` 는 200 이지만 본문은 비인증이다.

---

## 2. 브라우저 대면 URL

페이지: `https://54.116.158.29/proxy/8080/`

프론트 `getApiBaseUrl()` 은 pathname 의 `^(/proxy/\d+)` 를 매칭해 base 를 `/proxy/8080/api` 로 만든다.

| 호출 | 브라우저 대면 URL |
|---|---|
| POST login | `https://54.116.158.29/proxy/8080/api/login` |
| GET me | `https://54.116.158.29/proxy/8080/api/me` |

### code-server path strip

code-server 는 백엔드로 넘기기 전에 `/proxy/<port>` 를 제거한다.

- 브라우저: `/proxy/8080/api/me`
- FastAPI 가 보는 경로: `/api/me`

Mirror 진단에서 `path_seen: "/api/me"` 로 확인했다.

---

## 3. Login `Set-Cookie` (proxy 경유)

### Direct FastAPI (SessionMiddleware, 앱과 동일 플래그)

| 필드 | 값 |
|---|---|
| Set-Cookie present | **true** |
| cookie_name | `webui_session` |
| Path | `/` |
| Domain | *(미설정 / host-only)* |
| Secure | **false** |
| HttpOnly | **true** |
| SameSite | **lax** |
| Max-Age | **28800** |

### code-server proxy 경유 (POST `/proxy/8098/api/login`)

동일 속성으로 전달됨. Set-Cookie **제거되지 않음**.

| 질문 | 답 |
|---|---|
| FastAPI 가 Set-Cookie 를 생성하는가? | **Yes** |
| Set-Cookie 가 code-server proxy 를 통과하는가? | **Yes** (`proxyRes` 는 `Location` 만 재작성, `Set-Cookie` 미변경) |

쿠키 **값(value)** 은 본 문서에 기록하지 않는다.

---

## 4. 브라우저 저장 여부 (`webui_session`)

프록시 로그인 후 쿠키 jar(브라우저 저장 규칙과 동일 계열) 기준:

| 필드 | 값 |
|---|---|
| stored | **true** |
| path | `/` |
| secure | **false** |
| same_site | **lax** |
| domain | host-only (로컬 프로브: `127.0.0.1`, 실제 브라우저 호스트: `54.116.158.29`) |

---

## 5. 다음 `/api/me` 요청

| 검사 | 결과 |
|---|---|
| `browser_cookie_sent` | **true** |
| FastAPI `request_cookie_present` | **true** (`webui_session` 이 Cookie 헤더에 존재) |
| FastAPI `request_session_user_present` | **false** |
| `/api/me` body | `authenticated=false`, `user=null` |

**결론:** 쿠키는 전송되고 FastAPI 에 이름으로는 도착한다. 세션 payload 의 `user` 는 복원되지 않는다.

---

## 6. 첫 번째 깨진 단계 (FIRST BROKEN STEP)

### 판정: **G** (FastAPI 쪽 증상은 **F**)

| 코드 | 의미 | 해당 여부 |
|---|---|---|
| A | FastAPI 가 Set-Cookie 를 만들지 않음 | 해당 없음 |
| B | FastAPI 가 만들었으나 proxy 가 제거/변경 | Set-Cookie 자체는 생존 → 해당 없음 |
| C | 브라우저가 받아도 저장하지 않음 | 저장됨 → 해당 없음 |
| D | 저장했지만 `/proxy/.../api/me` 에 안 보냄 | 보냄 → 해당 없음 |
| E | 브라우저가 보냈으나 proxy 가 전달하지 않음 | 이름으로는 전달됨 → 순수 E 아님 |
| F | FastAPI 가 쿠키를 받았으나 SessionMiddleware 가 decode 실패 | **증상으로 해당** |
| **G** | **다른 확인된 원인** | **첫 번째 깨진 단계** |

### 정확히 깨지는 지점

1. Set-Cookie 발급 OK  
2. Proxy 를 통한 Set-Cookie 전달 OK  
3. 브라우저 저장 OK  
4. 브라우저가 Cookie 전송 OK  
5. **code-server 가 upstream 으로 Cookie 헤더를 재조립하면서 값 변조** ← **첫 실패**  
6. FastAPI 에 쿠키 이름은 있으나 session user 복원 실패  

---

## 7. 정확한 근본 원인

code-server `proxyReq` 는 로컬 포트로 `code-server-session` 이 유출되지 않도록 Cookie 헤더를 **재조립**한다.

```javascript
// /usr/lib/code-server/out/node/proxy.js
exports.proxy.on("proxyReq", (preq, req) => {
    const cookieSessionName = getCookieSessionName(req.args["cookie-suffix"]);
    preq.setHeader(
        "Cookie",
        cookie.stringifyCookie(
            Object.assign({}, req.cookies, { [cookieSessionName]: undefined })
        )
    );
});
```

`stringifyCookie` 는 값에 **`encodeURIComponent`** 를 적용한다.

Starlette `SessionMiddleware` 세션 쿠키 값은 서명된 payload 이며 base64 padding **`=`** 를 포함한다.

| 변환 | 결과 |
|---|---|
| `=` | `%3D` |

### 측정 결과

| 쿠키 값 형태 | `/api/me` |
|---|---|
| 원본 (`=` padding 유지) | `authenticated=true` |
| encodeURIComponent 적용 후 (`%3D`) | `authenticated=false`, 쿠키 이름은 존재 |
| Starlette `request.cookies` 가 `abc%3Ddef` 를 파싱 | 리터럴 `%3D` 유지 (자동 unquote 안 함) |

### 실제 code-server 경로 end-to-end (mirror `/proxy/8098`)

동일 SessionMiddleware 설정 (`webui_session`, `same_site=lax`, `https_only=False`):

1. Login 200 + Set-Cookie OK  
2. 쿠키 저장 OK  
3. `/api/me` → `request_cookie_present=true`  
4. `request_session_user_present=false` → `authenticated=false`  

**직접 FastAPI( proxy 없음 )** 에서는 동일 쿠키로 세션이 정상이다.  
→ “직접 세션 테스트 성공, 브라우저 `/proxy/8080` 실패” 패턴과 일치.

### Live 8080

- `GET /proxy/8080/api/me` (비로그인): `{"authenticated":false,"user":null}`  
- Live 앱 비밀번호 없이 full login 은 재현하지 않았고, mirror + 재인코딩 단위 테스트 + 동일 SessionMiddleware 설정으로 원인을 확정했다.

---

## 8. 쿠키 이름 충돌

호스트 `54.116.158.29` (로컬 프로브는 `127.0.0.1`) 기준:

| Cookie | 역할 | Path |
|---|---|---|
| `code-server-session` | code-server 인증 | `/` |
| `webui_session` | 앱 세션 | `/` |

- **동일 이름 `webui_session` 중복(다른 Path/Domain)** 은 관측되지 않음  
- 이름 충돌이 실패 원인이 아님  
- 공유 호스트 자체는 문제 없음; 실패 원인은 **값 재인코딩**

쿠키 값은 기록하지 않음.

---

## 9. 증거 요약 표

| # | 항목 | 결과 |
|---|---|---|
| 1 | `/api/me` JSON (브라우저 로그인 후) | `{"authenticated": false, "user": null}` |
| 2 | 브라우저 대면 login URL | `https://54.116.158.29/proxy/8080/api/login` |
| 3 | 브라우저 대면 `/api/me` URL | `https://54.116.158.29/proxy/8080/api/me` |
| 4 | Set-Cookie 가 proxy 를 통과 | **yes** |
| 5 | 브라우저에 `webui_session` 저장 | **yes** |
| 6 | `/api/me` 에 `webui_session` 전송 | **yes** |
| 7 | FastAPI 가 쿠키 수신 | **yes** (이름 존재) |
| 8 | SessionMiddleware 가 user 복원 | **no** |
| 9 | **FIRST BROKEN STEP** | **G** (proxy 가 Cookie 를 `encodeURIComponent` 로 변조; FastAPI 증상 = **F**) |
| 10 | exact root cause | code-server `proxyReq` 가 Cookie 를 재조립하며 Starlette 세션의 `=` 를 `%3D` 로 바꿈 → decode 실패 → `authenticated=false` |
| 11 | minimal recommended fix | 아래 §10 (미적용) |

---

## 10. 권장 최소 수정 (아직 적용하지 않음)

### 권장 (앱 측, 최소)

`SessionMiddleware` **앞**에 작은 ASGI 미들웨어를 두고, 세션 쿠키 이름(`webui_session`)의 Cookie 값만 **`urllib.parse.unquote`** 한다.

- code-server 의 `encodeURIComponent` 를 요청 유입 시 되돌림  
- 직접 `http://127.0.0.1:8080` 접속: `%` 없으면 no-op → 기존 동작 유지  
- SessionMiddleware 설정 / `WEBUI_SESSION_HTTPS_ONLY` / SameSite / Secure 를 바꿀 필요 없음  
- HttpOnly 제거, 인증 비활성화, Domain=공인 IP 하드코딩 금지  

### 운영 우회

code-server `/proxy` 없이 UI 에 직접 접속 (로컬 HTTP 등). 이미 직접 FastAPI 경로는 정상.

### 하지 말 것 (이 원인 기준)

- 인증 비활성화  
- HttpOnly 제거  
- 공인 IP 를 Domain 으로 하드코딩  
- Secure / SameSite 를 근거 없이 변경  
- `WEBUI_SESSION_HTTPS_ONLY` 를 이 이슈 때문에 변경  
- AWS 재배포  
- `/api/config` IAM 수정  
- 키오스크 트랜잭션 로직 변경  

---

## 11. 진단 방법 메모

| 방법 | 내용 |
|---|---|
| Live uvicorn | `127.0.0.1:8080` |
| code-server | `0.0.0.0:443`, auth=password, cert=true |
| Proxy 쿠키 동작 | `/usr/lib/code-server/out/node/proxy.js` 소스 확인 |
| Set-Cookie 통과 | 임시 probe 서버 + mirror FastAPI |
| 재인코딩 단위 검증 | 원본 vs `encodeURIComponent` 쿠키를 직접 `/api/me` 에 전송 비교 |
| Node 검증 | code-server 번들 `cookie.stringifyCookie` 가 `%3D` 생성 확인 |
| E2E | code-server 로그인 후 `/proxy/8098/api/login` → `/api/me` |

**보안:** 비밀번호, 세션 시크릿, 쿠키 값은 본 문서에 포함하지 않음.

---

## 12. 변경/커밋 상태

| 항목 | 상태 |
|---|---|
| 애플리케이션 코드 수정 | **없음** |
| SessionMiddleware 변경 | **없음** |
| `WEBUI_SESSION_HTTPS_ONLY` 변경 | **없음** |
| AWS 변경 | **없음** |
| git commit | **없음** |
| 본 문서 | 진단 결과 기록 전용 |

---

*End of report.*
