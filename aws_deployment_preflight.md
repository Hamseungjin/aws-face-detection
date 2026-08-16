# AWS Deployment Preflight

조사일: 2026-08-14  
계정: `115019372648`  
리전: `ap-northeast-2`  
목표 Collection ID: `kiosk-face-collection`  
방식: local working tree + AWS **read-only**  
실행하지 않음: stack create/update, Collection 생성, IAM 변경, 유료 Rekognition Index/Search/Delete, git commit/push

**전체 배포 판단: BLOCKED**

---

## 1. 목적

Collection + FaceId 키오스크를 실제 배포하기 전에, 코드·IAM·AWS 리소스가 맞는지 확인하고 부족한 것만 적는다. “아마 될 것”은 쓰지 않는다.

---

## 2. 현재 Local 상태

Branch: `devhsj`. commit/push 없음. working tree에 Quality Gate, locker-first STORE, Collection/FaceId, IaC Collection resource가 있다.

확인된 코드 사실:

| 항목 | 근거 |
|---|---|
| OpenCV Quality Gate | `web-ui/backend/face_quality.py`, STORE/RETRIEVE에서 `evaluate()` |
| Locker-first | `kiosk.js` `MODE_SELECT` → `LOCKER_SELECT` |
| Atomic hold | `KioskStore.reserve_locker` `UPDATE … WHERE status='AVAILABLE'` |
| IndexFaces at complete only | `app.py` `kiosk_store_complete` |
| 신규 `save_reference_face` 호출 | `app.py`에 **없음** |
| Search + expected FaceId | `face_collection.match_expected_face` |
| DeleteFaces after RETRIEVED | `app.py` `_cleanup_retrieved_biometrics` |
| Legacy S3 fallback | `stored_auth_mode` → `legacy_s3` |
| Face Liveness | 코드에 없음 |

---

## 3. 현재 Live AWS 상태

Caller (default): `assumed-role/AwsFaceDetectionKioskRole/i-060ed51daa9abc84f`

| 리소스 | Live |
|---|---|
| `video-analyzer-stack` | **없음** (`ValidationError` does not exist; 과거 DELETE_COMPLETE) |
| `video-analyzer-ec2-stack` | **없음** |
| Collection `kiosk-face-collection` | **없음** (`ResourceNotFoundException`) |
| Kinesis `FrameStream` | **없음** (`ResourceNotFoundException`) |
| S3 `aws-face-detection-lambda-115019372648-apne2` | **존재** (artifact; Lambda zip head 성공) |
| S3 `aws-face-detection-frames-115019372648-apne2` | **없음** (`head-bucket` 404) |
| VPC `vpc-05311e6f3180243c0` | available |
| Subnet `subnet-06563af7aa7d98464` | available, AZ `ap-northeast-2c` |
| EC2 `i-060ed51daa9abc84f` | running, profile `AwsFaceDetectionKioskRole` |

---

## 4. 현재 IAM 상태

확인된 Role:

| Role | 역할 |
|---|---|
| `AwsFaceDetectionKioskRole` | 현재 개발 EC2 runtime + 과거 Data 스택 배포 자격증명 |
| `ApplicationDeployerRole` | Application ChangeSet 전용 |
| `ApplicationStackExecutionRole` | Application CFN execution (EC2/EBS/EIP/SG/generated InstanceRole) |
| `ApplicationInstanceRole` | **live 없음** (스택 generated) |

`AwsFaceDetectionKioskRole` inline (`list-role-policies`):

- 기존 6개 + **`KioskRekognitionRuntimePolicy` 확인됨** (`get-role-policy` 성공)
- CompareFaces `*`, Index/Search/Delete/Describe on `collection/kiosk-face-collection`
- CreateCollection **없음** (의도대로)

고객 관리형 `AwsFaceDetectionKioskRolePolicy`: `ListAttachedRolePolicies` / `GetPolicy` AccessDenied → 문서에 제공된 JSON은 **live에서 재확인 불가 (NOT_VERIFIED)**. 내용은 사용자 제공과 모순되지 않으나 이번 자격증명으로 증명하지 못함.

`ApplicationDeployerRole` / `ApplicationStackExecutionRole`: Rekognition Collection 생성 권한 **없음**. 추가하지 말 것.

---

## 5. Data Stack 구조

파일: `aws-infra/aws-infra-cfn.yaml`  
배포 이름: `config/global-params.json` → `StackName=video-analyzer-stack`

생성 대상: Kinesis FrameStream, frames S3 버킷(파라미터 이름), DynamoDB, imageprocessor/framefetcher/facecompare Lambda, API Gateway, **KioskFaceCollection**.

`config/cfn-params.json`에는 `KioskFaceCollectionIdParameter` 키가 **없다**. `pynt createstack`은 파일에 있는 파라미터만 넘긴다. 템플릿 Default가 쓰인다.

---

## 6. Rekognition Collection Resource

Local 템플릿에서 확인한 값 (추측 아님):

| 항목 | 값 |
|---|---|
| LogicalResourceId | `KioskFaceCollection` |
| Resource Type | `AWS::Rekognition::Collection` |
| CollectionId Parameter | `KioskFaceCollectionIdParameter` |
| 기본값 | `kiosk-face-collection` |
| Output | `KioskFaceCollectionId` = `!Ref KioskFaceCollection` |
| 다른 Stack 자동 Export | **없음** (Export 이름 없음) |
| DeletionPolicy | **없음** (기본 Delete) |
| UpdateReplacePolicy | **없음** |

---

## 7. Data Stack Deployment Principal

코드 근거:

- `build.py` `createstack()` → `boto3.client('cloudformation').create_stack(...)`  
  **`RoleARN` 없음**
- RUNBOOK §3: `export AWS_PROFILE=…` 후 `pynt createstack`
- 이 호스트 default caller = `AwsFaceDetectionKioskRole`
- 그 Role inline `AwsFaceDetectionDeployS3Policy`에 `cloudformation:CreateStack/UpdateStack/DeleteStack` Resource `*`

**분류: A**

`AwsFaceDetectionKioskRole` 자격증명으로 CloudFormation이 **별도 execution role 없이** Data Stack 리소스를 생성한다.

B/C (별도 Data Deployer / Data Execution Role)는 repo에 Role 이름·PassRole·RoleARN이 없다.

Application 스택의 ChangeSet+Execution Role 경로와 **섞지 말 것**.

---

## 8. Data Stack에 필요한 IAM

CFN `AWS::Rekognition::Collection` 기준 (이 principal = `AwsFaceDetectionKioskRole`):

| Action | CFN 생성 | CFN 삭제 | 비고 |
|---|---|---|---|
| `CreateCollection` | **필수** | — | resource 생성 |
| `DeleteCollection` | — | **필수** | Collection에 DeletionPolicy 없음 → 스택 삭제 시 호출 |
| `DescribeCollection` | **필요** | **필요** | handler Read/stabilization |
| `ListCollections` | 불필요 | 불필요 | 템플릿/스크립트가 호출하지 않음. 넣지 않음 |

현재 그 Role에 위 세 Action **없음**. Runtime `KioskRekognitionRuntimePolicy`와 **섞지 말 것**.

권장 초안 (아직 적용하지 않음):

정책 이름 예: `DataStackRekognitionCollectionDeployPolicy`  
**추가 대상 Role: `AwsFaceDetectionKioskRole`** (Data 배포 principal)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ManageKioskFaceCollectionForDataStack",
      "Effect": "Allow",
      "Action": [
        "rekognition:CreateCollection",
        "rekognition:DescribeCollection",
        "rekognition:DeleteCollection"
      ],
      "Resource": "arn:aws:rekognition:ap-northeast-2:115019372648:collection/kiosk-face-collection"
    }
  ]
}
```

ApplicationDeployerRole / ApplicationStackExecutionRole에는 넣지 않는다.

---

## 9. Collection 현재 존재 여부

```text
aws rekognition describe-collection --collection-id kiosk-face-collection --region ap-northeast-2
```

결과: **`ResourceNotFoundException` — Collection 없음.**

CreateCollection은 호출하지 않았다.

---

## 10. Application Stack 구조

파일: `aws-infra/aws-infra-ec2-cfn.yaml`  
스택 이름: `video-analyzer-ec2-stack` (build.py `createec2stack`)

전제: Data 스택이 만든 `FrameS3BucketNameParameter` 등.  
`config/ec2-params.json`에 VPC/Subnet/FrameS3/artifact는 채워져 있다.  
`FaceCollectionIdParameter` 키는 **파일에 없음** → 템플릿 Default `kiosk-face-collection`.

---

## 11. Application Runtime Role

IaC: `ApplicationInstanceRole` + `AmazonSSMManagedInstanceCore` + 인라인들.  
Live: **없음**. 배포 후 generated `video-analyzer-ec2-stack-ApplicationInstanceRole-*`.

개발 테스트용 runtime은 `AwsFaceDetectionKioskRole` + `KioskRekognitionRuntimePolicy`.  
최종 Application EC2 runtime으로 간주하지 않는다.

---

## 12. Runtime Rekognition IAM

Local IaC (`ApplicationInstanceRole`):

| Action | Resource |
|---|---|
| `rekognition:CompareFaces` | `*` (API가 resource-level 미지원. 주석 있음) |
| `IndexFaces` | `arn:aws:rekognition:${AWS::Region}:${AWS::AccountId}:collection/${FaceCollectionIdParameter}` |
| `SearchFacesByImage` | 동일 |
| `DeleteFaces` | 동일 |
| `DescribeCollection` | 동일 |

Collection API는 `*`가 아니다. CompareFaces만 `*`.

개발 Role `KioskRekognitionRuntimePolicy`: 같은 범위, **확인됨**. Collection이 없어 Index/Search/Delete는 지금 호출해도 ResourceNotFound.

---

## 13. ApplicationDeployerRole 검증

Live inline으로 확인:

ValidateTemplate, CreateChangeSet(이름 `application-greenfield-create-*`, RoleArn=Execution), DescribeChangeSet, ExecuteChangeSet, stack read, PassRole Execution, artifact Get/Put, DeleteStack.

Application 스택 **ChangeSet 경로**에는 **수정 불필요**.

주의: `build.py` `createec2stack`은 RoleARN 없이 `create_stack`을 호출한다. 그 경로는 Execution Role을 쓰지 않으며, 이 인스턴스 default Role로 App 스택을 올리면 PassRole-to-EC2가 막힐 수 있다. **Application 배포는 ChangeSet + ApplicationDeployerRole을 사용한다.** `pynt createec2stack`을 이 배포의 실행 명령으로 쓰지 않는다.

---

## 14. ApplicationStackExecutionRole 검증

Live inline `ManageApplicationRuntimeRole`이 generated  
`arn:aws:iam::115019372648:role/video-analyzer-ec2-stack-ApplicationInstanceRole-*` 에

`CreateRole`, `PutRolePolicy`, `GetRole`, `GetRolePolicy`, `DeleteRolePolicy`, `DeleteRole` 등을 허용한다.

CFN이 템플릿의 Rekognition inline을 그 Role에 넣을 수 있다.

이 Role에 `IndexFaces` 같은 runtime 권한을 넣을 필요 **없음**.  
**ApplicationStackExecutionRole 수정 불필요.**

---

## 15. Data → Application Collection ID 전달

실제 chain:

```text
Data Parameter KioskFaceCollectionIdParameter (default kiosk-face-collection)
        → Resource KioskFaceCollection
        → Output KioskFaceCollectionId
        → [자동으로 App 파라미터에 넣는 스크립트 없음]
        → Application FaceCollectionIdParameter (default 동일)
        → IAM collection ARN
        → UserData REKOGNITION_FACE_COLLECTION_ID="${FaceCollectionIdParameter}"
        → bootstrap /etc/webui.env
        → config.REKOGNITION_FACE_COLLECTION_ID
```

`config/cfn-params.json` / `config/ec2-params.json` 모두 Collection 키 없음.  
**기본값이 같으면 동작한다.** ID를 바꾸면 수동으로 양쪽을 맞춰야 한다.

---

## 16. Runtime 환경변수

| 변수 | 정의 | 기본값 | `/etc/webui.env` | UserData/bootstrap | 누락 시 |
|---|---|---|---|---|---|
| `AWS_REGION` / `AWS_DEFAULT_REGION` | config.py + bootstrap | IMDSv2 region | 예 | 예 | boto3 기본 리전 의존 |
| `REKOGNITION_FACE_COLLECTION_ID` | config.py | **빈 문자열** | 예 (`${REKOGNITION_FACE_COLLECTION_ID}`, bootstrap 기본 `kiosk-face-collection`) | 예 | complete 502 `FACE_COLLECTION_NOT_CONFIGURED`. 신규 S3 fallback 없음 |
| `REKOGNITION_SEARCH_MAX_FACES` | config.py | 5 | **아니오** | 아니오 | 기본 5 |
| `FACE_SIMILARITY_THRESHOLD` | config.py | 90 | 예 | bootstrap 기본 90 | 기본 90 |
| `FACE_QUALITY_ALLOW_UNTRUSTED_DETECTOR` | config.py | false | 아니오 | 아니오 | YuNet 없으면 `QUALITY_DETECTOR_UNAVAILABLE` |
| `FACE_DETECTOR_MODEL_PATH` | config.py | 빈 값 | 아니오 | 아니오 | `web-ui/backend/models/*.onnx` 탐색 |

---

## 17. SQLite Migration

`KioskStore.initialize()` → `_migrate_transaction_columns()`  
`ALTER TABLE … ADD COLUMN` for:

`rekognition_collection_id`, `rekognition_face_id`, `face_indexed_at`, `face_deleted_at`  
(기존 S3 컬럼도 유지, DROP 없음)

`app.py` import 시 `KioskStore(...)`가 `initialize()`를 호출한다.

| 상황 | 동작 |
|---|---|
| 새 `/data/kiosk/kiosk.db` | CREATE TABLE + migrate |
| 기존 EBS DB | ADD COLUMN IF 없음 |
| ALTER 실패 | `initialize` 예외 → FastAPI 기동 실패 |

기존 컬럼/행을 지우지 않으므로 legacy retrieve는 스키마상 유지된다.

---

## 18. Legacy S3 Compatibility

신규 complete는 `save_reference_face`를 호출하지 않는다.

Legacy retrieve: `stored_auth_mode==legacy_s3` → `head_object` + CompareFaces + RETRIEVED 후 `delete_object`.

IaC `application-reference-face-s3`:

- `frames/*` Get/Put/Delete — operator capture 파이프라인 (키오스크 인증과 별개)
- `locker-references/*` Get/Put/Delete — **legacy** Get/Delete 필요. Put은 신규 STORE에서 안 씀. 템플릿에 Put이 남아 있음 (최소화를 더 할 수는 있으나 이번 preflight에서 템플릿 수정 없음)

`s3:ListBucket`은 이 인라인에 없다. `head_object`/`get_object`/`delete_object`만 사용.

---

## 19. Kinesis / DynamoDB / Operator Pipeline

| 권한 (IaC runtime) | 용도 |
|---|---|
| `kinesis:PutRecord(s)` FrameStream | operator `/api/capture-frame` |
| `dynamodb:GetItem` EnrichedFrame | operator/레거시 frame lookup |
| `dynamodb:Query/DescribeTable` + Logs/CW | KPI |
| `s3 frames/*` | operator 프레임 |
| Rekognition Collection | **kiosk 인증** |
| CompareFaces | kiosk STORE + legacy retrieve |

키오스크 본인인증은 Kinesis/DynamoDB를 타지 않는다.

---

## 20. OpenCV YuNet Deployment

- 공식 OpenCV Zoo `face_detection_yunet_2023mar.onnx`를
  `web-ui/backend/models/`에 두고 SHA-256 검증함. 상세:
  `opencv_yunet_deployment.md`
- `publishapps` webui 패키징은 모델+checksum이 맞아야 진행된다
- local `build/web-ui.tgz`에 `./backend/models/face_detection_yunet_2023mar.onnx` 확인됨
- production: YuNet 없으면 `QUALITY_DETECTOR_UNAVAILABLE` (untrusted fallback 금지)

**OpenCV model: READY** (S3 upload는 이번 작업에서 하지 않음)

---

## 21. External AWS Resource Preflight

| 리소스 | 상태 | 판정 |
|---|---|---|
| VPC / subnet / AZ 2c | 존재 | READY |
| Artifact bucket | 존재, Lambda zip head 성공 | READY |
| Frames bucket | 없음 | Data 스택이 생성. Data 배포 전 App 스택 FrameS3 참조는 파라미터 문자열만 필요 |
| FrameStream | 없음 | Data 스택이 생성 |
| SSM webui auth 3개 | GetParameter AccessDenied | **NOT_VERIFIED**. App bootstrap은 이 파라미터가 필요 |
| Collection | 없음 | Data 스택이 생성 |

---

## 22. CloudFormation Validation

```text
aws cloudformation validate-template --template-body file://aws-infra/aws-infra-cfn.yaml
aws cloudformation validate-template --template-body file://aws-infra/aws-infra-ec2-cfn.yaml
```

둘 다 **성공** (Description 반환). CreateChangeSet는 실행하지 않음.

---

## 23. Test Results

`python3 -m pytest -q tests/test_face_quality.py tests/test_face_collection.py tests/test_app_kiosk.py tests/test_kiosk_store.py tests/test_kiosk_javascript_correlation.py`

**110 passed.** 실제 Rekognition/Collection 호출 없음.

---

## 24. Deployment Blockers

| 항목 | 판정 |
|---|---|
| Data Stack template | READY |
| Collection resource | READY (템플릿) |
| Data deploy IAM | **BLOCKED** (Create/Describe/Delete Collection 없음) |
| Collection 존재 | **BLOCKED** (없음) |
| Application Stack template | READY |
| ApplicationDeployerRole | READY (ChangeSet 경로) |
| ApplicationStackExecutionRole | READY |
| ApplicationInstanceRole runtime IAM | READY (IaC). live Role 없음 |
| Dev EC2 KioskRekognitionRuntimePolicy | READY (개발 테스트용) |
| Collection ID 전달 | WARNING (자동 전달 없음, 기본값 일치) |
| Runtime env | READY (bootstrap가 ID를 씀) |
| SQLite migration | READY |
| OpenCV YuNet | READY |
| Artifact bucket | READY |
| VPC/Subnet | READY |
| Legacy S3 | READY (코드+IaC Get/Delete) |
| Tests | READY (PASS) |
| Data/App 스택 live | **BLOCKED** (둘 다 없음) |
| SSM auth params | NOT_VERIFIED |

---

## 25. 필요한 IAM 변경

실행하지 않음.

**STEP A** — `AwsFaceDetectionKioskRole`에 **배포 전용** `DataStackRekognitionCollectionDeployPolicy` (§8 JSON). Runtime 정책과 분리.

**STEP B** — Data Stack 생성 → Collection 생성.

**STEP C** — Application ChangeSet (Deployer + Execution) → generated InstanceRole에 템플릿 Rekognition 인라인 적용.

`KioskRekognitionRuntimePolicy`는 개발 EC2 테스트용으로만 남긴다.

광범위 개발 Role(`s3:*` 등) 정리는 **배포 검증 후 별도 TODO**. 이번 작업에서 축소하지 않는다.

---

## 26. 최종 배포 순서

1. STEP A IAM (Collection deploy policy) — 아직 안 함  
2. `pynt packagelambda` + `pynt deploylambda` (artifact zip)  
3. Data Stack `pynt createstack` (default creds = KioskRole, **RoleARN 없음**)  
4. `describe-collection`으로 Collection 확인  
5. YuNet ONNX를 `web-ui/backend/models/`에 두고 `pynt publishapps`  
6. SSM auth 존재 확인 (`setwebuiauth`는 별도 자격증명)  
7. Application ChangeSet create/execute (`RoleARN=ApplicationStackExecutionRole`)  
8. App EC2 `/etc/webui.env` 의 `REKOGNITION_FACE_COLLECTION_ID`  
9. healthz + kiosk smoke  

Data를 먼저 만드는 이유: Collection, frames 버킷, FrameStream, facecompare Lambda를 App/운영자가 참조한다.

---

## 27. 확인 명령어

```bash
aws sts get-caller-identity
aws cloudformation validate-template --template-body file://aws-infra/aws-infra-cfn.yaml --region ap-northeast-2
aws cloudformation validate-template --template-body file://aws-infra/aws-infra-ec2-cfn.yaml --region ap-northeast-2
aws cloudformation describe-stacks --stack-name video-analyzer-stack --region ap-northeast-2
aws cloudformation describe-stacks --stack-name video-analyzer-ec2-stack --region ap-northeast-2
aws rekognition describe-collection --collection-id kiosk-face-collection --region ap-northeast-2
aws iam get-role-policy --role-name AwsFaceDetectionKioskRole --policy-name KioskRekognitionRuntimePolicy
```

---

## 28. 실행 명령어 - 실행 금지

Data (기존 `build.py` / RUNBOOK, RoleARN 없음):

```bash
# 지금 실행하지 말 것
export AWS_DEFAULT_REGION=ap-northeast-2
pynt packagelambda
pynt deploylambda
pynt createstack
pynt stackstatus
```

Application (기존 계획서 ChangeSet 경로. `pynt createec2stack` 대체):

```bash
# 지금 실행하지 말 것
# 1) FaceCollectionIdParameter=kiosk-face-collection 을 ChangeSet 파라미터에 명시 권장
# 2) AWS_PROFILE=application-deployer
# 3) CreateChangeSet name=application-greenfield-create-*
#    RoleARN=arn:aws:iam::115019372648:role/ApplicationStackExecutionRole
# 4) DescribeChangeSet 검토 후 ExecuteChangeSet
```

`pynt createec2stack`은 Execution Role을 넘기지 않으므로 이 배포의 실행 명령으로 쓰지 않는다.

---

## 29. 배포 후 Smoke Test 계획

실행하지 않음.

1. App EC2 instance profile = generated ApplicationInstanceRole  
2. `/etc/webui.env` `REKOGNITION_FACE_COLLECTION_ID=kiosk-face-collection`  
3. DescribeCollection 성공  
4. `/kiosk`  
5. STORE가 locker 선택부터  
6. OpenCV 실패 시 Compare/Index 0  
7. CompareFaces 1회  
8. complete 후 SQLite FaceId  
9. 신규 `locker-references/` Put 없음  
10. SearchFacesByImage 1회  
11. expected FaceId  
12. RETRIEVED  
13. DeleteFaces  
14. `face_deleted_at`  

이미지/base64 로그 금지.

---

## 30. Rollback 계획

실행하지 않음.

- App 스택 실패: Data/Collection 유지 가능. App만 롤백.  
- Data 볼륨: `DeletionPolicy: Snapshot`. 스택 삭제 시 스냅샷.  
- FaceId가 이미 있으면 Collection에 orphan 가능. App `list_pending_face_cleanup` / DeleteFaces. Collection을 지우면 모든 FaceId 삭제 → 활성 STORED retrieve 불가.  
- Legacy S3 거래는 Collection과 무관하게 retrieve 가능.  
- SQLite ADD COLUMN은 하위 호환.  
- Collection 삭제 = 신규 거래 인증 불가. Data 스택 삭제는 DeletionPolicy 없어 Collection도 삭제됨.

---

## 31. 핵심 결론

배포는 **지금 BLOCKED**다.

막는 것:

1. Data principal에 Create/Describe/Delete Collection 없음  
2. Data 스택/Collection/FrameStream/frames 버킷 없음  
3. ~~YuNet 모델이 artifact에 없음~~ → 로컬 artifact 준비 완료 (S3 미업로드)

Application Deployer/Execution과 IaC runtime Rekognition 정의는 ChangeSet 경로 기준으로 준비되어 있다. Collection ID는 Output 자동 연결이 없고 기본값으로 맞춘다.

개발 Role의 `KioskRekognitionRuntimePolicy`는 이 호스트 테스트용일 뿐 ApplicationInstanceRole을 대체하지 않는다.

### 마지막 질문

1. Data Stack 배포 principal은 **`AwsFaceDetectionKioskRole`** (`pynt createstack`, RoleARN 없음).  
2. 그 principal에 **CreateCollection 없음**.  
3. **`AwsFaceDetectionKioskRole`에 배포 전용** Create/Describe/Delete Collection (collection ARN).  
4. ApplicationDeployerRole **수정 불필요**.  
5. ApplicationStackExecutionRole **수정 불필요**.  
6. IaC ApplicationInstanceRole에 Compare/Index/Search/Delete **있음**.  
7. Collection API Resource는 **특정 collection ARN**. CompareFaces만 `*`.  
8. `kiosk-face-collection`은 **현재 AWS에 없음**.  
9. **Data Stack을 먼저** 만들어야 한다.  
10. Output → App Parameter **자동 전달 없음**.  
11. App ChangeSet에 `FaceCollectionIdParameter=kiosk-face-collection` (또는 양쪽 Default 유지).  
12. bootstrap가 UserData 값을 `/etc/webui.env`에 쓴다. 빈 값이면 complete가 `FACE_COLLECTION_NOT_CONFIGURED`.  
13. SQLite ADD COLUMN은 새/기존 DB에 안전. 실패 시 기동 실패.  
14. 신규 complete는 `save_reference_face` 없음.  
15. legacy S3 retrieve 코드 유지.  
16. YuNet은 로컬 `web-ui/backend/models/`와 `build/web-ui.tgz`에 포함됨.  
17. Blocker: Data Collection IAM + Data 스택/Collection 부재 (YuNet은 로컬 READY).  
18. IAM STEP A와 모델 패키징 없이는 **바로 배포 불가**.  
19. Collection deploy IAM → Data stack → Collection 확인 → publishapps(모델 포함) → App ChangeSet.  
20. 첫 smoke: DescribeCollection + `/etc/webui.env` Collection ID + locker-first STORE.
