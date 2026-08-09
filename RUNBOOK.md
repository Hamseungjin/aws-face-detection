# AWS Face Detection 프로젝트 실행 런북

이 문서는 현재 저장소의 다음 구성요소를 실제로 실행하고 검증하는 절차를 설명합니다.

- AWS 데이터 파이프라인: Kinesis → ImageProcessor → S3/DynamoDB → API Gateway
- 운영자 대시보드: `GET /`
- 시민용 키오스크: `GET /kiosk`
- 선택 구성: 로컬 카메라 producer, KPI Dashboard, EC2 배포

기본 리전과 리소스 이름은 현재 코드의 기본값인 다음 값을 사용합니다.

```text
AWS region:     ap-northeast-2
data stack:     video-analyzer-stack
Kinesis stream: FrameStream
DynamoDB table: EnrichedFrame
API stage:      development
```

> 시민 키오스크의 얼굴 촬영과 비교는 로컬 FastAPI만으로 완결되지 않습니다. Kinesis, Lambda,
> S3, DynamoDB, Rekognition, API Gateway로 구성된 AWS 데이터 스택이 먼저 실행 중이어야 합니다.

## 1. 실행 시나리오 선택

| 상황 | 실행할 절차 |
|---|---|
| AWS 스택이 이미 있고 로컬에서 UI만 실행 | 2장 → 4장 |
| AWS 스택을 처음 생성 | 2장 → 3장 → 4장 |
| 이미 배포된 Lambda에 현재 코드를 반영 | 3.4장 → 4장 |
| 로컬 카메라 producer도 실행 | 5장 |
| KPI Dashboard도 실행 | 6장 |
| UI와 KPI를 EC2에 배포 | 7장 |
| 테스트만 실행 | 9장 |

가장 일반적인 개발 실행은 다음 세 프로세스입니다.

```text
터미널 1: FastAPI web UI (:8080)
터미널 2: 선택 사항 - video_cap.py
터미널 3: 선택 사항 - KPI Dashboard (:8000)
```

키오스크 자체 촬영은 브라우저가 `/api/capture-frame`으로 전송하므로 `video_cap.py`를 별도로
실행할 필요가 없습니다.

## 2. 공통 사전 준비

### 2.1 필수 도구

- Python 3.11 또는 3.12 권장
- `pip`, `venv`
- AWS CLI v2
- AWS 자격증명 또는 AWS profile
- Node.js: JavaScript 구문/동작 테스트 시 필요
- 카메라: 브라우저 촬영 또는 `video_cap.py` 실행 시 필요

AWS 배포 도구와 로컬 producer까지 사용할 루트 가상환경을 만듭니다.

```bash
cd /path/to/aws-face-detection
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install pynt boto3 botocore opencv-python pytz pyyaml
```

Windows PowerShell에서는 활성화 명령만 다음과 같이 바꿉니다.

```powershell
.\.venv\Scripts\Activate.ps1
```

### 2.2 AWS profile과 리전 확인

```bash
export AWS_PROFILE=<profile-name>
export AWS_DEFAULT_REGION=ap-northeast-2
aws sts get-caller-identity
```

`get-caller-identity`가 실패하면 이후 배포, Kinesis 전송, `/api/config` 조회도 실패합니다.

이미 데이터 스택이 있는지 확인합니다.

```bash
aws cloudformation describe-stacks \
  --stack-name video-analyzer-stack \
  --region ap-northeast-2 \
  --query 'Stacks[0].StackStatus' \
  --output text
```

`CREATE_COMPLETE` 또는 `UPDATE_COMPLETE`이면 3장을 건너뛰고 4장으로 이동할 수 있습니다.

## 3. AWS 데이터 스택 배포

### 3.1 로컬 배포 설정 파일 준비

`config/`의 환경별 JSON은 의도적으로 Git에서 제외됩니다. 기존 배포 설정이 있으면 그것을
복원해서 사용하십시오. 처음 배포한다면 아래 예시의 placeholder를 실제 값으로 바꿉니다.
S3 bucket 이름은 계정 전체에서 고유해야 합니다. 자격증명이나 API key는 JSON에 넣지 않습니다.

`config/global-params.json`:

```json
{
  "StackName": "video-analyzer-stack"
}
```

`config/cfn-params.json`:

```json
{
  "SourceS3BucketParameter": "<globally-unique-lambda-code-bucket>",
  "ImageProcessorSourceS3KeyParameter": "lambda/imageprocessor.zip",
  "FrameFetcherSourceS3KeyParameter": "lambda/framefetcher.zip",
  "FaceCompareSourceS3KeyParameter": "lambda/facecompare.zip",
  "FrameS3BucketNameParameter": "<globally-unique-frame-bucket>",
  "KinesisStreamNameParameter": "FrameStream",
  "DDBTableNameParameter": "EnrichedFrame",
  "DDBGlobalSecondaryIndexNameParameter": "processed_year_month-processed_timestamp-index",
  "ApiGatewayRestApiNameParameter": "RtRekogRestApi",
  "ApiGatewayStageNameParameter": "development",
  "ApiGatewayUsagePlanNameParameter": "development-plan"
}
```

`config/imageprocessor-params.json`:

```json
{
  "timezone": "Asia/Seoul",
  "s3_bucket": "<same-as-FrameS3BucketNameParameter>",
  "s3_key_frames_root": "frames/",
  "ddb_table": "EnrichedFrame",
  "rekog_max_labels": 10,
  "rekog_min_conf": 50.0,
  "label_watch_list": [],
  "label_watch_min_conf": 90.0,
  "label_watch_phone_num": "",
  "label_watch_sns_topic_arn": "",
  "enable_detect_labels": false,
  "ddb_ttl_days": 30
}
```

`enable_detect_labels=false`여도 S3/DynamoDB 저장과 얼굴 비교는 동작합니다. 객체 라벨이 실제로
필요할 때만 활성화하십시오. 활성화하면 프레임마다 Rekognition DetectLabels 비용이 발생합니다.

`config/framefetcher-params.json`:

```json
{
  "timezone": "Asia/Seoul",
  "ddb_table": "EnrichedFrame",
  "ddb_gsi_name": "processed_year_month-processed_timestamp-index",
  "fetch_horizon_hrs": 1,
  "fetch_limit": 20,
  "s3_pre_signed_url_expiry": 300
}
```

FaceCompare 설정은 추적 중인 예제에서 복사합니다.

```bash
cp config/facecompare-params.example.json config/facecompare-params.json
```

기본 얼굴 유사도 threshold는 `90.0`, exact-frame 최신성 허용 시간은 5분입니다.

### 3.2 템플릿과 Lambda 패키지 확인

```bash
aws cloudformation validate-template \
  --template-body file://aws-infra/aws-infra-cfn.yaml \
  --region ap-northeast-2

pynt packagelambda
ls -lh build/imageprocessor.zip build/framefetcher.zip build/facecompare.zip
```

`packagelambda`는 각 `config/<function>-params.json`을 Lambda ZIP 안에 포함합니다. 파일이 없거나
JSON 키가 빠지면 패키징 또는 Lambda 실행이 실패합니다.

### 3.3 최초 배포

```bash
pynt deploylambda
pynt createstack
pynt stackstatus
pynt setlogretention
```

예상 결과:

- CloudFormation: `video-analyzer-stack`이 `CREATE_COMPLETE`
- Kinesis: `FrameStream`
- Lambda: `imageprocessor`, `framefetcher`, `facecompare`
- DynamoDB: `EnrichedFrame`
- API Gateway: `/enrichedframe`, `/face-compare`
- S3: 프레임 bucket과 Lambda code bucket

### 3.4 기존 스택에 현재 Lambda 코드 반영

Phase 2A의 `CaptureId`/`targetFrameId` 처리를 사용하려면 배포된 ImageProcessor와 FaceCompare가
현재 소스여야 합니다.

```bash
pynt packagelambda
pynt "updatelambda[imageprocessor,facecompare,framefetcher]"
```

`pynt updatelambda` 인자 처리가 사용하는 로컬 `pynt` 버전과 맞지 않으면 전체 갱신으로 실행합니다.

```bash
pynt packagelambda
pynt updatelambda
```

CloudFormation 리소스나 파라미터도 변경했다면 별도로 실행합니다.

```bash
pynt updatestack
```

현재 Phase 2A exact lookup은 기존 `EnrichedFrame.frame_id` partition key와 FaceCompare의
`dynamodb:GetItem` 권한을 사용하므로 새 테이블이나 GSI는 필요하지 않습니다.

## 4. 운영자 UI와 시민 키오스크 로컬 실행

현재 UI는 정적 `pynt webuiserver`가 아니라 FastAPI backend로 실행해야 로그인, 브라우저
카메라 전송, `/api/config`, SQLite 키오스크 거래 API를 모두 사용할 수 있습니다.

### 4.1 Backend 가상환경

```bash
cd web-ui/backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

### 4.2 로그인 secret 생성

`.env.example`의 예시 hash와 session secret을 그대로 사용하지 마십시오. 다음 명령은 비밀번호를
터미널 입력으로 받아 shell history에 평문을 남기지 않습니다.

```bash
python -c "import getpass; from auth import hash_password; print(hash_password(getpass.getpass('Password: ')))"
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

출력값을 `.env`의 다음 항목에 넣습니다.

```text
WEBUI_AUTH_USERNAME=<login-name>
WEBUI_AUTH_PASSWORD_HASH='<first-command-output>'
WEBUI_SESSION_SECRET='<second-command-output>'
WEBUI_SESSION_HTTPS_ONLY=false
AWS_DEFAULT_REGION=ap-northeast-2
KINESIS_STREAM=FrameStream
DATA_STACK=video-analyzer-stack
DATA_API_STAGE=development
```

로컬 SQLite 기본 경로는 `web-ui/backend/data/kiosk.sqlite3`입니다. 다른 경로를 쓰려면 `.env`에
`KIOSK_DB_PATH=/absolute/path/kiosk.sqlite3`를 추가합니다. SQLite에는 얼굴 또는 ID 이미지가
저장되지 않습니다.

### 4.3 FastAPI 시작

```bash
export AWS_PROFILE=<profile-name>
uvicorn app:app --host 127.0.0.1 --port 8080
```

브라우저 접속:

- 운영자 대시보드: <http://localhost:8080/>
- 시민 키오스크: <http://localhost:8080/kiosk>
- health check: <http://localhost:8080/healthz>

`localhost`는 브라우저 secure context로 취급되므로 HTTP에서도 `getUserMedia` 카메라를 사용할 수
있습니다. 다른 PC에서 `http://<LAN-IP>:8080`으로 접속하면 카메라 API가 차단될 수 있으므로
HTTPS를 구성해야 합니다.

### 4.4 최소 동작 확인

1. `/healthz`가 `{"status":"ok"}`를 반환하는지 확인합니다.
2. `/`와 `/kiosk`에서 생성한 계정으로 로그인합니다.
3. 운영자 화면에서 최근 프레임 조회가 동작하는지 확인합니다.
4. 키오스크에서 ID용 JPEG/PNG를 선택하고 얼굴을 촬영합니다.
5. 브라우저 Network 탭에서 `/api/capture-frame` 응답에 UUIDv4 `captureId`가 있는지 확인합니다.
6. 이어지는 `/face-compare` 요청의 `targetFrameId`가 같은 값인지 확인합니다.
7. `TARGET_FRAME_NOT_READY`가 발생하면 약 1.5초 간격으로 같은 ID를 재사용하는지 확인합니다.
8. 실제 결과의 `targetFrameId`가 최초 capture ID와 같은지 확인합니다.

DynamoDB에서도 해당 프레임을 직접 확인할 수 있습니다.

```bash
aws dynamodb get-item \
  --table-name EnrichedFrame \
  --key '{"frame_id":{"S":"<captureId>"}}' \
  --consistent-read \
  --region ap-northeast-2
```

항목이 아직 없으면 ImageProcessor 처리 중일 수 있습니다. 키오스크는 최대 약 15초 동안 같은
capture ID만 재시도하며 다른 최신 프레임으로 대체하지 않습니다.

## 5. 선택 사항: 로컬 카메라 producer

운영자 대시보드에 지속적으로 프레임을 공급하거나 legacy producer 호환성을 확인할 때 실행합니다.

프로젝트 루트 가상환경에서:

```bash
cd /path/to/aws-face-detection
source .venv/bin/activate
export AWS_PROFILE=<profile-name>
export AWS_DEFAULT_REGION=ap-northeast-2
pynt videocapture[60,300]
```

- `60`: 카메라 60프레임마다 1장 전송
- `300`: 300초 뒤 자동 종료
- 즉시 종료: OpenCV 미리보기 창에서 `q`
- 스트림: `FrameStream`

`client/video_cap.py`는 `CaptureId`를 보내지 않는 legacy producer입니다. ImageProcessor가 기존처럼
UUIDv4 `frame_id`를 생성하므로 계속 동작합니다. 시민 키오스크의 exact 비교에는 브라우저가
`/api/capture-frame`으로 보낸 캡처를 사용합니다.

IP 카메라는 다음처럼 실행합니다.

```bash
pynt 'videocaptureip[http://<camera-host>/video,60]'
```

## 6. 선택 사항: KPI Dashboard 로컬 실행

별도 터미널에서 실행합니다.

```bash
cd kpi-dashboard/backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export AWS_PROFILE=<profile-name>
export AWS_DEFAULT_REGION=ap-northeast-2
export KPI_LOOKBACK_HOURS=1
uvicorn app:app --host 127.0.0.1 --port 8000
```

확인 주소:

- Dashboard: <http://localhost:8000/>
- Health: <http://localhost:8000/healthz>
- Raw KPI: <http://localhost:8000/api/kpis>

CloudWatch Logs Insights는 스캔량에 따라 비용이 발생하므로 `KPI_LOOKBACK_HOURS=1`을 권장합니다.

## 7. 선택 사항: EC2 배포

EC2 배포는 AWS 데이터 스택과 별도의 `video-analyzer-ec2-stack`을 만듭니다. 세부 파라미터와
스케줄은 [docs/EC2_DEPLOYMENT.md](docs/EC2_DEPLOYMENT.md)를 함께 확인하십시오.

### 7.1 최초 배포 순서

1. `config/ec2-global-params.json`, `config/ec2-params.json`을 기존 환경에 맞게 준비합니다.
2. VPC와 PUBLIC subnet을 확인합니다.
3. 로그인 정보를 SSM Parameter Store에 넣습니다.
4. 앱 artifact를 업로드한 뒤 스택을 생성합니다.

```bash
pynt ec2vpcinfo
pynt setwebuiauth
pynt publishapps
pynt createec2stack
pynt ec2ip
```

현재 web UI systemd unit은 self-signed TLS로 uvicorn을 실행합니다. CloudFormation의 `WebUiUrl`
출력이 `http://`로 표시되더라도 브라우저 카메라 사용 시 실제 접속은 다음 HTTPS 주소를
사용하고 self-signed 인증서 경고를 명시적으로 승인해야 합니다.

```text
https://<WebUiElasticIp>:8080/
https://<WebUiElasticIp>:8080/kiosk
```

KPI Dashboard는 기본적으로 다음 주소입니다.

```text
http://<KpiElasticIp>:8000/
```

### 7.2 EC2 키오스크 SQLite 지속 경로

현재 bootstrap 기본값은 `/opt/webui/backend/data/kiosk.sqlite3`입니다. 앱 artifact를 다시
bootstrap하면 `/opt/webui`를 교체하므로 데모 거래 DB도 사라질 수 있습니다. 재배포 간 보존이
필요하면 webui 인스턴스에서 다음을 수행합니다.

```bash
sudo install -d -o webui -g webui /var/lib/webui
sudoedit /etc/webui.env
```

`/etc/webui.env`에 다음 줄을 추가한 뒤 재시작합니다.

```text
KIOSK_DB_PATH=/var/lib/webui/kiosk.sqlite3
```

```bash
sudo systemctl restart webui
sudo systemctl status webui
```

bootstrap을 다시 실행하면 `/etc/webui.env`도 다시 생성되므로 이 설정을 재확인해야 합니다.
이는 단일 EC2 호스트의 지속 경로일 뿐 다중 호스트 공유 DB는 아닙니다.

### 7.3 EC2 앱 코드 갱신

```bash
pynt publishapps webui
```

단순 `systemctl restart`는 S3의 새 artifact를 다운로드하지 않습니다. SSM Session Manager로
접속해 bootstrap을 다시 실행하거나 인스턴스를 교체해야 합니다. 자세한 명령은
[docs/EC2_DEPLOYMENT.md](docs/EC2_DEPLOYMENT.md)의 앱 코드 갱신 절차를 따릅니다.

## 8. 종료와 비용 정리

### 8.1 로컬 프로세스 종료

- FastAPI/KPI: 실행 터미널에서 `Ctrl+C`
- OpenCV producer: 미리보기에서 `q` 또는 지정한 자동 종료 시간 대기

로컬 프로세스를 꺼도 AWS 데이터 스택은 계속 존재하며 비용이 발생할 수 있습니다.

### 8.2 EC2만 중지 또는 삭제

운영 시간 밖에는 EC2 콘솔 또는 CLI로 인스턴스를 중지합니다. EBS와 Elastic IP는 stopped
상태에서도 과금됩니다.

EC2 계층 전체를 삭제할 때:

```bash
pynt deleteec2stack
```

이 명령은 데이터 스택의 Kinesis, Lambda, frame S3 bucket, DynamoDB를 삭제하지 않습니다.

### 8.3 데이터 스택 삭제

```bash
pynt deletestack
```

> 주의: `pynt deletestack`은 CloudFormation 삭제 전에 frame S3 bucket의 객체를 비웁니다.
> 저장 프레임과 관련 로그를 복구할 수 없으므로, 정확한 AWS account/profile/region과 stack을
> 확인하고 필요한 데이터를 백업한 경우에만 실행하십시오.

## 9. 테스트와 정적 검증

테스트용 가상환경에는 backend 의존성 외에 `pytest`, ImageProcessor의 `pytz`가 필요합니다.

```bash
cd /path/to/aws-face-detection
python3 -m venv /tmp/aws-face-detection-test
source /tmp/aws-face-detection-test/bin/activate
python -m pip install -r web-ui/backend/requirements.txt pytest pytz

python -m pytest -q
node --check web-ui/src/kiosk.js
node tests/kiosk_correlation_test.js web-ui/src/kiosk.js
python -m py_compile \
  web-ui/backend/app.py \
  web-ui/backend/config.py \
  web-ui/backend/kiosk_store.py \
  lambda/imageprocessor/imageprocessor.py \
  lambda/facecompare/facecompare.py \
  lambda/framefetcher/framefetcher.py
git diff --check
```

AWS 단위 테스트는 mock/fake를 사용하므로 실제 AWS 리소스에 쓰지 않습니다.

## 10. 트러블슈팅

| 증상 | 확인 및 조치 |
|---|---|
| 로그인 실패 | `.env`의 username/hash 확인. hash 생성 시 사용한 비밀번호로 로그인했는지 확인 |
| `/api/config` 503 | AWS profile, region, `DATA_STACK`, CloudFormation/API Gateway 조회 권한 확인 |
| `/api/capture-frame` 502 | `FrameStream` 존재 여부와 `kinesis:PutRecord` 권한 확인 |
| 카메라가 열리지 않음 | localhost 또는 HTTPS인지, 브라우저 카메라 권한과 다른 앱의 카메라 점유 여부 확인 |
| `TARGET_FRAME_NOT_READY` 반복 후 timeout | Kinesis event source mapping, ImageProcessor 로그, S3/DynamoDB write 권한과 Lambda 최신 코드 확인 |
| `INVALID_TARGET_FRAME_ID` | FastAPI와 kiosk JavaScript가 같은 최신 artifact인지 확인 후 다시 촬영 |
| `FRAME_METADATA_INVALID` / `INVALID_S3_OBJECT` | DynamoDB item의 `s3_bucket`/`s3_key`, S3 객체 존재와 Rekognition 접근 권한 확인 |
| `THROTTLED` | 호출 빈도를 낮추고 Rekognition/API Gateway quota 확인 |
| 프레임은 보이지만 exact 비교가 다른 코드처럼 동작 | 배포된 ImageProcessor/FaceCompare를 3.4장 절차로 갱신 |
| SQLite `database is locked` | 같은 DB를 비정상적인 다중 프로세스/호스트에서 공유하지 않는지 확인; 단일 FastAPI host 사용 |
| 포트 8080/8000 충돌 | uvicorn의 `--port`를 변경하고 접속 URL도 같은 포트로 변경 |

CloudWatch 확인 예시:

```bash
aws logs tail /aws/lambda/imageprocessor --since 10m --region ap-northeast-2
aws logs tail /aws/lambda/facecompare --since 10m --region ap-northeast-2
```

로그나 이슈에 `imageBase64`, `ImageBytes`, API key, 로그인 secret, 전체 신분증 payload를 붙이지
마십시오. `captureId`는 한 촬영을 상관시키는 메타데이터이며 사용자 신원이 아닙니다.

## 11. 관련 문서

- [README.md](README.md): 프로젝트 전체 구조와 AWS 리소스
- [docs/KIOSK_UI.md](docs/KIOSK_UI.md): 키오스크 상태/거래/정확 프레임 상관관계
- [docs/LOCAL_RUN.md](docs/LOCAL_RUN.md): 기존 로컬 구성요소별 상세 설명
- [docs/EC2_DEPLOYMENT.md](docs/EC2_DEPLOYMENT.md): EC2/IAM/Scheduler 세부 배포
