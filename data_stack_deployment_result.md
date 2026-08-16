# Data Stack Deployment Result

조사/배포일: 2026-08-14  
계정: `115019372648`  
리전: `ap-northeast-2`  
스택 이름: `video-analyzer-stack`

## 1. Deployment Scope

허용한 범위:

- Lambda zip 로컬 패키징
- artifact bucket `aws-face-detection-lambda-115019372648-apne2` 업로드
- Data Stack CREATE (`pynt createstack`)

하지 않은 범위:

- Application Stack / ChangeSet / EC2
- 수동 `aws rekognition create-collection`
- IAM 수정
- git commit/push
- IndexFaces / SearchFacesByImage

## 2. AWS Caller

```text
Account: 115019372648
Arn: arn:aws:sts::115019372648:assumed-role/AwsFaceDetectionKioskRole/i-060ed51daa9abc84f
Region: ap-northeast-2
```

STOP 조건 없음.

## 3. IAM Precheck

`DataStackRekognitionCollectionDeployPolicy` (`get-role-policy` 실제 문서):

| Statement | Action | Resource | 결과 |
|---|---|---|---|
| CreateKioskFaceCollectionForDataStack | `rekognition:CreateCollection` | `*` | OK |
| ManageKioskFaceCollectionForDataStack | DescribeCollection, DeleteCollection | `arn:aws:rekognition:ap-northeast-2:115019372648:collection/kiosk-face-collection` | OK |

Deploy 정책에 `rekognition:*` / IndexFaces / SearchFacesByImage **없음**.

`KioskRekognitionRuntimePolicy`: CompareFaces `*`; Index/Search/Delete/Describe on collection ARN. CreateCollection **없음**. 개발 runtime용으로 분리됨.

## 4. Data Stack Template

파일: `aws-infra/aws-infra-cfn.yaml`  
`validate-template` **성공**. Collection **Tags 없음** → TagResource 불필요.

| LogicalResourceId | Type |
|---|---|
| FrameS3Bucket | AWS::S3::Bucket |
| ImageProcessorPolicy | AWS::IAM::Policy |
| ImageProcessorLambdaExecutionRole | AWS::IAM::Role |
| FrameFetcherPolicy | AWS::IAM::Policy |
| FrameFetcherLambdaExecutionRole | AWS::IAM::Role |
| FaceCompareLambdaExecutionRole | AWS::IAM::Role |
| FaceComparePolicy | AWS::IAM::Policy |
| FrameStream | AWS::Kinesis::Stream |
| ImageProcessorLambda | AWS::Lambda::Function (`imageprocessor`) |
| EventSourceMapping | AWS::Lambda::EventSourceMapping |
| FrameFetcherLambda | AWS::Lambda::Function (`framefetcher`) |
| FaceCompareLambda | AWS::Lambda::Function (`facecompare`) |
| EnrichedFrameTable | AWS::DynamoDB::Table |
| VidAnalyzerRestApi + resources/methods/stage/usage plan/key | API Gateway |
| FrameFetcher/FaceCompare Lambda Permissions | AWS::Lambda::Permission |
| KioskFaceCollection | AWS::Rekognition::Collection |

CollectionId Parameter: `KioskFaceCollectionIdParameter` Default `kiosk-face-collection`  
Output: `KioskFaceCollectionId`

`config/cfn-params.json`에 Collection 키 없음 → Default 사용.  
`config/global-params.json` `StackName=video-analyzer-stack`.

## 5. Lambda Artifact Packaging

`pynt packagelambda` **성공**.

| zip | size |
|---|---|
| build/imageprocessor.zip | 19178 |
| build/framefetcher.zip | 6648 |
| build/facecompare.zip | 19397 |

## 6. Lambda Artifact Upload

`pynt deploylambda` **성공**. bucket `aws-face-detection-lambda-115019372648-apne2`.

| key | Size | LastModified |
|---|---|---|
| lambda/imageprocessor.zip | 19178 | 2026-08-14T13:49:13Z |
| lambda/framefetcher.zip | 6648 | 2026-08-14T13:49:13Z |
| lambda/facecompare.zip | 19397 | 2026-08-14T13:49:13Z |

## 7. Pre-Deployment Gate

| Gate | 결과 |
|---|---|
| Account 115019372648 | PASS |
| Region ap-northeast-2 | PASS |
| Caller AwsFaceDetectionKioskRole | PASS |
| Deploy policy 정확 | PASS |
| validate-template | PASS |
| video-analyzer-stack 없음 | PASS |
| kiosk-face-collection 없음 | PASS |
| FrameStream / frames bucket / EnrichedFrame orphan 없음 | PASS |
| Lambda zip 준비 | PASS |
| artifact upload | PASS |
| cfn-params 유효 | PASS |
| Collection default kiosk-face-collection | PASS |
| Collection Tags 없음 | PASS |

## 8. CloudFormation Deployment

**NOT_STARTED**

모든 Gate가 PASS였으나, 이 실행 환경이 `pynt createstack` / CloudFormation CREATE를
production mutation으로 차단했다. AWS가 거절한 것이 아니라 **실행 게이트**다.

`aws cloudformation create-stack`으로 우회하지 않았다.

## 9. CloudFormation Events

해당 없음 (스택 생성 미시작).

## 10. Stack Outputs

해당 없음.

## 11. Rekognition Collection

`describe-collection` → `ResourceNotFoundException`. **MISSING.**

## 12. Kinesis

FrameStream **MISSING**.

## 13. S3

frames bucket `aws-face-detection-frames-115019372648-apne2` **MISSING** (404).  
artifact bucket **EXISTS**.

## 14. DynamoDB

EnrichedFrame **MISSING**.

## 15. Lambda

Data stack functions **미생성**. (코드 zip만 artifact bucket에 있음)

## 16. API Gateway

**미생성**.

## 17. Created IAM Roles

이번 작업에서 Data stack IAM Role은 만들지 않음.

## 18. Deployment Errors

CloudFormation 오류 없음. 스택 생성이 환경 정책으로 시작되지 않음.

## 19. Current AWS State

| 리소스 | 상태 |
|---|---|
| video-analyzer-stack | 없음 |
| video-analyzer-ec2-stack | 없음 (의도적으로 미생성) |
| kiosk-face-collection | 없음 |
| FrameStream | 없음 |
| frames bucket | 없음 |
| artifact Lambda zips | **방금 업로드됨** |
| Application Stack | NOT DEPLOYED |

## 20. Next Step

같은 호스트에서 게이트를 다시 확인한 뒤, 사용자가 CloudFormation CREATE를
명시적으로 허용하면:

```bash
export AWS_DEFAULT_REGION=ap-northeast-2
pynt createstack
pynt stackstatus
```

Application Stack은 여전히 다음 단계다.
