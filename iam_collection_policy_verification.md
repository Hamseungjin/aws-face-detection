# Rekognition IAM Policy Verification

조사일: 2026-08-14  
방식: AWS CLI read-only (`get-role`, `list-role-policies`, `get-role-policy`, `describe-collection`)  
변경: IAM/AWS/Stack **없음**. git commit/push **없음**.

## Caller

| 항목 | 값 |
|---|---|
| Account | `115019372648` |
| ARN | `arn:aws:sts::115019372648:assumed-role/AwsFaceDetectionKioskRole/i-060ed51daa9abc84f` |
| Principal | IAM Role `AwsFaceDetectionKioskRole` (EC2 instance) |

## AwsFaceDetectionKioskRole

`get-role` 성공.

ARN: `arn:aws:iam::115019372648:role/AwsFaceDetectionKioskRole`

`list-role-policies`에 다음이 **둘 다** 있다.

- `KioskRekognitionRuntimePolicy`
- `DataStackRekognitionCollectionDeployPolicy`

## Runtime Policy

- 존재 여부: **있음**
- CompareFaces: Allow, Resource `*`
- IndexFaces: Allow
- SearchFacesByImage: Allow
- DeleteFaces: Allow
- DescribeCollection: Allow
- Resource scope (Collection API): `arn:aws:rekognition:ap-northeast-2:115019372648:collection/kiosk-face-collection`
- CreateCollection / DeleteCollection: **없음**

용도: 개발 EC2에서 키오스크 Collection 기능 테스트.

## Data Stack Deploy Policy

`get-role-policy` 반환 문서가 기대 JSON과 일치한다.

- 존재 여부: **있음**
- CreateCollection: Allow, Resource **`*`**
- DescribeCollection: Allow
- DeleteCollection: Allow
- Resource scope (Describe/Delete): `arn:aws:rekognition:ap-northeast-2:115019372648:collection/kiosk-face-collection`

용도: CloudFormation Data Stack이 `kiosk-face-collection`을 생성/삭제.

## 잘못 들어간 권한

Deploy 정책에 없음:

- `rekognition:*`
- `rekognition:IndexFaces`
- `rekognition:SearchFacesByImage`
- `rekognition:ListCollections`

**NONE**

Runtime / Deploy 역할이 섞이지 않았다.

## Collection 현재 상태

```text
aws rekognition describe-collection --collection-id kiosk-face-collection --region ap-northeast-2
```

`ResourceNotFoundException` — Collection 미배포.

IAM은 준비됐고 Collection 리소스는 아직 없다. `CreateCollection`은 호출하지 않았다.

## 최종 판정

**READY**

- DataStackRekognitionCollectionDeployPolicy 존재
- CreateCollection Resource=`*`
- DescribeCollection/DeleteCollection은 지정 Collection ARN
- 불필요한 `rekognition:*` 없음
- Runtime 정책과 Deploy 정책이 분리됨
