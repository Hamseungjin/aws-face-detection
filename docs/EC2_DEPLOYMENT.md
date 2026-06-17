# EC2 배포 가이드 (webui + KPI Dashboard)

개발/데모/포트폴리오용으로, 기존 서버리스 파이프라인은 그대로 두고 EC2 2대(웹 UI, KPI 대시보드)와
자동 start/stop 스케줄러를 **별도 CloudFormation 스택**으로 추가하는 가이드입니다.
리전 기준: **ap-northeast-2 (서울)**.

> 검증 환경 참고: `aws-infra/aws-infra-ec2-cfn.yaml`에는 실제 탭 문자(0x09)가 없습니다
> (Python 바이트 스캔 결과 0개). YAML 파싱 안전.

## 1. 전체 구조 요약
- **`video-analyzer-stack` (기존, 변경 없음)**: Kinesis · Lambda(imageprocessor/framefetcher/facecompare) ·
  Rekognition · S3(frames) · DynamoDB(EnrichedFrame) · API Gateway.
- **`video-analyzer-ec2-stack` (신규)**: webui EC2 + KPI EC2 + EventBridge Scheduler + 보안그룹 + IAM Role.
  - 두 스택은 느슨하게 연결: EC2가 런타임에 기존 스택 값을 **읽기만** 함(Export/ImportValue 미사용) →
    데이터 스택을 건드리지 않고 EC2 스택만 생성/삭제 가능.

```text
EventBridge Scheduler (Asia/Seoul, Mon-Fri 09/11/13/15)  ── start/stop ──┐
                                                                          ▼
  webui EC2 (t3.micro :8080)            KPI EC2 (t3.micro :8000)
  http.server /opt/webui                uvicorn /opt/kpi (FastAPI)
  apigw.js 부팅 시 생성                  /api/kpis (live, TTL 60s, lookback 1h)
        │ 브라우저 → API GW 직접 호출            │ boto3 → CloudWatch/DynamoDB 읽기
        ▼                                        ▼
  ───────────── video-analyzer-stack (데이터 파이프라인, 변경 없음) ─────────────
```

## 2. 사전 조건
- **기존 `video-analyzer-stack`이 먼저 배포되어 있어야 함** (webui의 apigw.js가 이 스택에서 API URL/Key를
  읽고, KPI가 이 스택의 로그/DynamoDB를 읽음). 없으면 webui의 `ExecStartPre`가 실패해 webui가 뜨지 않음(의도된 안전장치).
- **S3 code bucket 존재**: `config/ec2-params.json`의 `AppArtifactS3BucketParameter`
  (기본 `video-analyzer-code-115019372648-ap-northeast-2`). `pynt deploylambda`를 한 번이라도 했다면 생성돼 있음.
- **AWS 자격증명** (admin 또는 CFN/EC2/IAM/Scheduler 생성 권한).
- **리전 ap-northeast-2** 기준 (CLI 기본 리전 또는 `AWS_DEFAULT_REGION`).

## 3. 초기 설정 (1회)
1. 기본 VPC와 퍼블릭 서브넷 확인:
   ```
   pynt ec2vpcinfo
   ```
2. 출력의 기본 VPC id와 **PUBLIC** 서브넷 id를 `config/ec2-params.json`에 채움:
   ```json
   "VpcIdParameter": "vpc-xxxxxxxx",
   "SubnetIdParameter": "subnet-xxxxxxxx"
   ```
3. **`AllowedIngressCidrParameter`** (기본 `0.0.0.0/0`): 웹 포트(8080/8000)를 **누구에게 열지** 결정.
   - 기본값은 전체 공개(데모 편의). SSH는 절대 열지 않음(관리 접속은 SSM).
   - **내 IP만 허용**하려면 `MY_PUBLIC_IP/32`로 바꾸고 `pynt updateec2stack` 실행:
     ```json
     "AllowedIngressCidrParameter": "203.0.113.45/32"
     ```

## 4. 배포 순서 (최초)
```
pynt publishapps                 # webui + kpi artifact를 S3 apps/webui, apps/kpi 로 업로드
pynt createec2stack              # EC2 2대 + 보안그룹 + IAM + 스케줄러 생성 (CREATE_COMPLETE)
pynt ec2ip                       # 두 인스턴스의 public IP(= 고정 Elastic IP) 확인
```
- 접속 주소는 **Elastic IP로 고정**입니다(스택 Outputs `WebUiUrl`/`KpiUrl`, `WebUiElasticIp`/`KpiElasticIp`).
- webui 접속: 브라우저 `http://<WebUiElasticIp>:8080` → SPA 로드, DevTools에서 `/enrichedframe` 200.
- KPI 접속: 브라우저 `http://<KpiElasticIp>:8000` → 대시보드, 배지가 **LIVE**.
- `/api/kpis`에서 `"source":"live"` 확인(아래 6장).
- **스케줄러는 정해진 시각에만 동작** → 최초 검증 후 인스턴스가 켜져 있으면 **수동 stop**(8장 비용).

## 5. 이미 EC2 스택이 떠 있을 때 앱 코드 갱신
앱 코드(webui 정적 파일, KPI 백엔드)는 S3 artifact로 배포되며, **EC2 UserData는 첫 부팅에만 실행**됩니다.
따라서 코드 변경 후에는:
```
pynt publishapps webui           # 또는: pynt publishapps kpi  /  pynt publishapps (둘 다)
```
그다음 인스턴스에 **새 artifact를 다시 받게** 해야 합니다:
- **`systemctl restart` 만으로는 S3의 새 artifact를 받지 않습니다.** (이미 받은 `/opt/...`만 재실행)
- SSM 접속 후 bootstrap 재실행:
  ```bash
  # webui
  sudo WEBUI_PORT=8080 DATA_STACK=video-analyzer-stack DATA_API_STAGE=development \
       ARTIFACT_BUCKET=<bucket> ARTIFACT_PREFIX=apps/ bash /opt/app/bootstrap.sh
  # kpi
  sudo KPI_PORT=8000 DATA_STACK=video-analyzer-stack \
       ARTIFACT_BUCKET=<bucket> ARTIFACT_PREFIX=apps/ bash /opt/app/bootstrap.sh
  ```
  (재다운로드 + 재설치 + `systemctl restart` 포함)
- 또는 해당 인스턴스를 교체(재생성)하면 UserData가 다시 돌며 자동 적용.

> 참고: `webui`는 `apigw.js`를 systemd `ExecStartPre`에서 **매 시작 재생성**하므로, API 재배포 후 webui는
> `systemctl restart webui`만으로 설정이 보정됩니다(정적 파일 자체 갱신은 위 절차 필요).

## 6. 검증 방법
SSM Session Manager로 접속(SSH/키페어 불필요): 콘솔 → Systems Manager → Session Manager → Start session.
```bash
# webui 인스턴스
systemctl status webui
cat /opt/webui/src/apigw.js          # apiBaseUrl + apiKey 확인

# kpi 인스턴스
systemctl status kpi-dashboard
curl http://localhost:8000/healthz   # {"status":"ok"}
curl http://localhost:8000/api/kpis  # "source":"live", cache_hit/cache_expires_at, 섹션별 값 또는 {"error":...}
```
브라우저: `http://<WebUiElasticIp>:8080`, `http://<KpiElasticIp>:8000` (고정 EIP, 스택 Outputs `WebUiUrl`/`KpiUrl`).

## 7. 스케줄 동작 (자동 start/stop)
EventBridge Scheduler 4개, **Timezone Asia/Seoul**, 월~금:

| 시각(KST) | 동작 | 스케줄 이름 |
|---|---|---|
| 09:00 | start | `video-analyzer-ec2-start-0900` |
| 11:00 | stop | `video-analyzer-ec2-stop-1100` |
| 13:00 | start | `video-analyzer-ec2-start-1300` |
| 15:00 | stop | `video-analyzer-ec2-stop-1500` |

- **EIP 고정**: 인스턴스는 Elastic IP를 사용하므로 stop/start 후에도 **접속 URL이 동일**합니다(Outputs `WebUiUrl`/`KpiUrl`). 매번 `pynt ec2ip`로 새 IP를 확인할 필요 없음.
- **생성 직후 즉시 stop되지 않음**: 스케줄은 정해진 시각에만 실행됨(현재 시간이 운영 시간 밖인지 판단해 즉시 끄지 않음).
- **최초 검증 후 인스턴스가 running이면 수동 stop 필요**:
  ```
  aws ec2 stop-instances --instance-ids <WebUiInstanceId> <KpiInstanceId>
  ```
- **수동으로 켜도 11:00/15:00 stop 스케줄이 오면 꺼짐**(stop은 무조건 실행, 이미 정지면 no-op).
- **빈틈**: 15:00 이후 수동 start 시 **다음 stop 시각(다음 영업일 11:00 KST)** 까지 켜질 수 있음 →
  **야간 안전 stop은 P1 옵션**(예: 평일 23:00 stop 스케줄 추가). 현재 운영 스케줄은 09/11/13/15 유지.

## 8. 비용 주의
- **EC2 컴퓨트**: **running 시간에만** 과금(평일 4시간/일 목표).
- **EBS 루트 볼륨(gp3 8GB×2)**: **stopped 상태에서도 24/7 과금**. 줄이려면 인스턴스/스택을 삭제해야 함.
- **Elastic IP (EIP) ×2 — 접속 편의성 우선 선택**: 이 스택은 **고정 IP(EIP)** 를 사용합니다.
  **EIP/public IPv4는 인스턴스가 stopped 상태여도 계정에 할당돼 있으면 24/7 과금**됩니다
  (~$0.005/h → EIP당 ~$3.6/월, **2개 ≈ 월 +~$7** 상시). 이는 **비용 최소보다 "고정 URL 접속 편의성"을
  우선**한 선택입니다. 비용을 줄이려면 EIP를 release(= 스택 삭제)해야 합니다.
- **EventBridge Scheduler**: 월 ~수십 회 호출 → 무료티어로 사실상 $0.
- **만들지 않는 것**: NAT, ALB, Route53, ACM (전부 추가 비용 없음).
- 대략 월 ~$10–11 (t3.micro 2대 + EBS + EIP 2개; **EIP 미사용 대비 약 +$7/월**).

## 9. 보안 주의
- **HTTP only, 도메인 없음, HTTPS 없음** (데모).
- `AllowedIngressCidrParameter`가 `0.0.0.0/0`이면 웹 포트(8080/8000)가 **전체 공개**. 가능하면 `MY_PUBLIC_IP/32`로 제한.
- **webui `apigw.js`에 API Key가 평문 노출**(브라우저로 전달). 이 키는 인증 비밀이 아니라 usage-plan
  throttling/quota 제어용이지만, 페이지 접근자는 누구나 볼 수 있음.
- **운영 전환 시(P2)**: Cognito / Lambda Authorizer로 API 인증, ALB+ACM으로 HTTPS, WAF, 백엔드 프록시 등 필요.

## 10. 삭제 / 정리
```
pynt deleteec2stack              # EC2 스택만 삭제 (DELETE_COMPLETE)
```
- **EC2 스택 삭제 시 EBS 루트 볼륨도 함께 삭제됨** (인스턴스 `DeleteOnTermination: true`). 보안그룹·IAM Role·스케줄러도 제거.
- **Elastic IP 2개도 함께 release됨** (`DeletionPolicy: Retain` 미부여). ⚠️ 스택 삭제가 부분 실패하거나 콘솔에서
  수동 할당한 EIP가 남으면 **orphan EIP가 계속 과금**되므로, 삭제 후 콘솔 **EC2 → Elastic IPs**에 남은 주소가 없는지 확인.
- **기존 `video-analyzer-stack`은 삭제하지 않음** (별도 스택). **frames S3 버킷, DynamoDB 데이터 유지**.
- `deleteec2stack`은 frames 버킷을 비우거나 usage plan을 건드리지 않음(데이터 스택 전용 로직과 분리).

## 11. 트러블슈팅
- **SSM 접속 안 됨**: 인스턴스 IAM Role에 `AmazonSSMManagedInstanceCore`가 있는지, 인스턴스가 running인지,
  아웃바운드 443이 열려 있는지(기본 SG egress allow) 확인. Systems Manager → Fleet Manager에 managed로 보이는지 확인.
- **webui가 안 뜸**: `systemctl status webui`, `journalctl -u webui`, `cat /var/log/webui-bootstrap.log`.
  보통 `ExecStartPre`(apigw.js 생성) 실패 → 기존 `video-analyzer-stack` 미배포 또는 권한 부족.
- **apigw.js 생성 실패**: `/var/log/webui-bootstrap.log` 및 `journalctl -u webui`. webui Role에
  `cloudformation:DescribeStackResource`(데이터 스택) + `apigateway:GET`(/apikeys) 권한 확인.
  `DATA_STACK`/`DATA_API_STAGE` 값 확인(`/etc/webui.env`).
- **KPI가 mock으로 보임**: PR 4 이전 코드가 떠 있는 것 → `pynt publishapps kpi` 후 bootstrap 재실행(5장).
- **KPI가 error 배지**: `/api/kpis`의 최상위 `error`/`collect_error` 확인(브라우저 콘솔에도 `console.warn`).
  보통 region/네트워크 문제.
- **카드별 AccessDenied(error)**: 해당 섹션만 `{"error":"...not authorized to perform: logs:StartQuery..."}`로
  표시되고 다른 섹션은 정상(partial failure). KPI Role의 `kpi-data-read` 정책(logs/cloudwatch/dynamodb) 확인.
- **접속 IP 확인**: 이제 **EIP로 고정**되어 stop/start 후에도 IP가 바뀌지 않습니다(Outputs `WebUiElasticIp`/`KpiElasticIp`).
  `pynt ec2ip`는 현재(= 고정 EIP) IP 확인용으로 유지. 만약 stop/start 후 IP가 바뀐다면 EIP association이 누락된 것이니
  콘솔 EC2 → Elastic IPs에서 EIP가 인스턴스에 연결돼 있는지 확인.
