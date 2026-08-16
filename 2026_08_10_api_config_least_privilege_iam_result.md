# `/api/config` 최소 권한 IAM 수정 결과

- 작업일: 2026-08-10
- AWS 계정: `115019372648`
- 리전: `ap-northeast-2`
- 대상 스택: `video-analyzer-stack`
- 라이브 IAM 역할: `AwsFaceDetectionKioskRole`
- 커밋: 없음

## 1. 최종 결과

인증된 `GET /api/config` 요청의 AWS `AccessDenied` 문제를 최소 권한 IAM
정책으로 해결했다.

| 항목 | 수정 전 | 수정 후 |
|---|---|---|
| `GET /api/config` | `503`, `config unavailable: AWS access denied` | **200** |
| 설정 로드 | 실패 | **성공** |
| API base URL | 없음 | **존재 및 배포 API와 일치** |
| API key | 확인 불가 | **존재 확인, 값 비공개** |
| API stage | 확인 불가 | `development` |
| `/api/me` | 인증 성공 | **200, authenticated=true** |
| `/api/detect-labels` | 200 | **200** |
| `/api/capture-frame` | 200 | **200** |
| Kinesis PutRecord | 성공 | **성공** |

## 2. 실제 `/api/config` 호출 순서

`web-ui/backend/app.py`의 `_load_gateway_config()`는 캐시가 없을 때 다음
순서로 AWS를 호출한다.

1. CloudFormation 클라이언트 생성
2. `DescribeStackResource`
   - StackName: `video-analyzer-stack`
   - LogicalResourceId: `VidAnalyzerRestApi`
3. `DescribeStackResource`
   - StackName: `video-analyzer-stack`
   - LogicalResourceId: `VidAnalyzerApiKey`
4. API Gateway v1 클라이언트 생성
5. `GetApiKey(apiKey=<physical-api-key-id>, includeValue=True)`
6. 다음 형식으로 base URL을 애플리케이션 내부에서 생성

```text
https://<rest-api-id>.execute-api.ap-northeast-2.amazonaws.com/development
```

7. 인증된 브라우저에 다음 필드를 반환

```text
apiBaseUrl
apiKey
```

실제 코드는 `DescribeStacks`, `GetRestApi`, `GetStages`를 호출하지 않는다.

## 3. 정확한 장애 원인

첫 번째로 실패한 AWS 작업은 다음과 같다.

```text
cloudformation:DescribeStackResource
```

실패한 실제 호출:

```text
DescribeStackResource(
  StackName="video-analyzer-stack",
  LogicalResourceId="VidAnalyzerRestApi"
)
```

오류 코드:

```text
AccessDenied
```

대상 스택 ARN:

```text
arn:aws:cloudformation:ap-northeast-2:115019372648:stack/video-analyzer-stack/*
```

`VidAnalyzerApiKey`에 대한 두 번째 `DescribeStackResource` 호출도 수정 전에는
동일하게 거부되었다.

## 4. 호출자와 자격 증명 체인

STS와 EC2 IMDS를 통해 다음을 확인했다.

| 항목 | 확인 결과 |
|---|---|
| 인스턴스 프로파일 | `AwsFaceDetectionKioskRole` |
| IAM 역할 | `AwsFaceDetectionKioskRole` |
| AWS 자격 증명 소스 | EC2 instance profile (`iam-role`) |
| boto3 사용 방식 | default credential chain |
| 명시적 access key 환경변수 | 없음 |
| 명시적 AWS profile | 없음 |
| permission boundary | 없음 |

브라우저용 Uvicorn 프로세스는 리전 환경변수만 사용하며 access key, secret key,
session token 또는 AWS profile override를 사용하지 않는다.

## 5. 기존 라이브 IAM 정책 상태

수정 전에 확인된 인라인 정책:

- `AwsFaceDetectionKioskPolicy`
- `AwsFaceDetectionDeployS3Policy`

두 정책 모두 일부 CloudFormation 작업을 허용했지만 실제 코드가 사용하는
`cloudformation:DescribeStackResource`를 포함하지 않았다.

API Gateway는 기존 정책에 의해 이미 읽을 수 있었다. 실제
`GetApiKey(includeValue=True)` 호출을 독립적으로 실행한 결과 성공했으며, API
키 값은 출력하거나 기록하지 않고 존재 여부만 확인했다.

`iam:ListAttachedRolePolicies`는 현재 호출 역할에 허용되지 않아 연결된 managed
policy 이름 전체는 열거하지 못했다. 다만 읽을 수 있었던 인라인 정책과 실제
AWS 호출 재현으로 이번 권한 누락은 확인할 수 있었다.

## 6. 저장소 정책과 라이브 역할 비교

저장소의 `aws-infra/aws-infra-ec2-cfn.yaml`에는 CloudFormation이 생성하는 별도
`WebUiInstanceRole`에 다음 권한이 이미 정의되어 있다.

- `cloudformation:DescribeStackResource`
  - 대상: 데이터 스택 한 개
- `apigateway:GET`
  - 대상: API key 관리 경로

그러나 라이브 EC2는 이 CloudFormation 생성 역할이 아니라 외부에서 관리하는
`AwsFaceDetectionKioskRole`을 사용한다. 또한 현재 계정/리전에
`video-analyzer-ec2-stack`이 존재하지 않는다.

따라서 저장소 템플릿만 수정하거나 EC2를 재생성하지 않고, 라이브 역할에
별도의 최소 인라인 정책을 적용하는 방법을 선택했다.

## 7. 적용한 최소 권한 정책

정책 이름:

```text
AwsFaceDetectionWebUiConfigReadPolicy
```

적용한 정책:

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

이 정책은 다음 이유로 최소 권한이다.

- CloudFormation 작업 한 개만 허용한다.
- `Resource: "*"`를 사용하지 않는다.
- `video-analyzer-stack` 한 개로 범위를 제한한다.
- Kinesis, Lambda, Rekognition, S3, IAM 권한을 함께 추가하지 않는다.
- API Gateway 호출은 이미 성공했으므로 새 권한을 추가하지 않는다.

## 8. API Gateway 최소 권한 분석

현재 코드가 필요로 하는 API Gateway 작업은 다음 하나이다.

```text
Action: apigateway:GET
Resource: arn:aws:apigateway:ap-northeast-2::/apikeys/u72scf8i45
Reason: GetApiKey(includeValue=True)
```

이 권한은 수정 전에 이미 유효했으므로 이번 IAM 정책에는 추가하지 않았다.

## 9. IAM 전파 후 검증

정책 적용 후 두 CloudFormation 호출을 다시 실행했다.

| LogicalResourceId | 리소스 유형 | 상태 |
|---|---|---|
| `VidAnalyzerRestApi` | `AWS::ApiGateway::RestApi` | `CREATE_COMPLETE` |
| `VidAnalyzerApiKey` | `AWS::ApiGateway::ApiKey` | `CREATE_COMPLETE` |

결과:

```text
AccessDenied → success
```

## 10. `/api/config` 검증

직접 FastAPI 경로와 실제 code-server 프록시 경로를 모두 검증했다.

```text
http://127.0.0.1:8080/api/config
https://<host>/proxy/8080/api/config
```

안전한 검증 결과:

```text
http_status=200
config_loaded=true
api_base_url_present=true
api_base_url_matches_deployed_rest_api=true
api_key_present=true
stage=development
```

API 키 실제 값, 세션 쿠키, AWS credential은 출력하거나 파일에 기록하지 않았다.

인증되지 않은 직접 요청은 계속 다음 상태를 반환한다.

```text
GET /api/config → 401
```

따라서 `/api/config` 인증은 약화되지 않았다.

## 11. 운영 UI와 회귀 검증

code-server의 실제 `/proxy/8080` 경로에서 확인한 결과:

| 요청 | 결과 |
|---|---|
| `/` | 200 |
| `/api/me` | 200, `authenticated=true` |
| `/api/config` | 200 |
| `/api/detect-labels` | 200 |
| `/api/capture-frame` | 200 |
| captureId 생성 | 성공 |
| Kinesis 레코드 수락 | 성공 |

`web-ui/src/app.js`는 `/api/config` 성공 시 `configError=null`로 설정한다.
따라서 노란색 설정 경고의 렌더링 조건은 해제된다.

호스트에 Chromium, Firefox, Playwright 등의 headless browser가 없어 화면을
픽셀 단위로 확인하는 시각 테스트는 수행하지 못했다. 실제 프록시 요청과 UI
상태 전환 조건은 검증했다.

## 12. 배포 API 구성 검증

| 항목 | 결과 |
|---|---|
| REST API 이름 | `RtRekogRestApi` |
| REST API ID | `m8ihlusjee` |
| stage | `development` |
| `/enrichedframe` | `GET`, `OPTIONS` |
| `/face-compare` | `POST`, `OPTIONS` |

라우트 이름이나 API Gateway 배포 구성은 변경하지 않았다.

## 13. 테스트 결과

```text
pytest -q tests/test_app_kiosk.py
→ 12 passed
```

전체 `pytest tests/` 실행은 앞의 테스트 75개가 통과한 뒤
`tests/test_session_auth.py`에서 완료되지 않고 정지했다. 관련 테스트만 다시
실행해도 같은 현상이 발생하여 중단했다. 실패 결과는 출력되지 않았다.

실제 실행 중인 FastAPI 및 code-server 프록시를 통한 인증/config/capture
검증은 모두 통과했다.

```text
git diff --check
→ PASS
```

애플리케이션 Python/JavaScript 코드는 이번 작업에서 변경하지 않아 별도의
`py_compile` 또는 `node --check` 대상은 없다.

## 14. 보안 확인

- `AdministratorAccess` 추가 없음
- `cloudformation:*` 추가 없음
- `apigateway:*` 추가 없음
- `iam:*` 추가 없음
- access key 생성 없음
- EC2 교체 또는 재생성 없음
- `video-analyzer-stack` 교체 또는 재생성 없음
- frontend AWS credentials 추가 없음
- API key 값 기록 없음
- AWS credentials 기록 없음
- 세션 쿠키 기록 없음
- `SESSION_SECRET` 기록 없음
- 비밀번호 기록 없음
- `/api/config` 익명화 없음
- 인증 우회 없음
- SQLite 및 captureId 로직 변경 없음

기존 역할에 이미 존재하던 광범위한 배포용 권한은 이번 작업에서 추가하거나
확장하지 않았다.

## 15. 변경 사항

### AWS IAM

- 라이브 역할 `AwsFaceDetectionKioskRole`에
  `AwsFaceDetectionWebUiConfigReadPolicy` 추가

### 저장소

- 애플리케이션 코드 변경 없음
- IAM CloudFormation 템플릿 변경 없음
- 결과 보고서만 추가/갱신
- 커밋 없음

## 16. 결론

`/api/config`의 장애 원인은 세션이나 리소스 부재가 아니라 라이브 EC2 역할의
정확한 `cloudformation:DescribeStackResource` 권한 누락이었다.

한 개 CloudFormation 작업을 한 개 스택 ARN에만 허용하는 인라인 정책을
적용하여 다음 흐름이 정상화되었다.

```text
authenticated browser
→ GET /api/config
→ FastAPI
→ CloudFormation DescribeStackResource × 2
→ API Gateway GetApiKey
→ API configuration returned
→ HTTP 200
```

작업 중 커밋은 생성하지 않았다.
