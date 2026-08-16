# EC2 Application Stack — Greenfield Pivot Result

**Date:** 2026-08-11  
**Account:** 115019372648 · **Region:** ap-northeast-2  
**AWS mutations:** **NONE**  
**Git commit:** **NONE**

---

## 1. Selected strategy

**Strategy A — Remove legacy WebUi/Kpi resources entirely** from
`aws-infra/aws-infra-ec2-cfn.yaml`.

Not Strategy B (Conditions) because:

- `video-analyzer-ec2-stack` **does not exist** (re-proven this task)
- No live dual-host resources to preserve
- Default create must not require a hidden operator flag to avoid three EC2s

**Proved again:**

```text
aws cloudformation describe-stacks --stack-name video-analyzer-ec2-stack
→ ValidationError: Stack with id video-analyzer-ec2-stack does not exist
```

---

## 2. Files changed / added

### Modified

| File | Change |
|---|---|
| `aws-infra/aws-infra-ec2-cfn.yaml` | Full rewrite: Application-only + optional Application scheduler |
| `aws-infra/userdata/application-bootstrap.sh` | Install/enable TLS refresh oneshot |
| `build.py` | Greenfield create messaging; publish TLS artifacts; status counts |
| `docs/EC2_DEPLOYMENT.md` | Primary = Application-only; dual-host → Legacy section |
| `2026_08_11_ec2_application_stack_design.md` | Greenfield Pivot section |
| `2026_08_11_ec2_application_stack_phase_a_result.md` | Pivot note |

### Added

| File | Purpose |
|---|---|
| `aws-infra/userdata/application-tls-refresh.sh` | Idempotent SAN refresh (bounded retries) |
| `aws-infra/userdata/application-tls-refresh.service` | systemd Type=oneshot |
| `config/ec2-global-params.example.json` | StackName example |
| `config/ec2-params.example.json` | Operator placeholders (no live VPC baked in) |
| `2026_08_11_ec2_application_stack_greenfield_result.md` | This report |

---

## 3–8. Default resource counts (static proof)

From template structure (no Conditions wrapping Application*; no WebUi/Kpi logical IDs):

| Metric | Count |
|---|---|
| `AWS::EC2::Instance` resources | **1** (`ApplicationInstance`) |
| `ApplicationInstance` | **1** |
| `WebUiInstance` | **0** (absent) |
| `KpiInstance` | **0** (absent) |
| `AWS::EC2::EIP` | **1** (`ApplicationElasticIp`) |
| `AWS::EC2::Volume` (data) | **1** (`ApplicationDataVolume`) |
| Root volume | via instance BDM (1) |

Scheduler resources exist only when `EnableSchedulerParameter=true` (default) and target **ApplicationInstance only**.

---

## 9. Legacy resources removed

Removed entirely (no Condition):

- WebUiSecurityGroup, KpiSecurityGroup  
- WebUiInstanceRole/Profile, KpiInstanceRole/Profile  
- WebUiInstance, KpiInstance  
- WebUiElasticIp(+assoc), KpiElasticIp(+assoc)  
- Legacy outputs: WebUiUrl, KpiUrl, WebUiInstanceId, KpiInstanceId, …  
- Parameters: WebUiPortParameter, KpiPortParameter, InstanceTypeParameter (legacy)

---

## 10. Scheduler final behavior

| Item | Behavior |
|---|---|
| Parameter | `EnableSchedulerParameter` default **`true`** |
| Condition | `SchedulerEnabled` |
| Target | `InstanceIds: [ApplicationInstance]` only |
| Windows | Mon–Fri KST 09/11/13/15 start/stop |
| When false | No SchedulerRole / schedules created |

---

## 11. Application runtime IAM coverage

Union of former WebUi + KPI needs + kiosk product, without deployment rights:

| Capability | ApplicationInstanceRole |
|---|---|
| Kinesis PutRecord(s) FrameStream | Yes |
| DynamoDB GetItem EnrichedFrame | Yes |
| DynamoDB Query/DescribeTable + GSI | Yes (KPI) |
| S3 Get/Put/Delete frames/* + locker-references/* | Yes |
| Lambda Invoke facecompare | Yes |
| Lambda Get/Update imageprocessor | Yes |
| CFN DescribeStackResource data stack | Yes |
| API Gateway GET apikeys | Yes |
| Logs Insights + GetMetricData | Yes |
| SSM auth read + KMS via SSM | Yes |
| Artifact S3 GetObject apps/* | Yes |
| Create/Delete stack, iam:*, Lambda create/delete | **No** |

### Legacy IAM removed

WebUiInstanceRole and KpiInstanceRole deleted with their hosts. Coverage is fully on ApplicationInstanceRole (table above).

---

## 12–14. Parameters / outputs / build.py

**Obsolete public-port parameters removed.** Loopback ports remain optional:

- `ApplicationFastApiPortParameter` default 8080  
- `ApplicationKpiPortParameter` default 8000  

**Outputs (default):**

- ApplicationInstanceId  
- ApplicationElasticIp  
- ApplicationUrl (`https://<EIP>/`)  
- ApplicationDashboardUrl  
- ApplicationDataVolumeId  
- StopInstancesCommand  
- ScheduleSummary  
- ArchitectureNote  

No WebUiUrl / KpiUrl.

**`pynt createec2stack`:** prints greenfield notice; creates Application-only stack (EC2 count target 1). No special flag required.

---

## 15–16. Parameter examples

```text
config/ec2-global-params.example.json
config/ec2-params.example.json
```

Operator:

```bash
cp config/ec2-global-params.example.json config/ec2-global-params.json
cp config/ec2-params.example.json config/ec2-params.json
# fill VpcId, SubnetId, FrameS3BucketName, AppArtifactS3Bucket, AllowedIngressCidr
```

Frame bucket: resolve after data stack restore via `DescribeStackResource` logical **`FrameS3Bucket`**.

---

## 17–18. TLS / EIP race fix

| Item | Detail |
|---|---|
| Race | Cert may use temporary public IP before EIP associates |
| Fix | `application-tls-refresh.service` (oneshot) + `application-tls-refresh.sh` |
| Behavior | IMDSv2 public IP; if cert SAN already matches → no regen; if no public IP → keep existing cert; else regen + nginx reload |
| Retry | default **10 × 30s** |
| Logs | `tls_refresh_needed=…` `public_ip_present=…` only |
| Bootstrap | enables unit; `start` after nginx; `regenerate-tls.sh` symlink for operators |

---

## 19–22. EBS / Nginx / session / code-server

| Area | Preserved |
|---|---|
| EBS AZ | `!GetAtt ApplicationInstance.AvailabilityZone` |
| Mount | UUID → `/data/kiosk`; format only if empty; Nitro discovery |
| Fail closed | `RequiresMountsFor=/data/kiosk`; `KIOSK_DB_PATH=/data/kiosk/kiosk.db` |
| Nginx | 80→HTTPS, 443, /dashboard/ strip, X-Forwarded-Proto https, no-store |
| Session | `WEBUI_SESSION_HTTPS_ONLY=true` |
| code-server | **not** in application path |

---

## 23–26. Validation (this task)

| Check | Result |
|---|---|
| CFN validate-template | run in session |
| bash -n | bootstrap + tls-refresh scripts |
| py_compile build.py | yes |
| git diff --check | Phase files |
| Static counts | Application EC2=1, legacy=0, EIP=1, data vol=1 |

---

## 27. Data stack prerequisite

`video-analyzer-stack` must be restored **before** meaningful Application E2E:

```bash
pynt packagelambda && pynt deploylambda && pynt createstack
```

---

## 28. Next deployment commands (do not run until ready)

```bash
export AWS_DEFAULT_REGION=ap-northeast-2
# data stack healthy first
cp config/ec2-global-params.example.json config/ec2-global-params.json
cp config/ec2-params.example.json config/ec2-params.json
# edit VPC/subnet/FrameS3Bucket/artifact bucket
pynt setwebuiauth
pynt publishapps
# FUTURE (mutates AWS):
# pynt createec2stack
# pynt applicationip
# pynt applicationstatus
```

Optional CREATE change set review (future admin session):

```bash
# expect only Application* (+ optional scheduler) — never WebUi/Kpi
```

---

## 29–30. Safety

| Item | Status |
|---|---|
| AWS mutations | **NONE** |
| Git commit | **NONE** |
