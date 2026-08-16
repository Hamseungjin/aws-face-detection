# Full Kiosk Reset Result

Local working tree `~/workspace/aws-face-detection` (branch `devhsj`) is
source of truth. Git commit/push were not performed.

## 1. Reset Reason

Development/test reset to a Collection-only kiosk:

Browser → FastAPI → SQLite → OpenCV/YuNet → Rekognition
(CompareFaces, IndexFaces, SearchFacesByImage, DeleteFaces).

The user confirmed current locker contents, SQLite STORED/OCCUPIED rows,
and Collection FaceIds are disposable test data.

## 2. Pre-reset AWS State

| Item | Value |
|---|---|
| Account | `115019372648` |
| Role | `AwsFaceDetectionKioskRole` / `i-060ed51daa9abc84f` |
| Region | `ap-northeast-2` |
| Stack | `video-analyzer-stack` `CREATE_COMPLETE` |
| Resources | 29 (Collection + FrameS3Bucket + Kinesis + DDB + 3 Lambdas + API Gateway) |
| Collection | `kiosk-face-collection` FaceCount=1 |
| Frames bucket | `aws-face-detection-frames-115019372648-apne2` exists, prefixes empty |
| Artifact bucket | `aws-face-detection-lambda-115019372648-apne2` exists |

`pynt deletestack` targets `config/global-params.json` → `video-analyzer-stack`.
`pynt createstack` uses `aws-infra/aws-infra-cfn.yaml` + `config/cfn-params.json`.

**AWS stack delete was blocked by the execution environment.** No
`aws cloudformation delete-stack` workaround was attempted. The live Data
Stack is therefore still the pre-reset stack.

## 3. Pre-reset SQLite State

Path: `web-ui/backend/data/kiosk.sqlite3`

| status | count |
|---|---|
| CANCELLED | 12 |
| EXPIRED | 3 |
| RETRIEVED | 6 |
| STORED | 3 |
| **total** | **24** |

STORED:

| locker | FaceId | S3 key |
|---|---|---|
| 01 | no | yes |
| 02 | no | no |
| 07 | yes | no |

`STORED AND rekognition_face_id IS NULL` = 2  
`STORED AND rekognition_face_id IS NOT NULL` = 1

## 4. DB Backup

Integrity-checked SQLite backup API copy:

`backups/kiosk.sqlite3.pre-reset-20260815-074029`

`PRAGMA integrity_check` = `ok`

Live file was then **moved** (not `rm`):

`web-ui/backend/data/kiosk.sqlite3.pre-reset-20260815-074029`

## 5. Legacy S3 Removal

Removed from kiosk runtime:

- `boto3.client("s3")` in `app.py`
- `save_reference_face` / `compare_reference_to_face_bytes` /
  `reference_object_exists` / `delete_reference_face` /
  `build_reference_face_s3_key`
- `legacy_s3` auth branch
- `FRAME_S3_BUCKET`, `KIOSK_REFERENCE_S3_PREFIX`,
  `KIOSK_REFERENCE_DELETE_ON_RETRIEVE`
- Data Stack `FrameS3Bucket` resource/parameter/output
- App Stack `application-reference-face-s3` IAM
- UserData / bootstrap `FRAME_S3_BUCKET`

Artifact S3 (`aws-face-detection-lambda-115019372648-apne2`, `build.py`
publishapps, userdata `aws s3 cp` of apps) is unchanged.

## 6. Removed Runtime Code

RETRIEVE is Collection-only. STORED without FaceId raises
`HTTP 500 DATA_CONSISTENCY_ERROR`. No S3 fallback, no AWS call, locker stays
closed.

New `complete_store` requires `rekognition_collection_id` +
`rekognition_face_id`. Legacy S3 columns are not written (always NULL).

## 7. Data Stack Before

Live `video-analyzer-stack` (still present):

- `KioskFaceCollection`
- `FrameS3Bucket`
- `FrameStream` (Kinesis)
- `EnrichedFrameTable` (DynamoDB)
- `imageprocessor` / `framefetcher` / `facecompare` (Lambda)
- API Gateway REST API, key, usage plan, stages

## 8. Data Stack After

**Not recreated.** Delete never started.

Local template after this change (what `pynt createstack` would create):

- `KioskFaceCollection` only
- no S3 / Kinesis / DynamoDB / Lambda / API Gateway

`config/cfn-params.json` is now only
`KioskFaceCollectionIdParameter=kiosk-face-collection`.

## 9. Collection

Still the pre-reset collection: `kiosk-face-collection`, FaceCount=1.
A full stack recreate would replace it with FaceCount=0.

## 10. Artifact Bucket Protection

`aws s3api head-bucket aws-face-detection-lambda-115019372648-apne2` succeeded
after the local reset. No artifact objects were deleted.

Protected IAM roles were not modified:
`AwsFaceDetectionKioskRole`, `ApplicationDeployerRole`,
`ApplicationStackExecutionRole`.

## 11. Fresh SQLite

FastAPI started without `--reload` and created:

`web-ui/backend/data/kiosk.sqlite3`

- tables: `lockers`, `transactions`
- `PRAGMA integrity_check` = `ok`
- transactions = 0
- locker seed: 01/02/04/05/07/09/10/12 AVAILABLE, 03/06/11 OCCUPIED (no
  transaction; original demo seed), 08 DISABLED

No old FaceIds or retrieval codes.

## 12. Runtime Environment

Running uvicorn (`0.0.0.0:8080`, no reload):

- `REKOGNITION_FACE_COLLECTION_ID=kiosk-face-collection`
- no `FRAME_S3_BUCKET` / `KIOSK_REFERENCE_*` in process env or `config.py`
- OpenCV `cv2` 5.0.0, `FaceDetectorYN` present
- Quality gate detector `yunet`, `trusted=True`

`/healthz` → `{"status":"ok"}`

## 13. STORE Workflow

Locker hold → ID + FRAME-A → OpenCV YuNet → CompareFaces → payment →
IndexFaces(FRAME-A) → SQLite FaceId → STORED.

S3 PutObject is not in the runtime path.

Browser smoke (do not automate real faces):

1. Open `/kiosk`
2. Reserve an AVAILABLE locker
3. ID + FRAME-A until CompareFaces PASS
4. Complete store
5. Expect SQLite FaceId and Collection FaceCount +1

## 14. RETRIEVE Workflow

8-digit code → SQLite lookup (no camera/AWS if invalid) → FRAME-B → YuNet →
SearchFacesByImage → expected FaceId + threshold → RETRIEVED → DeleteFaces.

Invalid code: SQLite FAIL, camera/OpenCV/Rekognition 0.

FaceId-less STORED: `DATA_CONSISTENCY_ERROR`, AWS 0, locker closed.

## 15. AWS Call Boundaries

| Step | AWS |
|---|---|
| retrieve/lookup | 0 |
| quality fail | 0 |
| invalid code | 0 |
| STORE verify | CompareFaces |
| STORE complete | IndexFaces |
| RETRIEVE start | SearchFacesByImage |
| RETRIEVE complete | DeleteFaces |
| any S3 object API | 0 |

## 16. Tests

`pytest tests`: **181 passed / 0 failed**

CloudFormation `validate-template`:

- `aws-infra/aws-infra-cfn.yaml` VALID
- `aws-infra/aws-infra-ec2-cfn.yaml` VALID

## 17. Remaining Work

To finish the AWS half of this reset, re-run with CloudFormation
delete/create allowed:

```
cd ~/workspace/aws-face-detection
AWS_DEFAULT_REGION=ap-northeast-2 .venv/bin/pynt deletestack
aws cloudformation wait stack-delete-complete \
  --stack-name video-analyzer-stack --region ap-northeast-2
AWS_DEFAULT_REGION=ap-northeast-2 .venv/bin/pynt createstack
aws cloudformation wait stack-create-complete \
  --stack-name video-analyzer-stack --region ap-northeast-2
```

Then confirm:

- stack resources = `KioskFaceCollection` only
- FaceCount = 0
- frames bucket gone or leftover-empty (live template had no Retain)
- artifact bucket still present
- kiosk STORE/RETRIEVE browser smoke
