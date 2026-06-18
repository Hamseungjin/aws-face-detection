# 비용 분석 (Cost Explain)

> **조사 시점:** 2026-06-18 / **리전:** ap-northeast-2(서울) / **프로필:** `video-analyzer`
> **방식:** AWS CLI **조회 전용**(read-only)만 사용. 삭제·중지·수정 변경 작업은 수행하지 않음.
> **환율 기준:** 약 ₩1,380/USD 환산(근사치). 단가는 서울 리전 근사치이며 시점에 따라 다를 수 있음.

EC2 인스턴스를 **중지(stop)** 한 상태에서 어떤 AWS 리소스가 남아 있고 어떤 비용이 계속 발생하는지 분석한 문서입니다.

**결론부터:** EC2를 중지해도 비용은 0원이 되지 않습니다. 그리고 조사 중 두 가지 중요한 사실을 발견했습니다.

- ⚠️ EC2 인스턴스는 **2개가 아니라 3개**입니다. 어느 스택에도 속하지 않은 `i-09d588e2927e6c36a`(t3.small, EIP `13.209.30.31`, 20GB EBS)가 별도로 존재합니다.
- ⚠️ **자동 시작 스케줄러 4개가 ENABLED** 상태라 중지한 인스턴스가 평일 **09:00·13:00(KST)** 에 자동으로 다시 켜집니다. 수동 중지는 일시적입니다.

---

## 1. 결론 요약

| 구분 | 내용 |
|---|---|
| **EC2 중지로 멈춘 비용** | EC2 **컴퓨팅(인스턴스 시간) 요금만** 멈춤. t3.micro 2대 + (미관리) t3.small 1대의 시간당 요금이 중지 동안 $0. |
| **중지 후에도 계속 발생하는 비용** | ① **Elastic IP / Public IPv4 3개** ② **Kinesis 스트림 1샤드(24시간 가동)** ③ **EBS 볼륨 36GB(8+8+20)** — 이 셋은 인스턴스가 꺼져 있어도 24/7 과금. |
| **가장 비용이 클 가능성이 높은 리소스** | **Kinesis 프로비저닝 샤드(≈$11/월)** 와 **Elastic IP 3개(≈$11/월)** 가 거의 동률 1위, **EBS 36GB(≈$3.3/월)** 가 그다음. 합계 **약 $25/월(≈₩3.5만)** 가 중지 상태에서도 계속 나감. |

---

## 2. CloudFormation 스택 상태

“스택이 실행 중”이라는 말은 세 가지를 구분해야 합니다.

- **CloudFormation 스택 상태** = 리소스 정의/프로비저닝 상태. `CREATE_COMPLETE`는 “리소스가 **존재**한다”는 뜻이지 “컴퓨팅이 **돌고 있다**”는 뜻이 아님. 스택 자체는 “실행” 개념이 없음(과금도 없음).
- **EC2 인스턴스 상태** = `stopped` (컴퓨팅 정지).
- **스택 내 다른 리소스의 활성 상태** = Kinesis `ACTIVE`, DynamoDB `ACTIVE`, Lambda 배포됨 등 — EC2와 독립적으로 계속 활성.

| Stack Name | Stack Status | 생성 시간(UTC) | 수정 시간 | 의미 |
|---|---|---|---|---|
| `video-analyzer-stack` | CREATE_COMPLETE | 2026-06-18 06:42 | None | 서버리스 백엔드 리소스가 모두 존재·활성(Kinesis/Lambda/DynamoDB/API GW/S3). EC2와 무관하게 유지됨 |
| `video-analyzer-ec2-stack` | CREATE_COMPLETE | 2026-06-18 07:11 | None | EC2 2대·EIP 2개·스케줄러 4개·IAM이 존재. 인스턴스는 stopped지만 EIP/EBS/스케줄러는 살아 있음 |

> 두 스택 모두 **2026-06-18 생성**되어 아직 몇 시간만 가동됨 → Cost Explorer 누적액이 거의 $0으로 보이지만, 이는 “저렴하다”가 아니라 “아직 시간이 안 쌓였다”는 의미. 판단은 **누적액이 아니라 시간당 단가(run-rate)** 로 해야 함.

---

## 3. 스택별 리소스 목록

### 3-1. `video-analyzer-stack` (서버리스 백엔드)

| Logical ID | Resource Type | Physical ID | 현재 상태 | 비용 발생 | 비고 |
|---|---|---|---|---|---|
| FrameStream | Kinesis::Stream | FrameStream | ACTIVE, 1샤드, PROVISIONED | 🔴 계속 | 24/7 샤드 과금. EC2와 무관 |
| EnrichedFrameTable | DynamoDB::Table | EnrichedFrame | ACTIVE, **PAY_PER_REQUEST**, 0 items | 🟢 사실상 0 | 온디맨드 → 유휴 시 요금 없음(스토리지 0) |
| FrameS3Bucket | S3::Bucket | video-analyzer-frames-…-ap-northeast-2 | 90객체 / 2.6MB | 🟢 미미 | 스토리지 ≈ $0.0001/월 |
| imageprocessor | Lambda::Function | imageprocessor | 배포됨 | 🟢 0(유휴) | Kinesis 이벤트소스. 캡처 정지 시 호출 없음 |
| framefetcher | Lambda::Function | framefetcher | 배포됨 | 🟢 0(유휴) | 호출당 과금 |
| facecompare | Lambda::Function | facecompare | 배포됨 | 🟢 0(유휴) | 호출당 과금 |
| EventSourceMapping | Lambda::EventSourceMapping | f6cd2995-… | Enabled | 🟢 0 | 폴링은 샤드 비용에 포함 |
| VidAnalyzerRestApi (+Stage/UsagePlan/ApiKey/Methods) | ApiGateway::* | woxbljcdz6 | 배포됨 | 🟢 0(유휴) | REST API는 시간당 요금 없음, 요청당 과금 |
| 각종 IAM Role/Policy/Permission | IAM::* | … | 활성 | 🟢 0 | IAM 무료 |

### 3-2. `video-analyzer-ec2-stack` (EC2 프런트)

| Logical ID | Resource Type | Physical ID | 현재 상태 | 비용 발생 | 비고 |
|---|---|---|---|---|---|
| WebUiInstance | EC2::Instance | i-064aa4033fd428313 (t3.micro) | **stopped** | 🟡 컴퓨팅 0 / EBS·EIP는 별도 | 중지 중엔 시간당 요금 없음 |
| KpiInstance | EC2::Instance | i-02115c204ecf26406 (t3.micro) | **stopped** | 🟡 컴퓨팅 0 / EBS·EIP는 별도 | 동일 |
| WebUiElasticIp | EC2::EIP | 3.39.183.12 | 연결됨(중지 인스턴스에) | 🔴 계속 | Public IPv4 시간당 과금 |
| KpiElasticIp | EC2::EIP | 3.37.126.71 | 연결됨(중지 인스턴스에) | 🔴 계속 | 동일 |
| (EBS, 스택에 미표시) | EC2::Volume | vol-099ed71… 8GB / vol-0f933… 8GB | in-use(attached) | 🔴 계속 | 중지해도 스토리지 과금 |
| ScheduleStart/Stop ×4 | Scheduler::Schedule | start-0900/1300, stop-1100/1500 | **ENABLED** | 🟢 ~0 | 거의 무료지만 **자동 재기동 유발** |
| SchedulerRole / Instance Role·Profile ×3 | IAM::* | … | 활성 | 🟢 0 | IAM 무료 |
| KpiSecurityGroup / WebUiSecurityGroup | EC2::SecurityGroup | sg-059ba…, sg-000882… | 활성 | 🟢 0 | 보안그룹 무료 |

### 3-3. ⚠️ 어느 스택에도 속하지 않는 리소스

| 리소스 | 식별자 | 상태 | 비용 발생 | 비고 |
|---|---|---|---|---|
| EC2 인스턴스 | **i-09d588e2927e6c36a** (t3.small) | stopped | 🟡 컴퓨팅 0 / EBS·EIP 별도 | Tag `Scheduling=OfficeHours`, 2026-06-17 기동. **CloudFormation 관리 밖** |
| Elastic IP | **13.209.30.31** (eipalloc-0457b…) | 연결됨 | 🔴 계속 | Public IPv4 과금 |
| EBS 볼륨 | **vol-07bd61ae60a911234** (20GB gp3) | in-use | 🔴 계속 | 36GB 중 가장 큼 |

---

## 4. EC2 중지 상태에서의 비용 영향

**더 이상 발생하지 않는 비용 (중지로 절감됨)**
- EC2 **인스턴스 시간당 컴퓨팅 요금** (t3.micro ×2, t3.small ×1) → 중지 동안 $0.

**중지해도 계속 발생하는 비용**
- **Elastic IP / Public IPv4 3개** — 2024-02 이후 AWS는 모든 Public IPv4를 (연결 여부·인스턴스 실행 여부와 무관하게) 시간당 과금.
- **EBS 36GB(gp3)** — `in-use`(중지 인스턴스에 부착) 상태도 스토리지 요금 100% 부과.
- **Kinesis FrameStream 1샤드** — EC2와 완전 무관하게 24/7 가동.
- (미미) S3 스토리지, CloudWatch Logs 보관, DynamoDB 스토리지(현재 0).

**애플리케이션 동작상 멈춘 부분**
- Web UI EC2 중지 → `https://3.39.183.12:8080` 접속 불가 → 브라우저 ‘촬영하기’·최근 프레임 조회·얼굴 비교 모두 중단.
- KPI Dashboard EC2 중지 → `http://3.37.126.71:8000` 대시보드 중단.

**그래도 백그라운드에서 계속 도는 부분**
- Kinesis 스트림은 계속 ACTIVE(빈 스트림이라도 샤드 과금).
- Lambda/API Gateway/DynamoDB는 “대기” 상태로 살아 있음(요청 없으면 과금 없음).
- **EventBridge 스케줄러 4개가 평일 09:00·13:00(KST)에 인스턴스를 자동 start** → 중지가 풀리고 컴퓨팅 요금이 다시 시작됨.

---

## 5. 비용 발생 가능 리소스 상세 분석

| 리소스 | 왜 과금? | 현재 발생 가능성 | 절감 조치 |
|---|---|---|---|
| **중지 EC2 컴퓨팅** | 인스턴스 실행 시간 | 🟢 중지 중 $0 | 스케줄러 자동 start를 끄지 않으면 다시 켜짐 |
| **EBS 볼륨 36GB** | 부착·중지 무관 스토리지 | 🔴 ≈$3.3/월 | 불필요 인스턴스/볼륨 삭제, 또는 스냅샷 후 볼륨 삭제 |
| **EBS 스냅샷** | 스냅샷 GB-월 | 🟢 **스냅샷 없음 → $0** | 해당 없음 |
| **Elastic IP ×3 / Public IPv4** | 2024-02~ 모든 Public IPv4 시간당($0.005/hr) | 🔴 ≈$11/월(3개) | 미사용 EIP 릴리스 |
| **Load Balancer** | 시간 + LCU | 🟢 **없음 → $0** | 해당 없음 |
| **NAT Gateway** | 시간 + 데이터 | 🟢 **없음 → $0** | 해당 없음 |
| **CloudWatch Logs/Metrics/Alarms** | 로그 보관·알람 개수 | 🟢 로그 ~4.7MB(≈$0), 알람 0개 | 필요시 retention 단축 |
| **S3 (2버킷)** | 스토리지/요청 | 🟢 ~3.4MB ≈ $0.0001/월 | `deletedata`로 프레임 정리 가능 |
| **ECR** | 이미지 스토리지 | 🟢 **리포지토리 없음 → $0** | 해당 없음 |
| **Lambda ×4** | 호출·실행시간 | 🟢 유휴 시 $0 | 해당 없음 |
| **API Gateway (REST)** | 요청당(시간당 요금 없음) | 🟢 유휴 시 $0 | 해당 없음 |
| **DynamoDB EnrichedFrame** | 온디맨드(PAY_PER_REQUEST) | 🟢 요청·스토리지 0 → $0 | 해당 없음 |
| **Kinesis FrameStream** | 프로비저닝 샤드시간 24/7 | 🔴 ≈$11/월(1샤드) | 사용 안 하면 스택 삭제가 유일한 정지 방법 |
| **RDS/ElastiCache/OpenSearch** | — | 🟢 **모두 없음 → $0** | 해당 없음 |
| **Route53/CloudFront/ACM/Secrets Manager** | — | 🟢 **모두 없음 → $0** | 해당 없음 |
| **KMS** | 고객관리키(CMK) $1/월 | 🟢 **3개 모두 AWS 관리형(alias/aws/*) → $0** | 해당 없음 |
| **SSM 파라미터 ×3** | Standard 무료 | 🟢 $0 | 해당 없음 |

### 월간 run-rate 추정 (중지 상태 유지 시, 근사치)

| 항목 | 단가(서울, 근사) | 월 비용 |
|---|---|---|
| Public IPv4 ×3 | $0.005/hr ×3 | ≈ **$10.95** |
| Kinesis 1샤드 | ~$0.015/shard-hr | ≈ **$11** |
| EBS gp3 36GB | ~$0.0912/GB-월 | ≈ **$3.3** |
| S3 / Logs / 기타 | — | < $0.01 |
| **합계** | | **≈ $25/월 (≈ ₩3.5만), ≈ $0.83/일** |

> EC2가 자동 재기동되어 매일 4시간(09–11, 13–15)씩 켜지면 컴퓨팅이 추가됨(3대 합쳐 대략 +$5~6/월 수준).

---

## 6. 확인용 AWS CLI 명령어 (read-only)

모두 `--profile video-analyzer --region ap-northeast-2` 기준. (Cost Explorer만 `--region us-east-1`)

```bash
# (1) CloudFormation stack status
aws cloudformation describe-stacks --stack-name video-analyzer-stack \
  --query "Stacks[].{Name:StackName,Status:StackStatus,Created:CreationTime}" --output table
aws cloudformation describe-stacks --stack-name video-analyzer-ec2-stack \
  --query "Stacks[].{Name:StackName,Status:StackStatus,Created:CreationTime}" --output table

# (2) Stack resources
aws cloudformation list-stack-resources --stack-name video-analyzer-stack --output table
aws cloudformation list-stack-resources --stack-name video-analyzer-ec2-stack --output table

# (3) EC2 instance state (전체 — 숨은 3번째 인스턴스 포함)
aws ec2 describe-instances \
  --query "Reservations[].Instances[].{ID:InstanceId,Name:Tags[?Key=='Name']|[0].Value,Type:InstanceType,State:State.Name}" --output table

# (4) EBS volumes
aws ec2 describe-volumes \
  --query "Volumes[].{ID:VolumeId,Size:Size,Type:VolumeType,State:State,Attached:Attachments[0].InstanceId}" --output table

# (5) EBS snapshots
aws ec2 describe-snapshots --owner-ids self --query "Snapshots[].{ID:SnapshotId,Size:VolumeSize}" --output table

# (6) Elastic IP / Public IPv4
aws ec2 describe-addresses --query "Addresses[].{IP:PublicIp,AllocID:AllocationId,Instance:InstanceId}" --output table

# (7) Load Balancer
aws elbv2 describe-load-balancers --output table
aws elb   describe-load-balancers --output table

# (8) NAT Gateway
aws ec2 describe-nat-gateways --query "NatGateways[].{ID:NatGatewayId,State:State}" --output table

# (9) Kinesis (샤드 수 = 핵심 비용)
aws kinesis describe-stream-summary --stream-name FrameStream \
  --query "StreamDescriptionSummary.{Mode:StreamModeDetails.StreamMode,Shards:OpenShardCount}" --output table

# (10) CloudWatch logs
aws logs describe-log-groups --query "logGroups[].{Name:logGroupName,Bytes:storedBytes,Retention:retentionInDays}" --output table

# (11) S3 buckets + size
aws s3api list-buckets --query "Buckets[].Name" --output table
aws s3 ls s3://video-analyzer-frames-115019372648-ap-northeast-2 --recursive --summarize | tail -3

# (12) ECR / Lambda / API GW / DynamoDB
aws ecr describe-repositories --output table
aws lambda list-functions --query "Functions[].FunctionName" --output table
aws apigateway get-rest-apis --query "items[].{Name:name,ID:id}" --output table
aws dynamodb describe-table --table-name EnrichedFrame \
  --query "Table.{Billing:BillingModeSummary.BillingMode,Size:TableSizeBytes}" --output table

# (13) 자동 재기동 스케줄러 확인 (중요!)
aws scheduler list-schedules --query "Schedules[].{Name:Name,State:State}" --output table

# (14) Cost Explorer (서비스별, MTD)
aws ce get-cost-and-usage --region us-east-1 \
  --time-period Start=2026-06-01,End=2026-06-19 --granularity MONTHLY \
  --metrics UnblendedCost --group-by Type=DIMENSION,Key=SERVICE --output table
```

---

## 7. 최종 판단

**“EC2를 중지했으니 비용이 0원이 되는가?” → 아니요.**

중지로 멈추는 것은 **EC2 인스턴스의 시간당 컴퓨팅 요금뿐**입니다. **Elastic IP 3개·Kinesis 샤드·EBS 36GB는 인스턴스가 꺼져 있어도 24/7 과금**되어 **약 $25/월(≈₩3.5만)** 가 계속 나갑니다. 게다가 **자동 시작 스케줄러가 켜져 있어 평일 09:00·13:00(KST)에 인스턴스가 다시 켜지므로** 컴퓨팅 요금도 곧 재개됩니다. “스택이 CREATE_COMPLETE”라는 것은 “리소스가 존재한다”일 뿐 “돌고 있다/꺼졌다”와는 별개입니다.

### 우선 확인해야 할 리소스 TOP 5

1. **🔴 스택 밖 미관리 인스턴스 `i-09d588e2927e6c36a`(t3.small) + EIP `13.209.30.31` + 20GB EBS** — 어느 스택에도 없어 `deleteec2stack`으로도 정리되지 않음. 사용처를 가장 먼저 확인(가장 큰 단일 볼륨이자 가장 큰 인스턴스 타입).
2. **🔴 EventBridge 스케줄러 4개(ENABLED)** — 수동 중지를 무력화하는 자동 재기동. 비용을 정말 멈추려면 이 스케줄러 처리 여부부터 결정해야 함.
3. **🔴 Kinesis `FrameStream`(1샤드, PROVISIONED)** — EC2와 무관한 최대 상시 비용 중 하나(≈$11/월). 사용하지 않으면 서버리스 스택 삭제(`pynt deletestack`)가 유일한 정지 방법.
4. **🔴 Elastic IP 3개** — 중지 인스턴스에 붙어 있어도 Public IPv4 요금(≈$11/월). 미사용 IP가 있는지 점검.
5. **🟡 EBS 볼륨 36GB(8+8+20)** — 중지해도 스토리지 과금(≈$3.3/월). 특히 미관리 인스턴스의 20GB.

> 완전히 과금을 멈추려면 RUNBOOK 4장/5-3절의 `pynt deletestack` + `pynt deleteec2stack`(스택 리소스 정리)과, **스택에 속하지 않은 3번째 인스턴스·EIP·볼륨의 수동 정리**가 별도로 필요합니다.
