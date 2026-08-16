# EC2 배포 가이드 — ApplicationInstance (greenfield)

개발/데모용으로, 서버리스 **데이터 스택**은 그대로 두고, **애플리케이션 호스트 1대**를
별도 CloudFormation 스택으로 배포합니다. 리전: **ap-northeast-2 (서울)**.

> **2026-08-11 greenfield pivot:** `video-analyzer-ec2-stack`이 존재하지 않아
> WebUi+Kpi+Application 3대 생성을 거부하고, 템플릿 기본값을 **Application only** 로 고정했습니다.

---

## 1. 현재 권장 아키텍처

| 스택 | 내용 |
|---|---|
| `video-analyzer-stack` | 데이터 플레인 (Kinesis · Lambda · DynamoDB · S3 frames · API Gateway · Rekognition) |
| `video-analyzer-ec2-stack` | **ApplicationInstance 1대** + EIP 1 + 전용 SQLite EBS 1 |

```text
Browser / Kiosk
      |
   HTTPS :443  (Nginx, self-signed — 브라우저 경고 정상)
      |
  Application EC2  (Name: video-analyzer-app, default t3.small)
      ├── FastAPI  127.0.0.1:8080   → /  /kiosk  /api/*  /src/*  /healthz
      ├── KPI      127.0.0.1:8000   → /dashboard/
      ├── systemd  kiosk-fastapi, kpi-dashboard, nginx, application-tls-refresh
      └── SQLite   /data/kiosk/kiosk.db  (전용 encrypted EBS, Snapshot 정책)
```

**데모 앱 경로에 code-server `/proxy/8080/` 를 사용하지 않습니다.**

스택 기본 생성 수량 (정적 증명 대상):

| 리소스 | 개수 |
|---|---|
| ApplicationInstance | **1** |
| ApplicationElasticIp | **1** |
| ApplicationDataVolume | **1** |
| WebUiInstance | **0** |
| KpiInstance | **0** |

---

## 2. 사전 조건

1. **`video-analyzer-stack` 이 CREATE_COMPLETE / UPDATE_COMPLETE**
   (FrameStream, EnrichedFrame, imageprocessor, framefetcher, facecompare, frames 버킷, API stage)
2. **아티팩트 버킷** (보통 lambda/code 버킷) 존재
3. **관리자 권한** CFN/EC2/IAM/Scheduler 생성 (개발 kiosk 역할은 VPC Describe 불가한 경우가 많음)
4. 리전 **ap-northeast-2**

데이터 스택 복구 (없을 때):

```bash
export AWS_DEFAULT_REGION=ap-northeast-2
pynt packagelambda
pynt deploylambda
pynt setlogretention   # optional
pynt createstack
pynt stackstatus
```

frames 버킷 물리 이름:

```bash
aws cloudformation describe-stack-resource \
  --stack-name video-analyzer-stack \
  --logical-resource-id FrameS3Bucket \
  --query 'StackResourceDetail.PhysicalResourceId' --output text
```

---

## 3. 파라미터 파일

추적 파일은 예시만 둡니다 (VPC/subnet ID를 리포에 박지 않음).

```bash
cp config/ec2-global-params.example.json config/ec2-global-params.json
cp config/ec2-params.example.json config/ec2-params.json
# 편집: VpcIdParameter, SubnetIdParameter, FrameS3BucketNameParameter,
#       AppArtifactS3BucketParameter, AllowedIngressCidrParameter
```

| 키 | 설명 |
|---|---|
| `VpcIdParameter` / `SubnetIdParameter` | 퍼블릭 서브넷 |
| `AppArtifactS3BucketParameter` | `apps/` 업로드 버킷 |
| `FrameS3BucketNameParameter` | 데이터 스택 FrameS3Bucket 물리 이름 (**필수**) |
| `ApplicationInstanceTypeParameter` | 기본 `t3.small` |
| `EnableSchedulerParameter` | 기본 `true` (ApplicationInstance만 start/stop) |

VPC 조회 (admin):

```bash
pynt ec2vpcinfo
```

kiosk/dev 역할로 DescribeVpcs 가 막히면 admin에서 조회하거나, 참고용으로 **현재 개발 EC2** 의
서브넷을 IMDS/ENI로 확인할 수 있습니다. **템플릿에 IMDS 값을 자동 주입하지 마세요.**

---

## 4. 배포 순서 (greenfield)

```bash
export AWS_DEFAULT_REGION=ap-northeast-2

# 1) 로그인 시크릿 (SSM) — 평문 비밀번호는 저장하지 않음
pynt setwebuiauth

# 2) 앱 아티팩트 업로드 (webui + kpi + application)
pynt publishapps

# 3) 스택 생성 — ApplicationInstance 1대만
pynt createec2stack

# 4) URL 확인
pynt applicationip
pynt applicationstatus   # EC2_count=1, legacy present=false
```

접속 (self-signed 경고 수락):

- `https://<ApplicationElasticIp>/`
- `https://<ApplicationElasticIp>/kiosk`
- `https://<ApplicationElasticIp>/dashboard/`

TLS/EIP 레이스: 부팅 후 `application-tls-refresh.service` 가 공개 IP SAN을 보정합니다.
수동: `sudo /opt/app/regenerate-tls.sh` (alias → application-tls-refresh.sh)

---

## 5. 런타임 보안 요약

| 항목 | 값 |
|---|---|
| SG 인바운드 | **443**, **80**(리다이렉트만). **8080/8000/22 미개방** |
| FastAPI | `127.0.0.1:8080` |
| KPI | `127.0.0.1:8000` |
| `KIOSK_DB_PATH` | `/data/kiosk/kiosk.db` |
| EBS | encrypted gp3, `DeletionPolicy/UpdateReplacePolicy: Snapshot` |
| 세션 | `WEBUI_SESSION_HTTPS_ONLY=true` |
| IAM | 런타임 least-privilege (`ApplicationInstanceRole`) — 스택 Create/Delete 권한 없음 |

---

## 6. 스케줄러

`EnableSchedulerParameter=true` (기본): Mon–Fri Asia/Seoul **09 start / 11 stop / 13 start / 15 stop**
대상: **ApplicationInstance만**.

생성 직후 자동으로 즉시 stop 하지 않습니다. 검증 후 수동 stop:

```bash
aws ec2 stop-instances --instance-ids <ApplicationInstanceId>
```

`false` 로 두면 스케줄 리소스 자체를 만들지 않습니다.

---

## 7. 비용 주의 (구조)

| 항목 | 비고 |
|---|---|
| EC2 | running 시간만 컴퓨트 과금 |
| Root EBS + Data EBS | stopped 여도 과금 |
| EIP 1개 | 할당 유지 시 과금 |
| 데이터 스택 (Kinesis 등) | 별도 |

---

## 8. 삭제

```bash
pynt deleteec2stack
```

- Application 스택만 삭제. **데이터 스택·frames 버킷 유지**
- Data volume: **Snapshot** 정책 → 스냅샷/고아 볼륨 정리 책임은 운영자
- EIP 는 스택과 함께 release (Retain 없음)

---

## 9. 트러블슈팅

| 증상 | 확인 |
|---|---|
| 부트 후 서비스 없음 | `journalctl -u kiosk-fastapi`, `/var/log/application-bootstrap.log` |
| `/data/kiosk` 미마운트 | `lsblk`, `mountpoint /data/kiosk`, 볼륨 attach |
| 로그인 후 401 | SSM 시크릿, `WEBUI_SESSION_HTTPS_ONLY`, 호스트를 EIP 한 곳으로 고정 |
| cert IP 불일치 | `systemctl status application-tls-refresh`, `sudo /opt/app/regenerate-tls.sh` |
| capture/config 502 | 데이터 스택·IAM·리전 |

SSM: Systems Manager → Session Manager (SSH 불필요).

---

## Legacy Architecture (historical dual-host)

> **Not the recommended deployment.** Kept for historical context only.
> The current template **does not create** these resources.

Historically the EC2 stack could create:

- `WebUiInstance` (public :8080, self-signed uvicorn TLS)
- `KpiInstance` (public :8000)
- two EIPs, two SGs, two instance roles
- EventBridge schedules targeting both instances

Phase A briefly kept both plus Application for parallel migration. Because
`video-analyzer-ec2-stack` was **absent**, greenfield 3-host create was rejected and
legacy CFN resources were **removed** from `aws-infra/aws-infra-ec2-cfn.yaml`.

Standalone artifact paths (`apps/webui/bootstrap.sh`, `apps/kpi/bootstrap.sh`) may still
be published by `pynt publishapps` for experimental standalone hosts, but they are **not**
created by the EC2 CloudFormation stack.

Out-of-band **development/kiosk EC2** hosts (e.g. with code-server) remain **outside**
CloudFormation and are not `WebUiInstance` / `KpiInstance`.

See:

- `2026_08_11_ec2_application_stack_design.md`
- `2026_08_11_ec2_application_stack_phase_b_preflight.md`
- `2026_08_11_ec2_application_stack_greenfield_result.md`
