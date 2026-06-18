# 실행 가이드 (RUNBOOK) — Linux/bash 기준

메인 영상 분석 파이프라인을 **처음부터 끝까지** 실행하기 위한 가이드입니다.  
실행 흐름은 **사전 준비 → 서버리스 백엔드 배포 → 실행 → 정리** 순서입니다.

> 신분증 얼굴 비교 CLI(키오스크 인증)는 이 파이프라인과 별개입니다. 해당 기능은 [runner.md](runner.md)를 참고하세요.

---

## 0. 전체 실행 순서

### 기본 실행: 서버리스 백엔드 + 로컬 Web UI

```text
[1. 사전 준비]
AWS 자격증명 설정 → Python 의존성 설치 → config/*.json 값 교체

[2. 서버리스 백엔드 배포]
pynt packagelambda → pynt deploylambda → pynt createstack → pynt stackstatus

[3. 로컬 실행]
터미널 A: web-ui/backend FastAPI 실행
터미널 B: 영상 캡처 실행 또는 Web UI에서 '촬영하기'
브라우저: http://localhost:8080 접속 → 로그인 → 프레임 조회 / 촬영 / 얼굴 비교

[4. 정리]
EC2를 배포했다면 EC2 스택 삭제 → 데이터 삭제 선택 → 서버리스 스택 삭제
```

### 선택 실행: EC2 Web UI / KPI Dashboard

EC2에서 Web UI와 KPI Dashboard를 띄우려면 **서버리스 백엔드를 먼저 배포**한 뒤 EC2 스택을 추가로 배포합니다.

```text
서버리스 백엔드 배포 완료
→ pynt ec2vpcinfo
→ config/ec2-params.json 값 교체
→ pynt setwebuiauth
→ pynt publishapps
→ pynt createec2stack
→ pynt ec2ip
→ 브라우저에서 Web UI / KPI 접속
```

---

## 1. 사전 준비

### 1-1. AWS 자격증명과 리전 설정

AWS 계정과 관리자 권한 IAM 사용자가 필요합니다. AWS CLI를 설치한 뒤 기본 리전을 설정합니다. 권장 리전은 `ap-northeast-2`(서울)입니다.

```bash
aws configure
# AWS Access Key ID     : <IAM 액세스 키>
# AWS Secret Access Key : <IAM 시크릿 키>
# Default region name   : ap-northeast-2
# Default output format : json
```

`pynt` 태스크는 boto3 기본 세션을 사용하므로, `aws configure`에 설정한 리전과 `config` 파일의 버킷명 리전 suffix를 일치시켜야 합니다.

named profile만 만든 경우에는 같은 터미널에서 아래 환경변수를 설정한 뒤 `pynt` 명령을 실행합니다.

```bash
export AWS_PROFILE=video-analyzer
```

또는 `aws configure`를 `--profile` 없이 실행해 `[default]` 프로필을 만들면 환경변수 없이 동작합니다.

---

### 1-2. Python 의존성 설치

이 저장소는 Python 3.11 가상환경을 기준으로 사용합니다.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install boto3 opencv-python pynt pytz numpy
```

필요 패키지:

| 패키지 | 용도 |
| --- | --- |
| `boto3` | AWS SDK |
| `opencv-python` | 영상 캡처, `cv2` |
| `pynt` | 빌드 태스크 실행 |
| `pytz` | 타임존 처리 |
| `numpy` | 영상 처리 보조 |

> Web UI 백엔드(FastAPI)는 별도 의존성을 사용합니다. 자세한 설치는 [3-1. Web UI 백엔드 실행](#3-1-web-ui-백엔드-실행)을 참고하세요.

---

### 1-3. 설정 파일 작성

`.gitignore`에 `config/*`가 포함되어 있으므로 설정 파일은 Git에 추적되지 않습니다. 실행 전 본인 AWS 환경에 맞게 값을 채워야 합니다.

반드시 바꿔야 하는 값:

- 예시 계정 ID `115019372648` → 본인 AWS 계정 ID
- S3 버킷명 → 전역에서 유일한 이름

| 파일 | 키 | 설명 |
| --- | --- | --- |
| `config/cfn-params.json` | `SourceS3BucketParameter` | Lambda ZIP 업로드용 S3 버킷 |
|  | `FrameS3BucketNameParameter` | 캡처 프레임 저장 S3 버킷 |
|  | `ApiGatewayRestApiNameParameter` | API Gateway 이름 |
|  | `ApiGatewayStageNameParameter` | API Gateway 스테이지 |
| `config/global-params.json` | `StackName` | CloudFormation 스택 이름. 기본값: `video-analyzer-stack` |
| `config/imageprocessor-params.json` | `s3_bucket` | `FrameS3BucketNameParameter`와 동일해야 함 |
|  | `label_watch_list` | 알림 대상 라벨. 예: `Person, Dog, Cat, Bag, Backpack, Toy` |
|  | `label_watch_phone_num` | SMS 수신 번호. 비우면 SMS 비활성화 |
|  | `timezone` | 기본값: `Asia/Seoul` |
| `config/framefetcher-params.json` | `fetch_horizon_hrs` | Web UI 조회 시간 범위 |
|  | `fetch_limit` | Web UI 조회 개수 제한 |
| `config/facecompare-params.json` | `latest_frame_horizon_minutes` | 얼굴 비교에 사용할 최신 프레임 허용 시간. 기본값: `5`분 |

`SourceS3BucketParameter`와 `FrameS3BucketNameParameter`는 `deploylambda`, `createstack`, `deletestack`에서 직접 사용됩니다. 버킷명이 이미 사용 중이면 배포가 실패합니다.

---

## 2. 서버리스 백엔드 배포

아래 명령은 **터미널 1개에서 순서대로** 실행합니다. 각 단계는 직전 단계의 산출물에 의존하므로, 이전 명령이 성공한 뒤 다음 명령을 실행해야 합니다.

```bash
pynt packagelambda
pynt deploylambda
pynt createstack
pynt stackstatus
```

한 번에 실행하려면 실패 시 다음 단계가 실행되지 않도록 `&&`를 사용합니다.

```bash
pynt packagelambda \
  && pynt deploylambda \
  && pynt createstack \
  && pynt stackstatus
```

성공 기준:

```text
CREATE_COMPLETE
```

의존 관계:

| 순서 | 명령 | 이유 |
| --- | --- | --- |
| 1 | `pynt packagelambda` | Lambda ZIP 파일 생성 |
| 2 | `pynt deploylambda` | 생성된 ZIP을 S3에 업로드. 버킷이 없으면 생성 |
| 3 | `pynt createstack` | S3에 올라간 ZIP을 참조해 CloudFormation 배포 |
| 4 | `pynt stackstatus` | 배포 완료 여부 확인 |

---

## 3. 로컬 실행

로컬 Web UI는 **로그인 게이트가 있는 FastAPI 앱**입니다. 예전의 정적 서버(`pynt webuiserver`)가 아니라 백엔드를 실행해야 로그인과 촬영 기능이 동작합니다.

### 3-1. Web UI 백엔드 실행

**터미널 A**에서 실행합니다. 서버리스 스택이 `CREATE_COMPLETE` 상태여야 프레임 조회가 됩니다.

```bash
cd web-ui/backend
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

로그인 설정 파일을 작성합니다.

```bash
cp .env.example .env
python auth.py "내비밀번호"
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

`.env`에 아래 값을 채웁니다.

```dotenv
WEBUI_AUTH_USERNAME=<아이디>
WEBUI_AUTH_PASSWORD_HASH=<auth.py 출력 PBKDF2 해시>
WEBUI_SESSION_SECRET=<secrets.token_urlsafe(48) 출력값>
```

AWS profile을 사용하는 경우 같은 터미널에서 설정합니다.

```bash
export AWS_PROFILE=video-analyzer
```

Web UI 백엔드를 실행합니다.

```bash
python -m uvicorn app:app --host 127.0.0.1 --port 8080
```

> `localhost`는 보안 컨텍스트라 HTTPS 없이도 브라우저 카메라(`촬영하기`)를 사용할 수 있습니다.

---

### 3-2. 브라우저 접속과 로그인

브라우저에서 아래 주소로 접속합니다.

```text
http://localhost:8080
```

3-1에서 설정한 아이디와 비밀번호로 로그인합니다. 로그인하지 않으면 페이지와 촬영 API에 접근할 수 없습니다.

---

### 3-3. 영상 캡처

프레임을 Kinesis `FrameStream`으로 보내는 방법은 두 가지입니다. 둘 중 하나만 실행해도 같은 파이프라인을 거칩니다.

#### 방법 A: 로컬 캡처 클라이언트

**터미널 B**에서 실행합니다.

```bash
source .venv/bin/activate
export AWS_PROFILE=video-analyzer
pynt videocapture[20]
```

`[20]`은 20프레임당 1장을 캡처한다는 뜻입니다. Rekognition이 객체를 감지하려면 카메라 앞에 사람 또는 물체가 보여야 합니다.

IP 카메라 또는 스마트폰 MJPEG 스트림을 사용할 경우:

```bash
source .venv/bin/activate
export AWS_PROFILE=video-analyzer
pynt videocaptureip["http://192.168.0.2/video",20]
```

#### 방법 B: Web UI `촬영하기`

Web UI에 로그인한 뒤 `frameInterval`과 `durationSeconds`를 입력하고 **촬영 시작**을 누릅니다. 브라우저 카메라 권한을 허용하면 브라우저가 캡처한 JPEG를 백엔드가 Kinesis로 전송합니다.

AWS 키는 브라우저에 노출되지 않습니다. 전송 성공/실패 수, 남은 시간, 마지막 전송 시각은 화면에 표시됩니다.

---

### 3-4. 얼굴 비교 기능 사용 조건

Web UI의 **최근 프레임과 얼굴 비교** 기능을 쓰려면 영상 캡처가 실행 중이거나 최근 프레임이 저장되어 있어야 합니다.

얼굴 비교는 DynamoDB에 저장된 **가장 최신 프레임**과 업로드한 얼굴을 비교합니다. 최신 프레임의 캡처 시각이 `latest_frame_horizon_minutes` 값을 넘으면 비교가 차단됩니다.

기본 설정에서는 5분이 지나면 다음 오류가 표시됩니다.

```text
최근 프레임이 너무 오래됐어요 (캡처가 실행 중인지 확인).
```

상황별 원인:

| 화면 메시지 / 코드 | 원인 | 해결 |
| --- | --- | --- |
| `NO_RECENT_FRAME` | 마지막 프레임이 너무 오래됨 | 촬영을 켠 상태에서 다시 비교 |
| `NO_LATEST_FRAME` | 저장된 프레임이 아직 없음 | 촬영을 먼저 실행하고 몇 초 뒤 재시도 |

---

## 4. 선택: EC2 Web UI / KPI Dashboard 배포

EC2 배포는 선택 사항입니다. 로컬 Web UI만 사용할 경우 이 장은 건너뛰어도 됩니다.

EC2에서 Web UI와 KPI Dashboard를 띄우려면 **서버리스 백엔드가 먼저 `CREATE_COMPLETE` 상태**여야 합니다.

EC2 스택 구성:

| 구성 요소 | 설명 |
| --- | --- |
| Web UI EC2 | FastAPI/uvicorn, 로그인, 브라우저 촬영, HTTPS |
| KPI Dashboard EC2 | KPI Dashboard 실행 |
| Elastic IP 2개 | Web UI / KPI 접속용 |
| 스케줄러 4개 | 자동 실행 작업 |
| 보안 그룹 | 인바운드/아웃바운드 제어 |
| IAM Role | webui 인스턴스의 `kinesis:PutRecord`, SSM 읽기 권한 포함 |

---

### 4-1. EC2 배포 순서

아래 순서대로 실행합니다.

```bash
# 1) 서버리스 백엔드 배포 상태 확인
pynt stackstatus
```

`CREATE_COMPLETE`가 아니면 먼저 서버리스 백엔드를 배포합니다.

```bash
pynt packagelambda \
  && pynt deploylambda \
  && pynt createstack \
  && pynt stackstatus
```

서버리스 백엔드 배포가 완료되면 EC2 배포를 진행합니다.

```bash
# 2) VPC/Subnet 확인
pynt ec2vpcinfo

# 3) config/ec2-params.json 수정
# VpcIdParameter / SubnetIdParameter 값을 실제 VPC/Subnet 값으로 교체

# 4) Web UI 로그인 계정을 SSM Parameter Store에 저장
pynt setwebuiauth

# 5) Web UI / KPI 앱 artifact 업로드
pynt publishapps

# 6) EC2 스택 생성
pynt createec2stack

# 7) 접속 IP 확인
pynt ec2ip
```

한 번에 실행할 수 있는 구간은 아래처럼 묶을 수 있습니다. 단, `ec2vpcinfo` 실행 후에는 반드시 `config/ec2-params.json`을 먼저 수정해야 합니다.

```bash
pynt setwebuiauth \
  && pynt publishapps \
  && pynt createec2stack \
  && pynt ec2ip
```

---

### 4-2. EC2 접속 주소

`pynt ec2ip`로 확인한 IP를 사용합니다.

```text
Web UI : https://<WebUiElasticIp>:8080
KPI    : http://<KpiElasticIp>:8000
```

Web UI는 자체서명 인증서를 사용하므로 첫 접속 시 브라우저 경고를 한 번 수락해야 합니다.

기동 확인:

```bash
curl -k https://<WebUiElasticIp>:8080/healthz
# {"status":"ok"}
```

---

### 4-3. EC2 배포 시 주의할 점

| 항목 | 설명 |
| --- | --- |
| 서버리스 선행 배포 | `publishapps`는 S3에 업로드만 하며 버킷을 만들지 않습니다. 코드 버킷은 `deploylambda`가 생성하므로 서버리스 배포가 먼저 필요합니다. |
| VPC/Subnet 값 교체 | `config/ec2-params.json`의 `vpc-REPLACE_ME`, `subnet-REPLACE_ME`를 실제 값으로 바꿔야 합니다. |
| 로그인 계정 저장 | `pynt setwebuiauth`는 `createec2stack` 전에 실행해야 합니다. 계정 변경 시 재실행 후 인스턴스를 재부팅하거나 `systemctl restart webui`를 실행합니다. |
| HTTPS 자체서명 | 브라우저 카메라 사용을 위해 Web UI는 HTTPS로 실행됩니다. 자체서명 인증서라 첫 접속 시 경고가 표시됩니다. |
| 촬영 권한 | webui 인스턴스 IAM Role에 `kinesis:PutRecord` 권한이 부여됩니다. AWS 키는 브라우저에 전달되지 않습니다. |
| 상태 확인 | 서버리스 스택은 `pynt stackstatus`, EC2 스택은 AWS 콘솔 또는 `aws cloudformation describe-stacks`로 확인합니다. |
| 비용 | EC2 실행 시간, EBS, EIP 비용이 발생합니다. EIP는 정지 중에도 과금될 수 있습니다. |

---

## 5. 수정 배포

### 5-1. 서버리스 백엔드 갱신

Lambda 코드 또는 서버리스 리소스를 수정한 경우 아래 순서로 실행합니다.

```bash
pynt packagelambda \
  && pynt deploylambda \
  && pynt updatestack \
  && pynt stackstatus
```

---

### 5-2. EC2 앱 갱신

`web-ui/backend` 또는 KPI 코드가 바뀐 경우 앱 artifact를 다시 업로드한 뒤 EC2 스택을 갱신합니다.

```bash
pynt publishapps \
  && pynt updateec2stack
```

주의: EC2 UserData는 첫 부팅에만 실행됩니다. 기존 인스턴스에 새 코드를 확실히 반영하려면 인스턴스를 교체하거나, SSM Session Manager로 접속해 `/opt/app/bootstrap.sh`를 다시 실행해야 합니다.

로그인 계정을 변경한 경우:

```bash
pynt setwebuiauth
```

그 다음 webui 인스턴스를 재부팅하거나 인스턴스 내부에서 아래 명령을 실행합니다.

```bash
sudo systemctl restart webui
```

---

## 6. 정리와 삭제

테스트 후에는 비용 방지를 위해 사용하지 않는 리소스를 삭제합니다.

### 6-1. EC2 스택을 배포한 경우

EC2를 배포했다면 먼저 EC2 스택부터 삭제합니다.

```bash
pynt deleteec2stack
```

EC2를 삭제하지 않고 일시 중지하려면 실제 인스턴스 ID로 바꿔 실행합니다.

```bash
aws ec2 stop-instances \
  --region ap-northeast-2 \
  --profile video-analyzer \
  --instance-ids <WebUiInstanceId> <KpiInstanceId>
```

다시 시작하려면:

```bash
aws ec2 start-instances \
  --region ap-northeast-2 \
  --profile video-analyzer \
  --instance-ids <WebUiInstanceId> <KpiInstanceId>
```

---

### 6-2. 서버리스 데이터와 스택 삭제

저장된 프레임과 DynamoDB 데이터를 삭제하려면 먼저 `deletedata`를 실행합니다. 데이터 보존이 필요하면 건너뛰어도 됩니다.

```bash
pynt deletedata
```

서버리스 스택은 반드시 삭제합니다.

```bash
pynt deletestack
```

`deletestack`은 프레임 버킷을 비운 뒤 스택을 삭제하고 `DELETE_COMPLETE`까지 대기합니다. 삭제 후 AWS 콘솔에서 S3 버킷과 객체가 정리됐는지 확인하는 것을 권장합니다.

---

## 7. 문제 진단

EC2 Web UI 문제는 SSM Session Manager로 인스턴스에 접속해 아래 명령으로 확인합니다.

```bash
sudo systemctl status webui
sudo journalctl -u webui -n 100 --no-pager
sudo tail -n 100 /var/log/webui-bootstrap.log
```

서버리스 스택 상태 확인:

```bash
aws cloudformation describe-stacks \
  --region ap-northeast-2 \
  --profile video-analyzer \
  --stack-name video-analyzer-stack
```

---

## 8. 참고 문서

- 전체 `pynt` 명령어 표: [README.md](README.md) “6. 주요 build command 정리”
- 신분증 얼굴 비교 CLI: [runner.md](runner.md)
- EC2 상세 배포: [docs/EC2_DEPLOYMENT.md](docs/EC2_DEPLOYMENT.md)
- 로컬 실행: [docs/LOCAL_RUN.md](docs/LOCAL_RUN.md)
