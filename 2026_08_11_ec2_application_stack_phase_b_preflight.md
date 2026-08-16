# EC2 Application Stack — Phase B Preflight (B0/B1)

**Date:** 2026-08-11  
**Region:** `ap-northeast-2`  
**Account:** `115019372648`  
**Caller:** `AwsFaceDetectionKioskRole` on instance `i-060ed51daa9abc84f`  
**AWS application-resource mutations:** **NONE**  
**Change set executed:** **NONE** (not generated)  
**Git commit:** **NONE**

---

## Classification (exactly one)

# **PHASE_B_BLOCKED_DATA_STACK**

**Primary reason:** `video-analyzer-stack` does **not** exist (historical `DELETE_COMPLETE` only). Essential data-plane resources are **absent** (Kinesis, DynamoDB, Lambdas, frames bucket, API Gateway). ApplicationInstance E2E and runtime IAM targets cannot function.

### Co-blockers (must clear after data stack)

| Co-blocker | Status |
|---|---|
| **EC2 stack mode** | `video-analyzer-ec2-stack` **does not exist** → **GREENFIELD_CREATE** would launch **3 EC2s** (WebUi + Kpi + Application) from current Phase A template — **do not run `createec2stack` blindly** |
| **Parameters** | `config/ec2-params.json` and `config/ec2-global-params.json` **missing** from workspace |
| **Artifacts** | `apps/webui|kpi|application/` **not present** on artifact bucket (only `lambda/` zips found) |
| **SSM auth** | Existence **not verifiable** with current role (`ssm:GetParameter` AccessDenied); treat as **must re-run `setwebuiauth`** with a principal that can write SSM before Application bootstrap |
| **EIP/quota** | `ec2:DescribeAddresses` / service-quotas **denied** from this role — cannot prove quota headroom |

---

## 1. AWS account / region

| Check | Result |
|---|---|
| Account | **115019372648** (matches required) |
| Region env `AWS_DEFAULT_REGION` / `AWS_REGION` | **ap-northeast-2** |
| `aws configure get region` | (empty / not set; env vars govern) |
| STS ARN | `arn:aws:sts::115019372648:assumed-role/AwsFaceDetectionKioskRole/i-060ed51daa9abc84f` |
| Continue? | **Yes** for identity/region; **No** for Application deploy |

---

## 2–3. Data stack status

| Item | Result |
|---|---|
| `video-analyzer-stack` live | **NO** — `ValidationError: Stack with id video-analyzer-stack does not exist` |
| Historical | Multiple `DELETE_COMPLETE` entries; most recent deletion **2026-08-11T00:46:10Z** |
| DATA_STACK_READY | **false** |

### Essential resource checks (independent of stack)

| Resource | Status |
|---|---|
| Kinesis `FrameStream` | **NOT FOUND** |
| DynamoDB `EnrichedFrame` | **NOT FOUND** |
| Lambda `imageprocessor` | **NOT FOUND** |
| Lambda `framefetcher` | **NOT FOUND** |
| Lambda `facecompare` | **NOT FOUND** |
| Frames S3 `aws-face-detection-frames-115019372648-apne2` | **NOT FOUND** (`frame_bucket_present=false`) |
| API Gateway (Rekog/Vid/video names) | **empty list** |
| CloudWatch log groups `/aws/lambda/{imageprocessor,framefetcher,facecompare}` | **still exist** (orphaned log groups; not a substitute for Lambdas) |
| Code/artifact bucket `aws-face-detection-lambda-115019372648-apne2` | **EXISTS** (`ap-northeast-2`) |

### Frame bucket parameter resolution

| Item | Result |
|---|---|
| Data CFN logical ID | `FrameS3Bucket` (`aws-infra/aws-infra-cfn.yaml`) |
| Intended physical name (from `config/cfn-params.json`) | `aws-face-detection-frames-115019372648-apne2` |
| `DescribeStackResource` | **N/A** — stack absent |
| Live `head-bucket` | **404 / not found** |
| `frame_bucket_present` | **false** |
| `bucket_name` (intended when recreated) | `aws-face-detection-frames-115019372648-apne2` |
| Prefixes `frames/`, `locker-references/` | **Cannot verify** — bucket missing (lifecycle on data template only expires `frames/` + `logging/`; `locker-references/` remains app-managed once bucket exists) |

---

## 4. Data stack recovery (do **not** auto-execute)

Verified from current `build.py`:

| Task | Role |
|---|---|
| `pynt packagelambda` | Zips `lambda/{framefetcher,imageprocessor,facecompare}/` + `config/*-params.json` into `build/*.zip` |
| `pynt deploylambda` | Uploads zips to `SourceS3BucketParameter` keys from `config/cfn-params.json` |
| `pynt createstack` | Creates `video-analyzer-stack` from `aws-infra/aws-infra-cfn.yaml` |
| `pynt stackstatus` | Describes stack status |
| `pynt updatelambda` | Optional: push code directly to existing functions (not create) |

**facecompare packaging note:** Local source includes transaction-bound reference fields (`referenceS3Key`, `referenceS3Bucket`, `targetFrameId`). A fresh `packagelambda` + `deploylambda` **would** package the current reference-face implementation. S3 already has `lambda/facecompare.zip` dated **2026-08-11 09:43** on the code bucket (uploaded earlier); stack recreation still needs the **CloudFormation stack** and function resources.

### Recommended recovery sequence (manual, elevated principal if needed)

```bash
export AWS_DEFAULT_REGION=ap-northeast-2
# 1) Package current Lambda sources (includes facecompare reference-face mode)
pynt packagelambda
# 2) Upload zips to code bucket for CFN
pynt deploylambda
# 3) Optional log retention helper (create log groups / set retention)
pynt setlogretention
# 4) Create data stack
pynt createstack
# 5) Confirm
pynt stackstatus
# Then re-verify: FrameStream ACTIVE, EnrichedFrame ACTIVE, 3 Lambdas, frames bucket, API stage development
```

**Do not** recreate EC2 Application stack until data stack is healthy.

---

## 5. EC2 stack status

| Item | Result |
|---|---|
| `video-analyzer-ec2-stack` live | **NO** — does not exist |
| Historical | `DELETE_COMPLETE` (e.g. 2026-06-19 and earlier) |
| `ec2_stack_exists` | **false** |
| `stack_status` | **DOES_NOT_EXIST** (historical DELETE_COMPLETE only) |
| `WebUiInstance_live` | **unknown via API** — `ec2:DescribeInstances` **AccessDenied** for this role; no stack resources to list |
| `KpiInstance_live` | **unknown via API** (same) |
| `legacy_EIPs_live` | **unknown via API** — `ec2:DescribeAddresses` **AccessDenied** |
| Out-of-band host (this session) | Running **outside** CFN as kiosk/dev (`t3.medium`, public IP observed via IMDS, role `AwsFaceDetectionKioskRole`) — **not** WebUi/Kpi from template |

---

## 6. Greenfield vs parallel-update

| Classification | **GREENFIELD_CREATE** |
|---|---|
| Why | EC2 stack absent; Phase A template still defines WebUi + Kpi + Application |
| Risk of `pynt createec2stack` | Creates **three** EC2s + **three** EIPs + dual legacy SG/roles + Application data volume — wasteful and not the Phase A “parallel migration” intent |
| Change set | **Not generated** — cannot UPDATE a missing stack; CREATE change set of full template would encode the 3-host greenfield |

### Safer options before any EC2 create (choose later; not executed)

| Option | Description | Recommendation |
|---|---|---|
| **1** | Create all 3 from current template | **Reject for demo cost** unless explicitly wanted |
| **2** | New application-only stack/template (Application* only) | **Preferred** for greenfield: one host, one EIP, one data volume |
| **3** | Phase A.1: temporary template flag/condition to disable WebUi/Kpi resource creation | Acceptable intermediate if stack name must stay `video-analyzer-ec2-stack` |

**STOP:** Do not run `createec2stack` with the current dual+app template until mode is decided.

---

## 7. Parameters (`config/ec2-params.json`)

| Item | Result |
|---|---|
| `config/ec2-params.json` | **MISSING** |
| `config/ec2-global-params.json` | **MISSING** |
| `parameters ready` | **false** |

### Required for Application (from Phase A template + `build.py`)

| Parameter | Status in workspace |
|---|---|
| `VpcIdParameter` | missing file |
| `SubnetIdParameter` | missing file |
| `AppArtifactS3BucketParameter` | missing (intended: code bucket) |
| `AppArtifactS3KeyPrefixParameter` | default `apps/` in template |
| `FrameS3BucketNameParameter` | **required, no default** — intended frames bucket name above |
| `DataStackNameParameter` | default `video-analyzer-stack` |
| `DataApiStageNameParameter` | default `development` |
| `KinesisStreamNameParameter` | default `FrameStream` |
| `DataDdbTableNameParameter` / GSI | defaults present in template |
| `ApplicationInstanceTypeParameter` | default `t3.small` |
| `ApplicationDataVolumeSizeGbParameter` | default `8` |
| `AllowedIngressCidrParameter` | default `0.0.0.0/0` |
| `WebUiAuthSsmPrefixParameter` | default `/video-analyzer/webui` |
| Facecompare / imageprocessor names | defaults in template |

**Note:** `config/cfn-params.json` (data stack) is present and lists frames/code bucket names — use as source when drafting `ec2-params.json` **after** data stack recreate.

Subnet/VPC discovery: this role lacks `ec2:DescribeVpcs` / `DescribeSubnets`. Use an admin principal for `pynt ec2vpcinfo` when preparing params.

---

## 8–11. Artifact, SSM, subnet/AZ, EIP readiness

### Artifacts

| Prefix / object | Result |
|---|---|
| Code bucket | present |
| `apps/` tree | **empty / not listed** |
| `apps/webui/` | **missing** |
| `apps/kpi/` | **missing** |
| `apps/application/` | **missing** |
| `lambda/*.zip` on S3 | **present** (2026-08-11) |
| Local preflight package (`build/local-preflight/`) | **built** this task: `web-ui.tgz`, `kpi-dashboard.tgz`, `bootstrap.sh`, `kiosk-fastapi.service`, `kpi-dashboard.service`, `nginx-application.conf` |
| `artifacts ready` (S3 for bootstrap) | **false** (local packaging OK) |

**publishapps was NOT run** (would upload to S3).

### SSM auth

| Item | Result |
|---|---|
| `ssm:GetParameter` on `/video-analyzer/webui/*` | **AccessDenied** (this role) |
| Values printed | **none** |
| `auth_ssm_ready` | **unknown → treat as false until proven with admin/`setwebuiauth`** |

### Subnet / EBS AZ

| Item | Result |
|---|---|
| Live subnet inspect | **AccessDenied** (`DescribeSubnets`) |
| Template AZ derivation | `ApplicationDataVolume.AvailabilityZone = !GetAtt ApplicationInstance.AvailabilityZone` → **same AZ as instance** when stack exists |
| Circular dep avoidance | UserData uses `DATA_VOLUME_DEVICE=/dev/xvdf` only (**no** `!Ref ApplicationDataVolume`) — correct |
| Race | Volume created **after** instance (GetAtt AZ); bootstrap **waits up to ~180s** for device — acceptable but first-boot race remains real |
| `subnet/EBS AZ ready` | **template OK; live params/subnet not ready** |

### EIP

| Item | Result |
|---|---|
| Describe addresses / quotas | **AccessDenied** |
| Phase B temporary EIP count if full greenfield | up to **3** (WebUi+Kpi+Application) |
| Application-only stack | **1** EIP |
| `EIP allocation ready` | **unknown** (cannot prove from this principal) |

---

## 12–15. Static product readiness (template/code)

### Runtime IAM (`ApplicationInstanceRole`) — static review

| Capability | Present |
|---|---|
| Kinesis PutRecord/PutRecords | Yes (stream-scoped) |
| DynamoDB GetItem | Yes (EnrichedFrame) |
| DynamoDB Query/DescribeTable + GSI | Yes (KPI) |
| S3 Get/Put/Delete on `frames/*` + `locker-references/*` | Yes |
| Lambda Invoke facecompare | Yes |
| Lambda Get/Update imageprocessor | Yes |
| CFN DescribeStackResource | Yes (data stack) |
| API Gateway GET apikeys | Yes |
| Logs StartQuery (3 groups) + Get/Stop; GetMetricData | Yes |
| SSM GetParameter(s) + KMS via SSM | Yes |
| Artifact S3 GetObject apps/* | Yes |
| Create/Delete stack, iam:*, Lambda create/delete, broad wildcards | **Excluded** |

**S3 Head/Copy:** IAM uses `s3:GetObject` / `s3:PutObject` / `s3:DeleteObject` only — correct (no fictional `s3:HeadObject` / `s3:CopyObject`). HeadObject maps to GetObject; CopyObject needs source Get + dest Put.

**Log group ARNs:** `/aws/lambda/imageprocessor|framefetcher|facecompare` — match orphaned log groups and intended Lambda names.

`runtime IAM ready` (template): **yes** (static). Live attach only after deploy.

### Nginx (`aws-infra/nginx/application.conf`)

| Check | OK |
|---|---|
| :80 → 308 HTTPS | Yes |
| :443 TLS | Yes |
| `/dashboard` → `/dashboard/` | Yes |
| `/dashboard/` → `127.0.0.1:8000/` strip | Yes |
| `/` → FastAPI `127.0.0.1:8080` | Yes |
| Host / X-Real-IP / X-Forwarded-For / X-Forwarded-Proto=https | Yes |
| `Cache-Control: no-store` (API not cached) | Yes |
| `Nginx ready` | **yes** (static) |

### Frontend base URL

| Page | `getApiBaseUrl` without `/proxy/\d+` prefix |
|---|---|
| `https://<EIP>/` | **`/api`** |
| `https://<EIP>/kiosk` | **`/api`** |
| code-server only | prefixes `/proxy/<port>` when path matches |

No defect found; no edit made.

### Session behind Nginx

| Item | Assessment |
|---|---|
| Bootstrap sets `WEBUI_SESSION_HTTPS_ONLY=true` | Yes |
| Browser origin HTTPS; uvicorn HTTP loopback | Yes |
| Secure cookie to browser | Yes (browser sees HTTPS) |
| code-server cookie middleware required for Nginx path | **No** |
| Keep `CodeServerSessionCookieCompatMiddleware` | **Yes** (harmless) |
| `session config ready` | **yes** (static) |

### EBS bootstrap / systemd

| Check | OK |
|---|---|
| Encrypted volume in CFN | Yes |
| Nitro device discovery | Yes |
| Format only if unformatted (`blkid`) | Yes |
| UUID fstab mount `/data/kiosk` | Yes |
| Refuse root-disk mount | Yes |
| `KIOSK_DB_PATH=/data/kiosk/kiosk.db` | Yes |
| `RequiresMountsFor=/data/kiosk` | Yes |
| webui ownership on `/data/kiosk` | Yes |
| `EBS bootstrap ready` | **yes** (static) |

---

## 16. TLS / EIP race

| Question | Answer |
|---|---|
| Race found? | **yes** |
| First-boot cert SAN likely contains | **B. launch-time auto public IP** (IMDS `public-ipv4` while waiting ≤~60s), then EIP may replace it |
| Guaranteed Application EIP in SAN? | **No** |
| Private-IP-only? | Unlikely if public IP appears; fallback CN=localhost if none |
| Existing mitigation | Manual `/opt/app/regenerate-tls.sh` + `nginx reload` |
| Smallest safe fix **before** deploy (recommended, not implemented this task) | Install a **systemd oneshot** `application-tls-refresh.service`: `After=network-online.target nginx.service`, run regenerate helper once (or when current public IP ∉ cert SAN), enable on boot; optional short retry loop (e.g. 10×30s) for EIP association lag |
| Alternative | Operator SSM after `CREATE_COMPLETE`: `sudo /opt/app/regenerate-tls.sh` (documented ops step) |

**Do not hand-wave:** camera getUserMedia works with self-signed if user accepts warning **and** cert IP matches browser host; EIP mismatch → extra browser warning / possible SAN complaints.

---

## 17. CloudFormation dependency chain

```text
ApplicationInstance (launch + UserData starts)
    │
    ├─► ApplicationDataVolume (AZ = GetAtt Instance.AvailabilityZone)  [after instance exists]
    │         │
    │         └─► ApplicationDataVolumeAttachment (/dev/xvdf)
    │
    └─► ApplicationElasticIpAssociation (EIP may land after UserData cert generation)
```

| Risk | Handling |
|---|---|
| UserData before volume attach | Bootstrap device wait loop (~180s) |
| Format race | Only format if no blkid |
| EIP after cert | regenerate helper (manual/oneshot recommended) |
| Circular Ref volume in UserData | **Avoided** (device path only) |

---

## 18–19. Change set

| Item | Result |
|---|---|
| Change set generated | **no** |
| Why | Data stack not ready; EC2 stack absent; greenfield would create 3 instances; params file missing |
| Destructive changes review | **N/A** (no change set) |
| `destructive changes present` | **no** (none generated; template still additive for Application* if UPDATE were used on a stack that already had WebUi/Kpi) |

### Expected additive set (when PARALLEL_UPDATE becomes possible)

- ApplicationSecurityGroup  
- ApplicationInstanceRole / Profile  
- ApplicationInstance  
- ApplicationDataVolume / Attachment  
- ApplicationElasticIp / Association  
- Outputs only additions  

Must **not** Remove/Replace WebUi/Kpi or drop legacy EIPs in parallel phase.

---

## 20. Temporary resource count (structural)

### If mistakenly greenfield-create **current** template

| Resource | Count |
|---|---|
| EC2 | **3** (WebUi + Kpi + Application) |
| EIP | **3** |
| Root volumes | **3** |
| Dedicated data volumes | **1** |

### If application-only stack (recommended greenfield)

| Resource | Count |
|---|---|
| EC2 | **1** |
| EIP | **1** |
| Root volumes | **1** |
| Dedicated data volumes | **1** |

### If true parallel update (stack already had WebUi+Kpi)

| Resource | Count after Phase B |
|---|---|
| EC2 | **3** temporary |
| EIP | **3** temporary |
| Root | **3** |
| Data | **1** |

No dollar estimates.

---

## 21. Exact next commands (do **not** execute until blockers clear)

### Stage 0 — Data plane recovery (blocker #1)

```bash
export AWS_DEFAULT_REGION=ap-northeast-2
pynt packagelambda
pynt deploylambda
pynt setlogretention
pynt createstack
pynt stackstatus
# verify FrameStream, EnrichedFrame, Lambdas, frames bucket, API stage
```

### Stage 1 — Decide EC2 mode (blocker #2)

**Do not** `pynt createec2stack` on current template without decision.

Recommended path for empty account state:

1. Author **application-only** template slice **or** Conditions disabling WebUi/Kpi creation (Phase A.1).  
2. Create `config/ec2-global-params.json` → `{"StackName":"video-analyzer-ec2-stack"}`  
3. Create `config/ec2-params.json` with VPC, public subnet, artifact bucket, **FrameS3BucketNameParameter**, ingress CIDR.  
4. `pynt setwebuiauth` (admin/SSM write).  
5. `pynt publishapps` (uploads webui+kpi+application).  
6. Create stack / change set → **review** → execute only after review.  
7. Post-deploy: TLS regenerate if needed; full Phase B E2E checklist.

### Stage 2 — If a live EC2 stack with WebUi+Kpi already existed (not current)

```bash
pynt setwebuiauth          # if needed
pynt publishapps
# create change set application-phase-b-preview-YYYYMMDD (review only)
# execute only if no Remove/Replace on WebUi/Kpi
pynt updateec2stack        # only after change-set approval
pynt applicationip
pynt applicationstatus
```

---

## Post-deploy Phase B execution checklist (future — not run)

1. ApplicationInstance CREATE_COMPLETE  
2. ApplicationDataVolume attached  
3. `/data/kiosk` mounted (not root)  
4. `kiosk-fastapi` active  
5. `kpi-dashboard` active  
6. `nginx` active  
7. `nginx -t` succeeds  
8. Application EIP associated  
9. TLS certificate SAN checked / regenerate if needed  
10. `https://<EIP>/healthz`  
11. login session  
12. `/api/config`  
13. `/api/detect-labels`  
14. `/api/capture-frame`  
15. `/dashboard/`  
16. STORE positive flow  
17. reference S3 object under `locker-references/`  
18. RETRIEVE positive flow  
19. RETRIEVE negative face flow  
20. SQLite persistence after FastAPI restart  

---

## Worktree

- Uncommitted product + Phase A infra preserved  
- `git diff --check` clean on prior check  
- **No commit**  

---

## Summary table

| Gate | Ready? |
|---|---|
| AWS identity/region | **yes** |
| Data stack | **no** |
| Data resources | **no** |
| EC2 stack exists | **no** |
| Deploy mode | **GREENFIELD_CREATE (unsafe as 3-host)** |
| Frame bucket resolved/live | **no** |
| Parameters file | **no** |
| S3 app artifacts | **no** (local package yes) |
| SSM auth proven | **no** |
| Subnet params | **no** (API denied) |
| EIP quota proven | **unknown** |
| Template IAM/Nginx/session/EBS | **yes** (static) |
| TLS/EIP race | **yes (found)** — fix before rely on first boot |
| Change set | **not generated** |
| Destructive change set | **n/a / none** |
| **PHASE_B_READY** | **false → PHASE_B_BLOCKED_DATA_STACK** |
