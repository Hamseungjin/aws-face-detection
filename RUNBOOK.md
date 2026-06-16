# 실행 가이드 (RUNBOOK)

메인 영상 분석 파이프라인을 **처음부터 끝까지** 돌리는 런북입니다. 순서는
**사전 준비 → 배포 → 실행 → 정리** 4단계이며, Windows PowerShell 기준으로 작성했습니다.

> **요약:** [README.md](README.md) "5. 배포 및 실행 순서"의 명령 순서
> (`packagelambda → deploylambda → createstack → stackstatus → webui → webuiserver → videocapture`)
> 는 [build.py](build.py)의 실제 의존 흐름과 **일치합니다.** 다만 그대로만 따라 하면 실패합니다.
> 이 문서는 빠진 3가지 — **① 사전 준비, ② `webuiserver`는 별도 터미널, ③ 정리(비용 방지)** — 를 포함합니다.
>
> 신분증 얼굴 비교 CLI(키오스크 인증)는 이 파이프라인과 별개이며 [runner.md](runner.md)를 참고하세요.

---

## 0. 전체 흐름 한눈에

```text
[1. 사전 준비]  AWS 자격증명 + 의존성 설치 + config/*.json 값 교체
        │
[2. 배포]       pynt packagelambda → deploylambda → createstack → stackstatus   (터미널 1개, 순차)
        │
[3. 실행]       pynt webui
                ├─ 터미널 A: pynt webuiserver         (http://localhost:8080, 점유됨)
                ├─ 터미널 B: pynt videocapture[20]    (웹캠 캡처)
                └─ 브라우저: http://localhost:8080
        │
[4. 정리]       pynt deletedata (선택) → pynt deletestack (필수, 비용 방지)
```

---

## 1. 사전 준비 (명령 실행 전 필수)

### 1-1. AWS

- AWS 계정 + 관리자 권한 IAM 사용자(액세스 키 발급).
- AWS CLI 설치 후 자격증명/리전 설정. 리전은 `ap-northeast-2`(서울) 권장:

  ```powershell
  aws configure
  # AWS Access Key ID     : <IAM 액세스 키>
  # AWS Secret Access Key : <IAM 시크릿 키>
  # Default region name   : ap-northeast-2
  # Default output format : json
  ```

  > `pynt` 태스크는 boto3 기본 세션의 리전을 그대로 사용합니다([build.py:109](build.py#L109), [build.py:347](build.py#L347)). `aws configure`의 리전과 `config`의 버킷 이름 리전 suffix를 **반드시 일치**시키세요.

  > ⚠️ **`pynt`(build.py)는 `default` 프로필 자격증명을 사용합니다** (`--profile`을 지정하지 않음). [runner.md](runner.md)의 얼굴 비교 CLI 안내대로 `video-analyzer` 같은 **named 프로필만** 만든 경우, `pynt`는 그 프로필을 보지 못해 `NoCredentialsError: Unable to locate credentials`가 납니다. 같은 터미널에서 아래처럼 프로필을 지정한 뒤 `pynt` 명령을 실행하세요.
  >
  > ```bash
  > export AWS_PROFILE=video-analyzer        # MINGW64 / bash
  > ```
  > ```powershell
  > $env:AWS_PROFILE = "video-analyzer"      # PowerShell
  > ```
  >
  > 이 환경변수는 같은 터미널 세션의 이후 모든 `pynt` AWS 명령(`deploylambda`/`createstack`/`stackstatus`/`webui`/`deletedata`/`deletestack`)에 적용됩니다. 또는 `aws configure`를 (`--profile` 없이) 실행해 `[default]` 프로필을 만들면 환경변수 없이도 동작합니다.

### 1-2. Python 의존성

이 저장소는 `.venv`(Python 3.11)를 사용합니다. 필요한 패키지:

```powershell
.\.venv\Scripts\python.exe -m pip install boto3 opencv-python pynt pytz numpy
```

- `boto3` (AWS SDK), `opencv-python`(=`cv2`, 영상 캡처), `pynt`(빌드 태스크 러너), `pytz`, `numpy`.

### 1-3. 설정 파일 작성 (가장 자주 빠뜨리는 단계)

`.gitignore`에 `config/*`가 있어 설정 파일은 추적되지 않습니다. 본인 AWS 환경에 맞게 값을 채워야 합니다.
**특히 `115019372648`(예시 계정 ID)을 본인 AWS 계정 ID로 바꾸고, S3 버킷명을 전역에서 유일한 이름으로** 바꾸세요.

| 파일 | 바꿔야 하는 키 | 이유 |
| --- | --- | --- |
| [config/cfn-params.json](config/cfn-params.json) | `SourceS3BucketParameter` | Lambda ZIP을 올릴 S3 버킷 (전역 유일) |
| | `FrameS3BucketNameParameter` | 캡처 프레임 저장 S3 버킷 (전역 유일) |
| | `ApiGatewayRestApiNameParameter` / `ApiGatewayStageNameParameter` | API Gateway 이름/스테이지 (기본값 사용 가능) |
| [config/global-params.json](config/global-params.json) | `StackName` | CloudFormation 스택 이름 (기본 `video-analyzer-stack`) |
| [config/imageprocessor-params.json](config/imageprocessor-params.json) | `s3_bucket` | 위 `FrameS3BucketNameParameter`와 **동일하게** |
| | `label_watch_list` | 알림 대상 라벨 (기본 `Person, Dog, Cat, Bag, Backpack, Toy`) |
| | `label_watch_phone_num` | SMS 수신 번호 (비우면 SMS 비활성) |
| | `timezone` | 기본 `Asia/Seoul` |
| [config/framefetcher-params.json](config/framefetcher-params.json) | `fetch_horizon_hrs` / `fetch_limit` | Web UI가 조회할 범위/개수 (기본값 사용 가능) |

> `SourceS3BucketParameter`/`FrameS3BucketNameParameter`는 [build.py](build.py)의 `deploylambda`·`createstack`·`deletestack`이 직접 읽습니다. 버킷명이 이미 다른 계정에서 쓰는 이름이면 배포가 실패하므로 유일한 이름으로 바꾸세요.

---

## 2. 배포 (터미널 1개에서 순차 실행)

각 단계는 직전 단계의 산출물에 의존하므로 **순서대로** 실행합니다.

```powershell
pynt packagelambda   # lambda/* 코드를 build/framefetcher.zip, build/imageprocessor.zip, build/facecompare.zip 으로 패키징
pynt deploylambda    # build/*.zip 을 S3에 업로드 (버킷이 없으면 자동 생성)
pynt createstack     # CloudFormation으로 Kinesis/Lambda/S3/DynamoDB/API Gateway/IAM 생성
pynt stackstatus     # 스택 상태 확인 → 'CREATE_COMPLETE' 가 나오면 성공
```

- `deploylambda`는 `build/*.zip`을 읽으므로 `packagelambda`가 먼저여야 합니다([build.py:142](build.py#L142)).
- `createstack`은 S3에 업로드된 ZIP을 참조하므로 `deploylambda` 다음입니다. **`CREATE_COMPLETE`까지 자동 대기**하며 수 분이 걸립니다([build.py:182-186](build.py#L182-L186)).
- `stackstatus`로 `CREATE_COMPLETE`를 확인한 뒤 다음 단계로 넘어가세요.

---

## 3. 실행 (터미널 2개 + 브라우저)

먼저 Web UI 설정 파일을 생성합니다(스택이 만들어진 뒤여야 함):

```powershell
pynt webui   # 스택에서 API URL + API Key를 조회해 build/web-ui/src/apigw.js 생성
```

그다음 **서버와 캡처를 각각 다른 터미널에서** 띄웁니다.

- **터미널 A — Web UI 서버**

  ```powershell
  pynt webuiserver
  ```

  > ⚠️ 이 명령은 `serve_forever()`로 **터미널을 계속 점유**합니다([build.py:375](build.py#L375)). 닫으면 서버도 종료됩니다. 그래서 캡처는 반드시 **다른 터미널**에서 실행해야 합니다.

- **터미널 B — 영상 캡처** (웹캠, 20프레임당 1장)

  ```powershell
  pynt videocapture[20]
  ```

  - 카메라 앞에 사람/객체가 보이게 해야 Rekognition이 라벨을 감지합니다.
  - IP 카메라 / 스마트폰 MJPEG 스트림을 쓸 경우:

    ```powershell
    pynt videocaptureip["http://192.168.0.2/video",20]
    ```

  > ⚠️ **얼굴 비교(웹 UI "최근 프레임과 얼굴 비교" 버튼)를 쓰려면 이 캡처가 돌고 있어야 합니다.**
  > 비교는 DynamoDB에 저장된 **가장 최신 프레임**과 업로드한 얼굴을 대조하는데, 그 프레임의 캡처 시각이 **5분을 넘으면** `NO_RECENT_FRAME`으로 막히고 화면에 `"최근 프레임이 너무 오래됐어요 (캡처가 실행 중인지 확인)."`가 뜹니다([facecompare.py:317-323](lambda/facecompare/facecompare.py#L317-L323)).
  >
  > - 5분 한도는 [config/facecompare-params.json](config/facecompare-params.json)의 `latest_frame_horizon_minutes`(기본 `5`, 분 단위) 설정값이며 `age > 5분`이면 차단됩니다.
  > - 그러므로 **터미널 B의 `pynt videocapture[20]`를 켜 둔 상태에서** 비교를 누르세요. `[20]`은 캡처 레이트(20프레임당 1장)일 뿐 — 값은 무엇이든 되고, 핵심은 캡처가 계속 돌아 새 프레임이 들어오는 것입니다.
  > - 캡처를 멈추면 마지막 프레임 기준 약 5분 뒤부터 막히고, 프레임이 **한 번도** 만들어진 적 없으면 `NO_RECENT_FRAME`이 아니라 `NO_LATEST_FRAME`(`"저장된 프레임이 없어요..."`)이 뜹니다 → 먼저 캡처를 시작하세요.

- **브라우저** — 분석 결과 확인:

  ```text
  http://localhost:8080
  ```

---

## 4. 정리 (테스트 후 필수 — 비용 방지)

```powershell
pynt deletedata   # (선택) S3 프레임 + DynamoDB 데이터 삭제. [Y/N] 확인 프롬프트가 뜸
pynt deletestack  # (필수) 스택 + 프레임 버킷 객체 + API Gateway UsagePlan 삭제
```

- `deletestack`은 프레임 버킷을 비운 뒤 스택을 삭제하고 `DELETE_COMPLETE`까지 대기합니다([build.py:269-291](build.py#L269-L291)).
- 삭제 후 AWS 콘솔에서 S3 버킷·객체가 실제로 비워졌는지 확인하는 것을 권장합니다.

---

## 5. 자주 겪는 문제

| 증상 | 원인 / 해결 |
| --- | --- |
| `NoCredentialsError: Unable to locate credentials` | `default` 프로필/자격증명 없음 (named 프로필만 설정한 경우) → 같은 터미널에서 `export AWS_PROFILE=video-analyzer`(bash) / `$env:AWS_PROFILE = "video-analyzer"`(PowerShell) 지정, 또는 `aws configure`로 `[default]` 생성 |
| `deploylambda`/`createstack` 버킷 생성 실패 | S3 버킷명이 전역에서 이미 사용 중 → `cfn-params.json`의 버킷명을 유일한 값으로 변경 |
| 리소스가 엉뚱한 리전에 생김 / API 조회 실패 | `aws configure` 리전과 버킷명 리전 suffix 불일치 → 일치시킨 뒤 재실행 |
| `pynt` 명령 없음 | 1-2 의존성 설치 누락 → `pip install` 재실행 (가상환경 활성화 확인) |
| `videocapture` 실행 시 `ModuleNotFoundError: No module named 'cv2'` | pynt 서브프로세스가 venv가 아닌 전역 파이썬을 실행해 생기던 문제로, build.py가 `sys.executable`을 쓰도록 고쳐 해결됨. 그래도 나면 venv에 직접 설치: `.\.venv\Scripts\python.exe -m pip install opencv-python numpy boto3 pytz` |
| `webuiserver` 포트 충돌 | 8080 사용 중 → 점유 중인 프로세스를 종료한 뒤 다시 실행 |
| `createstack`가 `ROLLBACK_COMPLETE`로 실패 | 먼저 원인 진단: `aws cloudformation describe-stack-events --stack-name video-analyzer-stack --region ap-northeast-2 --query "StackEvents[?ResourceStatus=='CREATE_FAILED'].[LogicalResourceId,ResourceStatusReason]" --output json`. 재시도 전 **반드시 삭제**: `aws cloudformation delete-stack --stack-name video-analyzer-stack --region ap-northeast-2` (frames 버킷이 아직 안 만들어진 상태라면 `pynt deletestack`은 `NoSuchBucket`으로 실패하니 CLI 사용). 삭제 완료 후 `pynt createstack` 재실행 |
| Kinesis `Internal error occurred (InternalFailure)` 또는 `SubscriptionRequiredException: ... needs a subscription for the service` | **AWS 계정 활성화 미완료** 가능성이 큼(신규 계정의 결제수단·신원확인 대기, 최대 ~24h). `aws kinesis list-streams --region ap-northeast-2`가 오류 없이 응답하면 사용 가능 → 위 절차로 롤백 스택 삭제 후 재생성 |
| Web UI에 데이터가 안 보임 | 캡처가 안 돌고 있거나 카메라에 객체가 없음 → 터미널 B에서 `videocapture` 실행 + 카메라 앞 확인 |
| 얼굴 비교 시 "최근 프레임이 너무 오래됐어요" (`NO_RECENT_FRAME`) | 최신 저장 프레임의 캡처 시각이 5분을 초과 → 터미널 B에서 `pynt videocapture[20]`를 켜 둔 채로 비교. 윈도우 조정은 `config/facecompare-params.json`의 `latest_frame_horizon_minutes`(분 단위) |
| 얼굴 비교 시 "저장된 프레임이 없어요" (`NO_LATEST_FRAME`) | 캡처된 프레임이 DynamoDB에 하나도 없음 → 먼저 `pynt videocapture[20]`로 프레임을 만든 뒤 몇 초 기다렸다 재시도 |

---

## 6. 참고

- 전체 `pynt` 명령어 표: [README.md](README.md) "6. 주요 build command 정리".
- 신분증 얼굴 비교 CLI(키오스크 신분증 인증): [runner.md](runner.md).
