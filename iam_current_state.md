# Current AWS IAM State

조사일: 2026-08-14  
리전: `ap-northeast-2`  
계정: `115019372648`  
방식: AWS CLI **read-only** (`get-*` / `list-*` / `describe-*`) + 현재 local working tree IaC  
변경: IAM/AWS 리소스 **미변경**. git commit/push **없음**.

---

## 1. 조사 목적

IAM을 고치는 것이 아니라, 현재 살아 있는 Role/Policy와 local IaC가 요구하는 권한의 차이를 확인한다. 특히 키오스크가 Collection + FaceId로 전환 중인 상태에서 `CompareFaces` / `IndexFaces` / `SearchFacesByImage` / `DeleteFaces` / `CreateCollection`이 **실제로 누구에게 있는지**를 가른다.

---

## 2. AWS Caller / Profile

`aws configure list-profiles` 결과:

| Profile | 용도 |
|---|---|
| (default, 이름 없음) | EC2 instance metadata |
| `application-deployer` | `role_arn` = `ApplicationDeployerRole`, `credential_source=Ec2InstanceMetadata` |

`aws sts get-caller-identity`:

| Profile | Account | Caller ARN | 유형 |
|---|---|---|---|
| default | `115019372648` | `arn:aws:sts::115019372648:assumed-role/AwsFaceDetectionKioskRole/i-060ed51daa9abc84f` | IAM Role (`AwsFaceDetectionKioskRole`)를 EC2가 Assume |
| `application-deployer` | `115019372648` | `arn:aws:sts::115019372648:assumed-role/ApplicationDeployerRole/botocore-session-…` | IAM Role (`ApplicationDeployerRole`)를 위 Role이 Assume |

Access Key / Secret / session token은 기록하지 않는다.

`iam:ListRoles` 는 두 principal 모두 AccessDenied. 아래 Role 목록은 **이름을 추측해 `get-role`로 확인된 것만**이다. 계정 전체 Role 목록은 **현재 조회 자격증명으로 확인 불가**.

---

## 3. 현재 IAM Role 목록

`get-role`으로 존재 확인된 Role:

| Role Name | ARN | 비고 |
|---|---|---|
| `ApplicationDeployerRole` | `arn:aws:iam::115019372648:role/ApplicationDeployerRole` | 존재. CloudFormation 관리 아님(독립 IAM) |
| `ApplicationStackExecutionRole` | `arn:aws:iam::115019372648:role/ApplicationStackExecutionRole` | 존재. CloudFormation 관리 아님 |
| `AwsFaceDetectionKioskRole` | `arn:aws:iam::115019372648:role/AwsFaceDetectionKioskRole` | 존재. 현재 이 조사 호스트의 runtime Role |

`get-role`이 `NoSuchEntity`인 이름 (존재하지 않음):

- `ApplicationInstanceRole`
- `video-analyzer-ec2-stack-ApplicationInstanceRole` (접미사 없는 정확 이름)
- `ImageProcessorLambdaExecutionRole` / `FrameFetcherLambdaExecutionRole` / `FaceCompareLambdaExecutionRole`
- `SchedulerRole`
- `video-analyzer-WebUiInstanceRole` / `video-analyzer-KpiInstanceRole`

CloudFormation:

| Stack | Live 상태 |
|---|---|
| `video-analyzer-ec2-stack` | **현재 존재하지 않음** (`DELETE_COMPLETE` 이력만) |
| `video-analyzer-stack` | **현재 존재하지 않음** (`DELETE_COMPLETE` 이력만) |

따라서 generated `ApplicationInstanceRole-*` PhysicalResourceId는 **현재 AWS에 없다.** Role 이름을 과거 문서에서 추정하지 않았고, `get-role`이 실패한 이름은 제외했다.

실행 중인 EC2:

| Instance | State | Name tag | Instance profile ARN |
|---|---|---|---|
| `i-060ed51daa9abc84f` | running | code-server | `arn:aws:iam::115019372648:instance-profile/AwsFaceDetectionKioskRole` |
| `i-09d588e2927e6c36a` | stopped | book-rental | profile 없음 |

**실제 Application/개발 EC2가 쓰는 Role 이름 = `AwsFaceDetectionKioskRole`.**  
IaC의 `ApplicationInstanceRole`은 스택이 지워진 뒤 남아 있지 않다.

---

## 4. ApplicationDeployerRole

| 항목 | 내용 |
|---|---|
| Role Name | `ApplicationDeployerRole` |
| ARN | `arn:aws:iam::115019372648:role/ApplicationDeployerRole` |
| Path | `/` |
| Created | 2026-08-12T01:10:12+00:00 |
| MaxSessionDuration | 3600 |
| PermissionsBoundary | 없음 (`null`) |
| Description | (empty) |
| CloudFormation 관리 | 아니오 (독립 Role). last used 2026-08-12 ap-northeast-2 |
| 관련 Stack | 대상 스택 이름 `video-analyzer-ec2-stack` (현재 DELETE_COMPLETE) |

**Trust:** `AwsFaceDetectionKioskRole`만 `sts:AssumeRole`.

**Inline (확인됨):** `ApplicationDeployerPolicy` 1개.

**Managed / Instance profile 목록:** `ListAttachedRolePolicies` / `ListInstanceProfilesForRole` AccessDenied → **현재 조회 자격증명으로 확인 불가.** EC2 describe 기준으로 이 Role은 instance profile에 붙어 있지 않다.

할 수 있는 일 (inline 기준):

- `video-analyzer-ec2-stack` ChangeSet 생성/조회/실행 (이름 `application-greenfield-create-*`, RoleArn은 `ApplicationStackExecutionRole`로 고정)
- 같은 스택 Describe/GetTemplate/DeleteStack
- 그 Role만 CloudFormation에 `iam:PassRole`
- 네트워크 preflight describe, EIP quota 조회
- artifact bucket `aws-face-detection-lambda-115019372648-apne2` 의 `apps/*` Get/Put, prefix List

Rekognition / runtime CompareFaces / IndexFaces 없음.

---

## 5. ApplicationStackExecutionRole

| 항목 | 내용 |
|---|---|
| Role Name | `ApplicationStackExecutionRole` |
| ARN | `arn:aws:iam::115019372648:role/ApplicationStackExecutionRole` |
| Path | `/` |
| Created | 2026-08-12T01:07:37+00:00 |
| MaxSessionDuration | 3600 |
| PermissionsBoundary | 없음 |
| Description | Allows CloudFormation to create and manage AWS stacks and resources on your behalf. |
| CloudFormation 관리 | 아니오. CFN **service**가 Assume |
| 관련 Stack | `video-analyzer-ec2-stack` 리소스 생성용. 스택 자체는 현재 없음 |

**Trust:** `cloudformation.amazonaws.com` → `sts:AssumeRole`.

**Inline:** `ApplicationStackExecutionPolicy` 1개 (20 statements).

**Managed:** 확인 불가 (ListAttached AccessDenied).

할 수 있는 일 (inline 기준):

- AL2023 public AMI SSM 파라미터 읽기
- EC2 describe / t3.small RunInstances (지정 subnet)
- SG, 암호화 볼륨(ap-northeast-2c), EIP, 태그
- 스택 태그 `aws:cloudformation:stack-name=video-analyzer-ec2-stack` 인 instance/SG/volume/EIP 수명주기
- generated `video-analyzer-ec2-stack-ApplicationInstanceRole-*` 생성/인라인 정책/SSM managed attach만
- generated InstanceProfile 생성
- 그 runtime Role만 `iam:PassRole`
- 데이터 볼륨 snapshot 생성

**Rekognition Collection 생성 권한 없음.** S3/DynamoDB/Kinesis/Lambda 데이터 스택 권한도 없음 (Application 스택 전용).

---

## 6. Application Runtime Role

IaC가 말하는 runtime Role은 `ApplicationInstanceRole`(스택 generated 이름). **Live에는 없음.**

현재 이 호스트의 runtime Role은 **`AwsFaceDetectionKioskRole`**.

| 항목 | 내용 |
|---|---|
| Role Name | `AwsFaceDetectionKioskRole` |
| ARN | `arn:aws:iam::115019372648:role/AwsFaceDetectionKioskRole` |
| Path | `/` |
| Created | 2026-08-09T07:22:08+00:00 |
| MaxSessionDuration | 3600 |
| PermissionsBoundary | 없음 |
| Description | (empty) |
| CloudFormation 관리 | 아니오 (독립 Role, 개발/키오스크 공용으로 보임) |
| 관련 Stack | 없음. 현재 running instance `i-060ed51daa9abc84f` (code-server) 에 instance profile로 연결 |

**Trust:** `ec2.amazonaws.com` → `sts:AssumeRole`.

**Inline (확인됨, 6개):**

1. `ApplicationNetworkPreflightReadOnly`
2. `AssumeApplicationDeployerRole`
3. `AwsFaceDetectionDeployS3Policy`
4. `AwsFaceDetectionKioskPolicy`
5. `AwsFaceDetectionWebUiConfigReadPolicy`
6. `WebUiAuthBootstrapPolicy`

**Managed:** 확인 불가. SSM Session Manager가 동작한다면 `AmazonSSMManagedInstanceCore`가 붙어 있을 수 있으나 **조회하지 못했으므로 단정하지 않는다.**

이 Role은 “키오스크 FastAPI 최소권한”이 아니라, **개발 인스턴스 + 과거 데이터 스택 배포 + artifact S3**가 한 Role에 섞여 있다.

---

## 7. 기타 프로젝트 IAM Role

Data stack Lambda execution Role, SchedulerRole, generated ApplicationInstanceRole은 **현재 AWS에 없음** (스택 DELETE_COMPLETE).

Data deployer / Data stack execution Role은 IaC에 고정 이름이 없고 `get-role`로도 확인되지 않았다. **존재하지 않는다고 단정하지는 않지만, 이번 자격증명으로 추가 존재를 확인하지 못함.**

---

## 8. Trust Policy

| Role | Trusted Principal | Action | Condition | 의미 |
|---|---|---|---|---|
| `ApplicationDeployerRole` | `arn:aws:iam::115019372648:role/AwsFaceDetectionKioskRole` | `sts:AssumeRole` | 없음 | 개발 EC2 Role만 Deployer를 Assume |
| `ApplicationStackExecutionRole` | `cloudformation.amazonaws.com` | `sts:AssumeRole` | 없음 | CloudFormation service만 Assume |
| `AwsFaceDetectionKioskRole` | `ec2.amazonaws.com` | `sts:AssumeRole` | 없음 | EC2 instance profile |

- Deployer를 Assume하는 주체: **`AwsFaceDetectionKioskRole`** (그리고 그 Role의 inline `sts:AssumeRole` on Deployer).
- Execution Role을 Assume하는 주체: **CloudFormation service**.
- Instance Role: **EC2 service**.

---

## 9. Managed Policies

세 Role 모두 `iam:ListAttachedRolePolicies` AccessDenied.

`get-policy` / `get-policy-version`을 실행할 ARN을 확보하지 못했다.

**Managed Policy 수 (확인된 것): 0**  
**확인 불가인 attached managed: 있을 수 있음.**

Local IaC는 generated `ApplicationInstanceRole`에만 `AmazonSSMManagedInstanceCore`를 붙이도록 되어 있다. 그 Role은 live에 없다.

---

## 10. Inline Policies

확인된 inline **8개** (Deployer 1 + Execution 1 + Kiosk 6).

전문을 여기 수백 줄 붙이지 않고, 해석은 §12–17에 모은다. 원본은 조사 시 `get-role-policy`로 읽었다.

Live Deployer inline은 repo `docs/iam/application-deployer-policy.json`과 **완전히 같지 않다** (ChangeSet execute Sid 분리, `DeleteStack` 추가).  
Live Execution inline은 repo `docs/iam/application-cfn-execution-policy.json`과 **RunInstances Sid 분할 + snapshot 권한 추가**로 다르다.

---

## 11. Role / Instance Profile 관계

`ListInstanceProfilesForRole` / `GetInstanceProfile` AccessDenied.

EC2 `describe-instances`로 확인한 실제 연결:

```text
i-060ed51daa9abc84f (running, code-server)
    → instance-profile/AwsFaceDetectionKioskRole
        → (describe의 IamInstanceProfile.Arn)
```

`ApplicationDeployerRole` / `ApplicationStackExecutionRole`은 EC2 profile로 보이지 않는다. Deployer는 이 인스턴스가 Assume한다.

---

## 12. AWS Service별 권한

### ApplicationDeployerRole (`ApplicationDeployerPolicy`)

| AWS Service | Action | Resource | Condition | Policy | 사용 목적 |
|---|---|---|---|---|---|
| CloudFormation | ValidateTemplate | `*` | | inline | 템플릿 문법 검사 |
| CloudFormation | CreateChangeSet | `stack/video-analyzer-ec2-stack/*` | RoleArn=Execution, 이름 `application-greenfield-create-*` | inline | App 스택 배포 |
| CloudFormation | DescribeChangeSet, ExecuteChangeSet | 같은 스택 | Execute는 changeset 이름 제한 | inline | 검토 후 실행 |
| CloudFormation | Describe*/GetTemplate/ListStackResources | 같은 스택 | | inline | 상태 조회 |
| CloudFormation | DeleteStack | 같은 스택 | | inline | 실패 스택 삭제 (live only) |
| IAM | PassRole | `role/ApplicationStackExecutionRole` | PassedToService=cloudformation | inline | CFN에 execution role 전달 |
| EC2 | Describe* (네트워크) | `*` | region ap-northeast-2 | inline | preflight |
| Service Quotas | GetServiceQuota | EIP quota | | inline | EIP 한도 |
| S3 | GetBucketLocation, ListBucket, GetObject, PutObject | artifact bucket / `apps/*` | List는 prefix `apps/` | inline | 아티팩트 게시 |

### ApplicationStackExecutionRole (`ApplicationStackExecutionPolicy`)

| AWS Service | Action | Resource | Condition | 사용 목적 |
|---|---|---|---|---|
| SSM | GetParameters | AL2023 public AMI param | | 이미지 ID |
| EC2 | 다수 Describe | `*` | region | CFN handler |
| EC2 | RunInstances | AMI, ENI, SG, 지정 subnet, volume, instance/* | instance는 t3.small | App EC2 생성 |
| EC2 | CreateSecurityGroup / CreateVolume / AllocateAddress / CreateTags / 수명주기 | VPC/AZ/스택 태그로 제한 | | SG, EBS, EIP |
| IAM | Create/Delete/Get/PutRolePolicy 등 | `role/video-analyzer-ec2-stack-ApplicationInstanceRole-*` | | generated runtime Role |
| IAM | Attach/DetachRolePolicy | 같은 Role | PolicyARN=AmazonSSMManagedInstanceCore only | SSM core |
| IAM | InstanceProfile CRUD | `instance-profile/video-analyzer-ec2-stack-ApplicationInstanceProfile-*` | | profile |
| IAM | PassRole | generated ApplicationInstanceRole-* | | EC2에 Role 연결 |

### AwsFaceDetectionKioskRole (6 inline 합산)

| AWS Service | Action | Resource | Policy | 사용 목적 |
|---|---|---|---|---|
| EC2 | Describe* 네트워크/인스턴스 | `*` | ApplicationNetworkPreflightReadOnly | 배포 전 네트워크 확인 (개발) |
| Service Quotas | GetServiceQuota | EIP | 위 | 한도 |
| STS | AssumeRole | `ApplicationDeployerRole` | AssumeApplicationDeployerRole | 배포 Role 전환 |
| S3 | **s3:\*** | frames 버킷 전체 + lambda artifact 버킷 전체 | AwsFaceDetectionDeployS3Policy | 배포/디버그. **runtime 최소권한 아님** |
| CloudFormation | Create/Update/Delete/Describe/Validate/ListStacks | `*` 또는 stack/* | DeployS3 + KioskPolicy | 데이터/앱 스택 배포 이력 |
| Kinesis | **kinesis:\*** | `*` | DeployS3 | 데이터 스택 배포 |
| Kinesis | PutRecord, DescribeStreamSummary | `stream/FrameStream` | KioskPolicy | operator capture |
| DynamoDB | **dynamodb:\*** | `*` | DeployS3 | 데이터 스택 배포 |
| Lambda | **lambda:\*** | `*` | DeployS3 | 데이터 스택 배포 |
| API Gateway | **apigateway:\*** | `*` | DeployS3 | 데이터 스택 배포 |
| API Gateway | GET | `apigateway:ap-northeast-2::*` | KioskPolicy | API key 조회 |
| IAM | Create/Delete/Get/PutRolePolicy 등 | `role/*` | DeployS3 | Lambda Role 관리 |
| IAM | PassRole | `role/*` | PassedToService=lambda | Lambda 배포 |
| Logs | DescribeLogGroups, PutRetentionPolicy | `*` | DeployS3 | Lambda 로그 |
| CloudFormation | DescribeStackResource | `stack/video-analyzer-stack/*` | WebUiConfigRead | /api/config |
| SSM | PutParameter | webui auth 3개 파라미터 | WebUiAuthBootstrap | 로그인 시크릿 기록 |

**Rekognition: 확인된 inline에 Action 없음.**

---

## 13. Rekognition 권한

확인된 live inline 전체에 `rekognition:` 문자열이 **없다.**

| Action | Live AWS Role에 존재? | 어느 Role? | Resource Scope | 현재 필요한가? |
|---|---|---|---|---|
| `CompareFaces` | **확인된 정책 기준 없음** | — | — | STORE ID 비교, legacy RETRIEVE. Local IaC runtime에 필요 |
| `IndexFaces` | 없음 | — | — | 신규 STORE complete |
| `SearchFacesByImage` | 없음 | — | — | 신규 RETRIEVE |
| `DeleteFaces` | 없음 | — | — | RETRIEVED cleanup |
| `DescribeCollection` | 없음 | — | — | 선택적 진단. Local IaC runtime에 있음 |
| `CreateCollection` | 없음 | — | — | Data stack CFN. Runtime 불필요 |
| `DeleteCollection` | 없음 | — | — | Data stack 삭제. Runtime 불필요 |
| `ListCollections` | 없음 | — | — | 배포/진단. Runtime 불필요 |
| `DetectLabels` | 없음 (Lambda Role도 live 없음) | — | — | operator imageprocessor |

`describe-collection` / `list-collections`: 두 caller 모두 AccessDenied. Collection 존재 여부는 **현재 조회 자격증명으로 확인 불가.** 데이터 스택이 DELETE_COMPLETE이므로 Collection이 남아 있을 가능성은 낮지만, 삭제 실패 orphan은 이 자격증명으로 증명하지 못한다.

---

## 14. S3 권한

Live **`AwsFaceDetectionKioskRole`** (`AwsFaceDetectionDeployS3Policy`):

| Prefix / bucket | GetObject | PutObject | DeleteObject | ListBucket |
|---|---|---|---|---|
| `aws-face-detection-lambda-…` (artifact) | s3:* | s3:* | s3:* | s3:* |
| `aws-face-detection-frames-…` **버킷 전체** (`frames/*`, `locker-references/*`, `logging/*` 포함) | s3:* | s3:* | s3:* | s3:* |

HeadObject는 `s3:GetObject`에 포함되는 경우가 많다. 별도 Head 선언은 없다.

Local **IaC `ApplicationInstanceRole`** (미배포):

| Prefix | Get | Put | Delete | 목적 |
|---|---|---|---|---|
| artifact `…/webui|kpi|application/*` | GetObject | — | — | 부트스트랩 |
| `frames/*` | Get/Put/Delete | | | operator + (과거) 프레임 |
| `locker-references/*` | Get/Put/Delete | | | **legacy** RETRIEVE CompareFaces + 기존 객체 cleanup |

신규 Collection STORE는 `reference.jpg`를 만들지 않는다. **이미 STORED인 legacy 거래**는 코드가 여전히 S3 CompareFaces + delete를 쓰므로, runtime에 `locker-references/*` Get/Delete는 **legacy가 남아 있는 동안 필요**하다. Put은 신규 complete에서 호출하지 않는다. Live kiosk Role은 prefix 구분 없이 버킷 전체 `s3:*`이다.

---

## 15. DynamoDB / Kinesis 권한

| 경로 | Live Role | 권한 | 용도 |
|---|---|---|---|
| Kiosk 인증 (FastAPI → Rekognition) | `AwsFaceDetectionKioskRole` | Rekognition 없음. DDB GetItem 전용 권한도 없음 (대신 dynamodb:*) | 키오스크 본인인증은 SQLite + Rekognition. EnrichedFrame은 더 이상 키오스크 경로가 아님 |
| Operator `/api/capture-frame` | 위 Role | `kinesis:PutRecord` on `FrameStream` + 별도 `kinesis:*` | 운영자 파이프라인 |
| Data plane Lambda | 없음 (스택 삭제) | — | imageprocessor/framefetcher/facecompare |

Local IaC runtime은 `kinesis:PutRecord(s)` on named stream, `dynamodb:GetItem` on EnrichedFrame, KPI용 Query/DescribeTable이다. Live kiosk Role은 그보다 넓은 `kinesis:*` / `dynamodb:*` 를 Deploy 정책에 갖고 있다.

---

## 16. EC2 / CloudFormation 권한

| Role | CloudFormation | EC2 mutate |
|---|---|---|
| Deployer | App 스택 ChangeSet + DeleteStack | Describe만 |
| Execution | (CFN이 이 Role로 리소스 API 호출) | RunInstances, volume, SG, EIP, terminate(스택 태그) |
| Kiosk | Create/Update/DeleteStack `*` 포함 | Describe만 (inline 기준) |

두 배포 Role의 목적은 섞이면 안 된다.

- **Deployer:** 사람이/개발 인스턴스가 ChangeSet을 만들고 Execution Role을 Pass.
- **Execution:** CloudFormation이 EC2/IAM 리소스를 실제로 만듦.

---

## 17. IAM PassRole 관계

```text
EC2 (AwsFaceDetectionKioskRole)
    -- sts:AssumeRole --> ApplicationDeployerRole
                              -- iam:PassRole --> ApplicationStackExecutionRole
                                                    (PassedToService=cloudformation)

CloudFormation (ApplicationStackExecutionRole)
    -- iam:PassRole --> video-analyzer-ec2-stack-ApplicationInstanceRole-*
                        (현재 대상 Role 없음)

AwsFaceDetectionKioskRole
    -- iam:PassRole --> arn:aws:iam::115019372648:role/*
                        (PassedToService=lambda.amazonaws.com)
```

---

## 18. Resource Wildcard 검사

| Role | Policy | Action | Resource | `*` 이유 | 축소 가능? |
|---|---|---|---|---|---|
| Deployer | ApplicationDeployerPolicy | cloudformation:ValidateTemplate | `*` | ValidateTemplate은 스택 ARN 없이 호출되는 경우가 많음 | 제한 어려움. 허용 범위는 좁은 편 |
| Deployer | 같은 | ec2:Describe* | `*` | Describe는 resource-level 미지원이 많음 | region condition 있음 |
| Execution | ApplicationStackExecutionPolicy | ec2:Describe* | `*` | CFN handler | region condition |
| Kiosk | ApplicationNetworkPreflightReadOnly | ec2:Describe* | `*` | Describe | 가능하면 region condition 추가 |
| Kiosk | AwsFaceDetectionKioskPolicy | cloudformation:ListStacks / ValidateTemplate | `*` | ListStacks는 `*` 필요 | Describe는 stack ARN으로 이미 별도 문 있음 |
| Kiosk | AwsFaceDetectionDeployS3Policy | cloudformation:Create/Update/DeleteStack 등 | `*` | 과거 데이터 스택 배포 | **축소 필요 (광범위)** |
| Kiosk | 같은 | kinesis:\* dynamodb:\* lambda:\* apigateway:\* | `*` | 과거 데이터 스택 배포 | **축소 필요** |
| Kiosk | 같은 | logs Describe/PutRetention | `*` | 배포 | 로그 그룹 ARN으로 축소 가능 |

**광범위 Service Wildcard Action (`ec2:*`, `s3:*`, `rekognition:*`, `iam:*`, `cloudformation:*`):**

- `s3:*` — **있음** (`AwsFaceDetectionKioskRole` / DeployS3, 두 버킷)
- `kinesis:*`, `dynamodb:*`, `lambda:*`, `apigateway:*` — **있음** (같은 정책, Resource `*`)
- `rekognition:*` — **확인된 정책에 없음**
- `iam:*` — 없음 (다만 `role/*` 에 다수 IAM 액션)
- `ec2:*` / `cloudformation:*` — 단일 `*:*` 형태는 없으나 CFN mutate가 Resource `*`

`CompareFaces`의 Resource `*`는 **live에 없음**. Local IaC에만 있고, AWS가 CompareFaces resource-level을 지원하지 않아 IaC에서 `*`를 쓰는 것은 타당하다.

---

## 19. Local IaC vs Live AWS 비교

A = 실제 AWS에서 읽은 inline  
B = 현재 repo (`aws-infra/aws-infra-ec2-cfn.yaml` + `docs/iam/`)

| Permission | Live AWS | Local IaC | 상태 |
|---|---|---|---|
| Deployer ChangeSet + PassRole(Execution) | ✅ inline | ✅ docs/iam | DIFFERENT_SCOPE (Sid/DeleteStack 차이) |
| Execution EC2/IAM generated runtime | ✅ inline | ✅ docs/iam | DIFFERENT_SCOPE (RunInstances 분할, snapshot live-only) |
| ApplicationInstanceRole 존재 | ❌ 스택 없음 | ✅ 템플릿 | LOCAL_ONLY (미배포) |
| AmazonSSMManagedInstanceCore on runtime | 확인 불가 | ✅ 템플릿 | NOT_VERIFIED |
| CompareFaces on runtime | ❌ 확인된 정책 없음 | ✅ ApplicationInstanceRole | LOCAL_ONLY / 배포 필요 |
| IndexFaces | ❌ | ✅ collection ARN | LOCAL_ONLY / 배포 필요 |
| SearchFacesByImage | ❌ | ✅ collection ARN | LOCAL_ONLY / 배포 필요 |
| DeleteFaces | ❌ | ✅ collection ARN | LOCAL_ONLY / 배포 필요 |
| DescribeCollection (runtime) | ❌ | ✅ collection ARN | LOCAL_ONLY |
| CreateCollection / DeleteCollection | ❌ | ✅ Data stack resource + `docs/iam/data-stack-rekognition-collection-policy.json` (배포 principal용, live Role에 미부착) | LOCAL_ONLY |
| locker-references Get/Put/Delete | ✅ (버킷 전체 s3:*) | ✅ prefix 한정 | DIFFERENT_SCOPE |
| Kinesis PutRecord FrameStream | ✅ | ✅ | MATCH (live는 추가로 kinesis:*) |
| Rekognition Collection resource | 확인 불가 / 스택 없음 | ✅ Data CFN | LOCAL_ONLY |
| DetectLabels on imageprocessor Role | Role 없음 | ✅ Data CFN | LOCAL_ONLY |
| Kiosk Role s3:* / lambda:* / iam role/* | ✅ live | ❌ App 템플릿에 없음 | LIVE_ONLY (개발 인스턴스 전용) |

---

## 20. Collection 배포 준비 상태

**Application Runtime (지금 돌아가는 `AwsFaceDetectionKioskRole`)**

IndexFaces / SearchFacesByImage / DeleteFaces / CompareFaces: **확인된 정책에 없음.**  
이 인스턴스에서 현재 코드의 Collection STORE/RETRIEVE를 그대로 실행하면 **AccessDenied 가능성이 높다.**

IaC가 만들 예정인 `ApplicationInstanceRole`에도 **아직 배포되지 않았으므로** live에 그 권한은 없다.

**Data Stack deployment principal**

CreateCollection / DeleteCollection / DescribeCollection / ListCollections:  
확인된 세 Role 어디에도 없다. `docs/iam/data-stack-rekognition-collection-policy.json`은 repo에만 있다.  
누가 데이터 스택을 배포할지는 live Role로 고정되어 있지 않다. 과거에는 `AwsFaceDetectionKioskRole`의 광범위 배포 정책이 데이터 스택을 만들었을 수 있으나, 그 정책에도 **rekognition:CreateCollection은 없다.**

**CloudFormation**

Local Data template에 `AWS::Rekognition::Collection`이 있다.  
`video-analyzer-stack`은 DELETE_COMPLETE. Collection 실체는 describe 불가로 미확인.

---

## 21. Role Permission Matrix

| Role | 목적 | Trust Principal | Managed (확인) | Inline | 주요 서비스 |
|---|---|---|---|---|---|
| ApplicationDeployerRole | App 스택 ChangeSet 배포 | AwsFaceDetectionKioskRole | 확인 불가 | ApplicationDeployerPolicy | CFN, S3 artifact, EC2 describe, PassRole |
| ApplicationStackExecutionRole | CFN이 App 리소스 생성 | cloudformation.amazonaws.com | 확인 불가 | ApplicationStackExecutionPolicy | EC2, IAM generated runtime, SSM AMI |
| AwsFaceDetectionKioskRole | 현재 개발 EC2 runtime + 구 배포 | ec2.amazonaws.com | 확인 불가 | 6개 | S3/CFN/Kinesis/DDB/Lambda/APIGW/IAM 광범위 + STS Assume Deployer |

| Role | CompareFaces | IndexFaces | SearchFacesByImage | DeleteFaces | S3 | DynamoDB | Kinesis | EC2 | IAM |
|---|---|---|---|---|---|---|---|---|---|
| ApplicationDeployerRole | ❌ | ❌ | ❌ | ❌ | artifact apps/* | ❌ | ❌ | Describe | PassRole Execution만 |
| ApplicationStackExecutionRole | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | Run/mutate 스택 리소스 | generated InstanceRole + PassRole |
| AwsFaceDetectionKioskRole | ❌ 확인분 | ❌ | ❌ | ❌ | 버킷 전체 s3:* | dynamodb:* | PutRecord + kinesis:* | Describe | role/* 관리 + PassRole lambda |
| ApplicationInstanceRole (IaC only) | ✅ `*` | ✅ collection | ✅ collection | ✅ collection | frames + locker-references + artifact Get | GetItem + KPI Query | PutRecord(s) | ❌ | ❌ |

평가:

| Role | 평가 |
|---|---|
| ApplicationDeployerRole | 정상(배포 범위는 좁음). Collection 생성 권한 없음(의도됨). |
| ApplicationStackExecutionRole | 정상(App 스택). Collection/Rekognition 없음(Data 스택 몫). |
| AwsFaceDetectionKioskRole | **광범위 권한** (배포용 wildcard) + **현재 코드 대비 부족** (Rekognition 전부). 개발 인스턴스와 키오스크 runtime이 같은 Role. |
| ApplicationInstanceRole | live 없음. Local IaC는 Collection runtime 권한을 정의함. |

---

## 22. 현재 부족한 권한

신규 Collection 코드를 **지금 이 EC2 Role** 또는 **아직 안 올린 ApplicationInstanceRole**에서 돌리려면:

반드시 필요 (없으면 기능 실패):

- Runtime: `rekognition:CompareFaces` (Resource `*`, API 제약)
- Runtime: `rekognition:IndexFaces` on collection ARN
- Runtime: `rekognition:SearchFacesByImage` on collection ARN
- Runtime: `rekognition:DeleteFaces` on collection ARN
- Runtime: legacy `s3:GetObject`/`DeleteObject` on `locker-references/*` (기존 거래)
- Data 배포 principal: `CreateCollection` / `DeleteCollection` / `DescribeCollection` / `ListCollections` (스택이 Collection을 만들 때)
- Data 스택 자체와 Collection 리소스 배포

선택적:

- Runtime `DescribeCollection`
- Collection 존재 여부 조회용 ListCollections (runtime 불필요)

확인 불가:

- Managed policy에 Rekognition이 숨어 있는지 (ListAttached 거부)

---

## 23. 현재 불필요하거나 과도한 권한

`AwsFaceDetectionKioskRole` (개발 인스턴스):

- `s3:*` 프레임/아티팩트 버킷 전체
- `kinesis:*` `dynamodb:*` `lambda:*` `apigateway:*` Resource `*`
- `cloudformation:CreateStack/UpdateStack/DeleteStack` Resource `*`
- `iam:PutRolePolicy` 등 `role/*`
- `iam:PassRole` `role/*` → Lambda

키오스크 FastAPI runtime에는 위 항목이 필요 없다.  
Runtime에 **CreateCollection / DeleteCollection을 줄 필요는 없다.**

---

## 24. 배포 전 필요한 IAM 변경사항

변경은 하지 않는다. 목록만.

### 반드시 필요

1. Data stack 배포: Collection resource + 배포 principal에 Create/Delete/Describe/List Collection.
2. Application stack 배포: generated `ApplicationInstanceRole`에 local 템플릿의 `application-rekognition-compare` + `application-rekognition-collection`.
3. 그 Role을 **키오스크 FastAPI가 도는 EC2**에 instance profile로 연결. 지금은 `AwsFaceDetectionKioskRole`이다.
4. Legacy 거래가 남아 있으면 runtime에 `locker-references/*` Get/Delete 유지.

### 선택적

- Runtime DescribeCollection
- Deployer/Execution 문서를 live Sid에 맞춰 동기화

### 불필요 (Runtime에 주지 말 것)

- CreateCollection / DeleteCollection
- rekognition:* on `*`
- 데이터 스택용 s3:*/lambda:*/iam role/*

`ApplicationStackExecutionRole`에 Rekognition Collection 권한을 넣을 필요는 없다. Collection은 Data stack ownership이다.

---

## 25. 핵심 요약

Live에 확인된 Role은 세 개다. Application/Data CloudFormation 스택은 둘 다 없다. 현재 EC2는 `AwsFaceDetectionKioskRole`을 쓴다. 그 Role은 배포용으로 매우 넓지만, **확인된 정책에 Rekognition이 전혀 없다.** Local IaC는 아직 배포되지 않은 `ApplicationInstanceRole`에 CompareFaces + Collection API를 정의한다. Collection workflow를 지금 live IAM 그대로 실행하면 실패할 가능성이 높다.

### 마지막 질문

1. **ApplicationDeployerRole은 현재 무엇을 할 수 있는가?**  
   `video-analyzer-ec2-stack` ChangeSet 생성/실행/삭제와 artifact `apps/*` 업로드, Execution Role PassRole.

2. **ApplicationStackExecutionRole은 현재 무엇을 할 수 있는가?**  
   CloudFormation이 App EC2/EBS/EIP/SG와 generated InstanceRole/Profile을 만들고 지울 수 있다. Rekognition은 없다.

3. **실제 Application EC2가 사용하는 IAM Role 이름은 무엇인가?**  
   **`AwsFaceDetectionKioskRole`** (`i-060ed51daa9abc84f`). IaC `ApplicationInstanceRole`은 live에 없다.

4. **Application Runtime Role에 현재 CompareFaces 권한이 있는가?**  
   확인된 inline 기준 **없다.** Managed는 확인 불가.

5. **IndexFaces 권한이 있는가?**  
   **없다** (확인분).

6. **SearchFacesByImage 권한이 있는가?**  
   **없다** (확인분).

7. **DeleteFaces 권한이 있는가?**  
   **없다** (확인분).

8. **Collection 생성 권한은 어느 Role에 있어야 하는가?**  
   Data stack을 배포하는 principal / 그 스택 execution role. Runtime EC2 Role이 아님.

9. **Application Runtime Role에 CreateCollection 권한이 필요한가?**  
   **아니오.**

10. **현재 S3 locker-references 권한은 무엇인가?**  
    Live kiosk Role은 frames 버킷 전체 `s3:*` (locker-references 포함). Prefix 한정 정책은 없다.

11. **신규 Collection workflow를 배포했을 때 현재 IAM 그대로 실행 가능한가?**  
    **아니오.**

12. **아니라면 정확히 어떤 권한이 추가되어야 하는가?**  
    Runtime: CompareFaces, IndexFaces, SearchFacesByImage, DeleteFaces (collection ARN). Data 배포: Create/Delete/Describe/List Collection. 그리고 Application/Data 스택 자체 배포.

13. **local IaC와 live AWS IAM 사이에 차이가 있는가?**  
    **있다.** Runtime Role 이름부터 다르고, Collection/CompareFaces는 IaC-only이며, live kiosk Role은 배포 wildcard가 있다.

14. **가장 큰 IAM 보안상 주의점은 무엇인가?**  
    `AwsFaceDetectionKioskRole`이 개발 인스턴스에 `s3:*` / `lambda:*` / `dynamodb:*` / `iam` on `role/*` / CFN DeleteStack을 갖고, 동시에 키오스크 runtime과 같은 Role이라는 점. Rekognition 부족보다 이 결합이 더 넓다.
