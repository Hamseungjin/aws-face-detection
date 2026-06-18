# 실행 가이드 (RUNBOOK)

메인 영상 분석 파이프라인을 **처음부터 끝까지** 실행하기 위한 가이드입니다.  
기본 흐름은 **사전 준비 → 서버리스 배포 → 실행 → 정리**이며, Windows PowerShell 기준으로 작성했습니다.

> 신분증 얼굴 비교 CLI(키오스크 인증)는 이 파이프라인과 별개입니다. 해당 기능은 [runner.md](runner.md)를 참고하세요.

---

## 0. 전체 흐름

### 기본 실행: 서버리스 백엔드 + 로컬 Web UI

```text
[1. 사전 준비]
AWS 자격증명 설정 → Python 의존성 설치 → config/*.json 값 교체

[2. 서버리스 배포]
pynt packagelambda → pynt deploylambda → pynt createstack → pynt stackstatus

[3. 로컬 실행]
터미널 A: cd web-ui/backend → uvicorn app:app --port 8080   # Web UI (FastAPI, 로그인)
터미널 B: pynt videocapture[20]                              # 또는 Web UI '촬영하기'
브라우저: http://localhost:8080 → 로그인 → 프레임 조회 / 촬영 / 얼굴 비교

[4. 정리]
pynt deletedata 선택 → pynt deletestack 필수
```

### 선택 실행: EC2 Web UI / KPI Dashboard

EC2로 Web UI와 KPI Dashboard를 띄우려면 서버리스 백엔드를 먼저 배포한 뒤, 별도 EC2 스택을 추가로 배포합니다.

```text
서버리스 배포 완료
→ pynt ec2vpcinfo
→ config/ec2-params.json 값 교체
→ pynt setwebuiauth          # Web UI 로그인 계정(아이디/비번 해시)을 SSM에 저장
→ pynt publishapps
→ pynt createec2stack
→ pynt ec2ip
브라우저: https://<WebUiElasticIp>:8080 → (자체서명 경고 수락) → 로그인 → '촬영하기'
```

---

## 1. 사전 준비

### 1-1. AWS 자격증명과 리전 설정

AWS 계정과 관리자 권한 IAM 사용자가 필요합니다. AWS CLI를 설치한 뒤 리전을 설정합니다. 권장 리전은 `ap-northeast-2`(서울)입니다.

```powershell
aws configure
# AWS Access Key ID     : <IAM 액세스 키>
# AWS Secret Access Key : <IAM 시크릿 키>
# Default region name   : ap-northeast-2
# Default output format : json
```

`pynt` 태스크는 boto3 기본 세션을 사용하므로, `aws configure`에 설정한 리전과 `config` 파일의 버킷명 리전 suffix를 일치시켜야 합니다.

named profile만 만든 경우에는 같은 터미널에서 아래 환경변수를 설정한 뒤 `pynt` 명령을 실행합니다.

```bash
export AWS_PROFILE=video-analyzer        # MINGW64 / bash
```

```powershell
$env:AWS_PROFILE = "video-analyzer"      # PowerShell
```

또는 `aws configure`를 `--profile` 없이 실행해 `[default]` 프로필을 만들면 환경변수 없이 동작합니다.

---

### 1-2. Python 의존성 설치

이 저장소는 `.venv`(Python 3.11)를 기준으로 사용합니다.

```powershell
.\.venv\Scripts\python.exe -m pip install boto3 opencv-python pynt pytz numpy
```

필요 패키지:

| 패키지 | 용도 |
| --- | --- |
| `boto3` | AWS SDK |
| `opencv-python` | 영상 캡처, `cv2` |
| `pynt` | 빌드 태스크 실행 |
| `pytz` | 타임존 처리 |
| `numpy` | 영상 처리 보조 |

> Web UI 백엔드(FastAPI)는 별도 의존성(`fastapi`, `uvicorn`, `itsdangerous`, `python-dotenv`, `boto3`)을 사용하며 `web-ui/backend/requirements.txt`로 설치합니다(3-1 참고).

---

### 1-3. 설정 파일 작성

`.gitignore`에 `config/*`가 포함되어 있어 설정 파일은 Git에 추적되지 않습니다. 실행 전 본인 AWS 환경에 맞게 값을 채워야 합니다.

특히 아래 두 가지는 반드시 바꿉니다.

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

아래 명령은 **터미널 1개에서 순서대로** 실행합니다. 각 단계는 직전 단계의 산출물에 의존합니다.

```powershell
pynt packagelambda   # lambda/* 코드를 build/*.zip 으로 패키징
pynt deploylambda    # build/*.zip 을 S3에 업로드. 버킷이 없으면 생성
pynt createstack     # Kinesis, Lambda, S3, DynamoDB, API Gateway, IAM 생성
pynt stackstatus     # 스택 상태 확인
```

성공 기준:

```text
CREATE_COMPLETE
```

의존 관계:

| 순서 | 명령 | 이유 |
| --- | --- | --- |
| 1 | `pynt packagelambda` | Lambda ZIP 파일 생성 |
| 2 | `pynt deploylambda` | 생성된 ZIP을 S3에 업로드 |
| 3 | `pynt createstack` | S3에 올라간 ZIP을 참조해 CloudFormation 배포 |
| 4 | `pynt stackstatus` | 배포 완료 여부 확인 |

---

## 3. 로컬 실행

로컬 Web UI는 **로그인 게이트가 있는 FastAPI 앱**입니다. 예전의 정적 서버(`pynt webuiserver`)가 아니라
백엔드를 실행해야 로그인/촬영이 동작합니다. `apiBaseUrl`/`apiKey`는 더 이상 정적 `apigw.js`가 아니라
로그인 후 백엔드 `/api/config`로 안전하게 전달됩니다.

### 3-1. Web UI 백엔드 실행 (FastAPI, 로그인 포함)

**터미널 A**에서 실행합니다. (서버리스 스택이 `CREATE_COMPLETE`여야 프레임 조회가 됩니다.)

```powershell
# 1) 의존성 설치 (최초 1회)
cd web-ui\backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2) 로그인 계정: .env 작성 (.gitignore 처리됨 — 절대 커밋하지 않음)
copy .env.example .env
.\.venv\Scripts\python.exe auth.py "내비밀번호"   # 출력된 PBKDF2 해시를 .env의 WEBUI_AUTH_PASSWORD_HASH 에 붙여넣기
#   .env 에 WEBUI_AUTH_USERNAME(아이디), WEBUI_SESSION_SECRET(랜덤 시크릿)도 채웁니다.
#   세션 시크릿 생성: python -c "import secrets; print(secrets.token_urlsafe(48))"

# 3) 실행 (AWS 자격증명 필요 — 프레임 조회/촬영 전송에 사용)
$env:AWS_PROFILE = "video-analyzer"
.\.venv\Scripts\uvicorn.exe app:app --host 127.0.0.1 --port 8080
```

> `localhost`는 보안 컨텍스트라 HTTPS 없이도 브라우저 카메라('촬영하기')를 쓸 수 있습니다.

---

### 3-2. 브라우저 접속 + 로그인

```text
http://localhost:8080
```

3-1에서 정한 아이디/비밀번호로 로그인합니다. **로그인하지 않으면 페이지와 촬영 API에 접근할 수 없습니다.**

---

### 3-3. 영상 캡처 (두 가지 방법)

프레임을 Kinesis `FrameStream`으로 보내는 방법은 두 가지이며, 둘 다 같은 파이프라인을 거칩니다.

**(a) 로컬 캡처 클라이언트** — **터미널 B**:

```powershell
pynt videocapture[20]
```

`[20]`은 20프레임당 1장을 캡처한다는 뜻입니다. Rekognition이 객체를 감지하려면 카메라 앞에 사람/물체가 보여야 합니다.
IP 카메라 또는 스마트폰 MJPEG 스트림을 사용할 경우:

```powershell
pynt videocaptureip["http://192.168.0.2/video",20]
```

**(b) Web UI '촬영하기'** — 로그인 후 화면에서 `frameInterval`(예: 20)·`durationSeconds`(예: 300)를 입력하고
**촬영 시작**(브라우저 카메라 권한 허용) → **촬영 중지**. 브라우저가 캡처한 JPEG를 백엔드가 인스턴스/프로필
권한으로 Kinesis에 전송하므로 **AWS 키가 브라우저에 노출되지 않습니다.** 전송 성공/실패 수, 남은 시간, 마지막
전송 시각이 화면에 표시됩니다.

---

### 3-4. 얼굴 비교 기능 사용 조건

Web UI의 **“최근 프레임과 얼굴 비교”** 기능을 쓰려면 영상 캡처(3-3의 (a) 또는 (b))가 계속 실행 중이어야 합니다.

얼굴 비교는 DynamoDB에 저장된 **가장 최신 프레임**과 업로드한 얼굴을 비교합니다. 최신 프레임의 캡처 시각이 `latest_frame_horizon_minutes` 값을 넘으면 비교가 차단됩니다.

기본 설정에서는 5분이 지나면 다음 오류가 표시됩니다.

```text
최근 프레임이 너무 오래됐어요 (캡처가 실행 중인지 확인).
```

상황별 원인:

| 화면 메시지 / 코드 | 원인 | 해결 |
| --- | --- | --- |
| `NO_RECENT_FRAME` | 마지막 프레임이 너무 오래됨 | 촬영(3-3)을 켠 상태에서 다시 비교 |
| `NO_LATEST_FRAME` | 저장된 프레임이 아직 없음 | 촬영을 먼저 실행하고 몇 초 뒤 재시도 |

---

## 4. 정리

테스트 후에는 비용 방지를 위해 스택을 삭제합니다.

```powershell
pynt deletedata   # 선택: S3 프레임 + DynamoDB 데이터 삭제. 확인 프롬프트 있음
pynt deletestack  # 필수: 스택 + 프레임 버킷 객체 + API Gateway UsagePlan 삭제
```

`deletestack`은 프레임 버킷을 비운 뒤 스택을 삭제하고 `DELETE_COMPLETE`까지 대기합니다. 삭제 후 AWS 콘솔에서 S3 버킷과 객체가 정리됐는지 확인하는 것을 권장합니다.

> EC2 스택을 배포했다면 `pynt deleteec2stack`도 함께 실행해야 EC2/EIP/EBS 과금이 멈춥니다(5-3 참고).

---

## 5. 선택: EC2 배포

기본 1~4장은 **서버리스 백엔드 + 로컬 Web UI** 실행입니다.  
Web UI와 KPI Dashboard를 EC2에서 띄우려면 별도 EC2 스택을 추가로 배포합니다.

EC2 스택 구성:

- Web UI EC2 (FastAPI/uvicorn, **로그인 + 브라우저 촬영**, HTTPS)
- KPI Dashboard EC2
- Elastic IP 2개
- 스케줄러 4개
- 보안 그룹
- IAM Role (webui는 `kinesis:PutRecord` + SSM 읽기 포함)

참고 문서:

- 상세 EC2 런북: `docs/EC2_DEPLOYMENT.md`
- 로컬 실행: `docs/LOCAL_RUN.md`

---

### 5-1. EC2 배포 순서

먼저 서버리스 백엔드가 `CREATE_COMPLETE` 상태여야 합니다.

```powershell
# 1단계: 서버리스 백엔드
pynt packagelambda; pynt deploylambda; pynt createstack; pynt stackstatus

# 2단계: EC2
pynt ec2vpcinfo
# config/ec2-params.json 의 VpcIdParameter / SubnetIdParameter 교체
pynt setwebuiauth          # Web UI 로그인 계정(아이디/비밀번호)을 SSM Parameter Store에 저장
pynt publishapps           # web-ui(FastAPI 백엔드 포함) + bootstrap + systemd 유닛 업로드
pynt createec2stack
pynt ec2ip
```

> `setwebuiauth`는 비밀번호를 입력받아 **PBKDF2 해시**와 세션 시크릿을 SSM **SecureString**에 저장합니다
> (평문·해시 모두 git에 커밋되지 않음). webui 인스턴스가 부팅 시 이 값을 읽어 로그인에 사용하므로
> **반드시 `createec2stack` 전에** 실행해야 합니다.

접속 주소:

```text
Web UI : https://<WebUiElasticIp>:8080   (HTTPS, 자체서명 → 브라우저 경고 1회 수락)
KPI    : http://<KpiElasticIp>:8000
```

webui 접속 후 **로그인**(setwebuiauth에서 정한 계정) → 프레임 조회 + **'촬영하기'**(브라우저 카메라로 프레임을
Kinesis에 전송). 기동 확인은 `curl -k https://<WebUiElasticIp>:8080/healthz` → `{"status":"ok"}`.

---

### 5-2. EC2 배포 시 주의할 점

| 항목 | 설명 |
| --- | --- |
| 서버리스 선행 배포 | `publishapps`는 S3에 업로드만 하며 버킷을 만들지 않습니다. 코드 버킷은 `deploylambda`가 생성하므로 서버리스 배포가 먼저 필요합니다. |
| VPC/Subnet 값 교체 | `config/ec2-params.json`의 `vpc-REPLACE_ME`, `subnet-REPLACE_ME`를 실제 값으로 바꿔야 합니다. |
| 로그인 계정(필수 선행) | `pynt setwebuiauth`로 SSM에 계정을 저장해야 webui 로그인이 됩니다. `createec2stack` 전에 실행. 계정 변경 시 재실행 후 인스턴스 재부팅(또는 `systemctl restart webui`). |
| HTTPS(자체서명) | webui는 브라우저 카메라(getUserMedia)가 보안 컨텍스트를 요구하므로 HTTPS로 뜹니다. 자체서명이라 첫 접속 시 브라우저 경고를 한 번 수락해야 합니다. |
| 촬영하기 권한 | webui 인스턴스 IAM Role에 `kinesis:PutRecord`가 부여됩니다(템플릿 포함). AWS 키는 브라우저에 전달되지 않습니다. |
| 상태 확인 | `pynt stackstatus`는 서버리스 스택만 확인합니다. EC2 스택은 AWS 콘솔 또는 `aws cloudformation describe-stacks`로 확인합니다. webui 기동은 `curl -k https://<ip>:8080/healthz`. |
| 비용 | EC2 실행 시간, EBS, EIP 비용이 발생합니다. EIP는 정지 중에도 과금될 수 있습니다. |

---

### 5-3. 수정 배포와 삭제

```powershell
# 서버리스 갱신
pynt packagelambda; pynt deploylambda; pynt updatestack

# EC2 앱 갱신 (web-ui/backend 또는 KPI 코드 변경 등)
pynt publishapps            # 새 artifact를 S3에 업로드
pynt updateec2stack         # 또는 깨끗한 재배포: pynt deleteec2stack 후 pynt createec2stack
#   주의: EC2 UserData는 첫 부팅에만 실행됩니다. 기존 인스턴스에 새 코드를 반영하려면
#   인스턴스 교체(재생성)하거나, SSM 접속 후 /opt/app/bootstrap.sh 를 다시 실행해야 합니다.

# 로그인 계정 변경
pynt setwebuiauth           # SSM 갱신 후 인스턴스 재부팅(또는 systemctl restart webui)

# EC2만 삭제
pynt deleteec2stack

# EC2 중지
aws ec2 stop-instances \
  --region ap-northeast-2 --profile video-analyzer \
  --instance-ids i-064aa4033fd428313 i-02115c204ecf26406

# EC2 재시작
aws ec2 start-instances --region ap-northeast-2 --profile video-analyzer --instance-ids i-064aa4033fd428313 i-02115c204ecf26406
  

```



EC2만 삭제하면 서버리스 스택과 저장 데이터는 유지됩니다. 문제 진단은 SSM Session Manager로 접속해
`systemctl status webui`, `journalctl -u webui -n 100`, `/var/log/webui-bootstrap.log` 를 확인합니다.

---

## 6. 참고

- 전체 `pynt` 명령어 표: [README.md](README.md) “6. 주요 build command 정리”
- 신분증 얼굴 비교 CLI: [runner.md](runner.md)
- EC2 상세 배포: [docs/EC2_DEPLOYMENT.md](docs/EC2_DEPLOYMENT.md)
- 로컬 실행: [docs/LOCAL_RUN.md](docs/LOCAL_RUN.md)
