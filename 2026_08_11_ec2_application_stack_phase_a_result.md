# EC2 Application Stack — Phase A Implementation Result

**Date:** 2026-08-11  
**Branch:** `devhsj`  
**Scope:** Additive CloudFormation + bootstrap/Nginx/systemd + build helpers  
**AWS mutations:** **NONE**  
**Git commit:** **NONE**  
**old resources preserved:** **true**

---

## 1. Summary

Phase A implements a parallel **ApplicationInstance** next to the existing dual-host layout:

| Still present (rollback) | Added (Phase A) |
|---|---|
| `WebUiInstance` | `ApplicationInstance` |
| `KpiInstance` | Nginx :443 + loopback FastAPI/KPI |
| Dual SGs / roles / EIPs | Dedicated SQLite EBS + Application EIP |
| Scheduler → WebUi+Kpi only | Application **not** scheduled |

Demo app path (target after deploy):

```text
https://<ApplicationElasticIp>/
https://<ApplicationElasticIp>/kiosk
https://<ApplicationElasticIp>/dashboard/
```

**Not** `/proxy/8080/` / code-server.

---

## 2. Files changed / added

### Added

| File | Purpose |
|---|---|
| `aws-infra/userdata/application-bootstrap.sh` | Co-located host bootstrap (idempotent EBS, venvs, TLS, units) |
| `aws-infra/userdata/kiosk-fastapi.service` | FastAPI loopback + `RequiresMountsFor=/data/kiosk` |
| `aws-infra/userdata/kpi-dashboard-loopback.service` | KPI loopback unit for Application host |
| `aws-infra/nginx/application.conf` | Public reverse proxy routes |
| `2026_08_11_ec2_application_stack_phase_a_result.md` | This report |

### Modified

| File | Purpose |
|---|---|
| `aws-infra/aws-infra-ec2-cfn.yaml` | Parameters, Application* resources, outputs (legacy kept) |
| `build.py` | `publishapps application`; `applicationip`; `applicationstatus` |
| `docs/EC2_DEPLOYMENT.md` | Section **ApplicationInstance Parallel Migration** |
| `2026_08_11_ec2_application_stack_design.md` | Section **Phase A Implementation Result** |

Unrelated uncommitted product work (kiosk face flow, etc.) was **not** reverted.

---

## 3. Application CFN resources added

- `ApplicationSecurityGroup`
- `ApplicationInstanceRole`
- `ApplicationInstanceProfile`
- `ApplicationInstance` (Name tag: `video-analyzer-app`)
- `ApplicationDataVolume` (+ `DeletionPolicy`/`UpdateReplacePolicy`: **Snapshot**)
- `ApplicationDataVolumeAttachment` (device `/dev/xvdf`)
- `ApplicationElasticIp` + `ApplicationElasticIpAssociation`

---

## 4. Old WebUi/Kpi resources preserved

Verified present in template:

- `WebUiInstance`, `KpiInstance`
- `WebUiSecurityGroup`, `KpiSecurityGroup`
- `WebUiInstanceRole`, `KpiInstanceRole`, profiles
- `WebUiElasticIp`, `KpiElasticIp` (+ associations)
- Schedules still `InstanceIds: [WebUiInstance, KpiInstance]` only

**old resources preserved=true**

---

## 5. Instance type

| Host | Parameter | Default |
|---|---|---|
| Legacy WebUi/Kpi | `InstanceTypeParameter` | `t3.micro` (unchanged) |
| Application | `ApplicationInstanceTypeParameter` | **`t3.small`** |

---

## 6. Application SG inbound

| Port | Purpose |
|---|---|
| **443/tcp** | HTTPS Nginx (app entry) |
| **80/tcp** | HTTP → HTTPS redirect only |

**Not open:** 8080, 8000, 22, code-server. Admin: SSM.

---

## 7. Runtime IAM actions (ApplicationInstanceRole)

| Area | Actions | Scope |
|---|---|---|
| Bootstrap artifacts | `s3:GetObject` | `apps/webui/*`, `apps/kpi/*`, `apps/application/*` |
| Auth secrets | `ssm:GetParameter(s)`, `kms:Decrypt` (via SSM) | `/video-analyzer/webui/*` |
| Capture | `kinesis:PutRecord`, `PutRecords` | stream `FrameStream` |
| API config | `cloudformation:DescribeStackResource` | data stack |
| API config | `apigateway:GET` | `/apikeys/*` |
| DetectLabels | `lambda:Get/UpdateFunctionConfiguration` | `imageprocessor` |
| Face verify | `lambda:InvokeFunction` | `facecompare` |
| Frame meta | `dynamodb:GetItem` | `EnrichedFrame` |
| Reference face | `s3:GetObject`, `PutObject`, `DeleteObject` | `frames/*`, `locker-references/*` |
| KPI | `logs:StartQuery` | 3 Lambda log groups |
| KPI | `logs:GetQueryResults`, `StopQuery` | `*` (IAM limitation) |
| KPI | `cloudwatch:GetMetricData` | `*` (IAM limitation) |
| KPI | `dynamodb:Query`, `DescribeTable` | table + GSI |
| Ops | `AmazonSSMManagedInstanceCore` | managed |

---

## 8. Deployment IAM intentionally excluded

- `cloudformation:CreateStack` / `UpdateStack` / `DeleteStack`
- `iam:*`
- `lambda:CreateFunction` / `DeleteFunction`
- Broad `s3:*`, `dynamodb:*`, `kinesis:*`
- Historical broad developer/kiosk deployment roles

---

## 9–12. EBS / SQLite

| Item | Value |
|---|---|
| Size / type | Parameter default **8 GiB**, **gp3**, **encrypted** |
| DeletionPolicy | **Snapshot** |
| UpdateReplacePolicy | **Snapshot** |
| Mount | **`/data/kiosk`** (UUID fstab; format only if unformatted) |
| SQLite | **`/data/kiosk/kiosk.db`** via `KIOSK_DB_PATH` |
| Fail closed | `RequiresMountsFor=/data/kiosk`; bootstrap refuses root-disk mount |

**Note:** UserData does not `!Ref` the volume ID (volume AZ depends on instance → circular dependency). Bootstrap discovers `/dev/xvdf` / Nitro NVMe paths and waits for attachment.

---

## 13–16. Binds, Nginx

| Service | Bind |
|---|---|
| kiosk-fastapi | **`127.0.0.1:8080`** (no TLS in uvicorn) |
| kpi-dashboard | **`127.0.0.1:8000`** |
| Nginx public | **443** (+ **80** redirect) |

Routes: `/` `/kiosk` `/api/*` `/src/*` `/healthz` → FastAPI; `/dashboard/` → KPI (prefix strip); `/dashboard` → 308 `/dashboard/`.

Proxy headers: `Host`, `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto: https`. No cache of API responses (`Cache-Control: no-store`).

---

## 17–18. TLS / code-server

| Topic | Decision |
|---|---|
| TLS | Nginx **self-signed** (generated if absent); SAN includes public IPv4 when metadata available |
| EIP SAN drift | `/opt/app/regenerate-tls.sh` |
| Browser | Certificate warning expected (demo) |
| getUserMedia | HTTPS origin satisfied |
| Session | `WEBUI_SESSION_HTTPS_ONLY=true` |
| code-server | **Not** in application path; not installed by this bootstrap |

---

## 19–20. EIP / scheduler

| Topic | Phase A behavior |
|---|---|
| EIP | **New** `ApplicationElasticIp` — does not reassign WebUi/Kpi EIPs |
| Temporary cost | Up to **3 EIPs** while parallel |
| Outputs | `ApplicationUrl` = `https://<eip>/` (no proxy path) |
| Scheduler | Legacy schedules **unchanged**; Application **manual** only |

Phase D (future): collapse schedules to ApplicationInstance; release legacy EIPs.

---

## 21. build.py changes

- `publishapps` accepts **`application`**; default set is `webui`, `kpi`, `application`
- Uploads under `apps/application/`: `bootstrap.sh`, `kiosk-fastapi.service`, `kpi-dashboard.service` (loopback), `nginx-application.conf`
- Reuses existing `apps/webui/web-ui.tgz` and `apps/kpi/kpi-dashboard.tgz`
- Helpers: `applicationip`, `applicationstatus` (read-only when run)
- Does **not** auto-delete legacy resources

---

## 22–25. Static validation (executed this phase)

| Check | Result |
|---|---|
| YAML parse (PyYAML + CFN multi-constructor) | **Pass** (25 resources, 0 tabs) |
| `aws cloudformation validate-template` | **Pass** (23 parameters; read-only; no create/update) |
| `bash -n` application/webui/kpi bootstrap scripts | **Pass** |
| `python3 -m py_compile build.py` | **Pass** |
| systemd unit structure + loopback `ExecStart` + `RequiresMountsFor` | **Pass** |
| nginx conf structure (443, routes, X-Forwarded-Proto, redirects) | **Pass** (live `nginx -t` after deploy) |
| `git diff --check` (Phase A modified tracked files) | **Pass** (after trailing-whitespace fix) |
| Legacy resources still in template | **Pass** (`old resources preserved=true`) |
| Scheduler targets exclude ApplicationInstance | **Pass** |
| No UserData `!Ref ApplicationDataVolume` (circular dep avoided) | **Pass** |
| shellcheck | Not installed (skipped) |

---

## 26–27. Safety confirmation

| Requirement | Status |
|---|---|
| AWS create/update/delete stack | **NONE** |
| Live EC2/EBS/IAM/SG/EIP mutations | **NONE** |
| Old CFN resource deletion | **NONE** |
| Application restart | **NONE** |
| Commit | **NONE** |

---

## 28. Next deployment phase

**Phase B (not this task):**

1. Ensure `config/ec2-params.json` includes `FrameS3BucketNameParameter` (copy from data-stack params).  
2. `pynt setwebuiauth` if needed.  
3. `pynt publishapps`  
4. `pynt createec2stack` or `pynt updateec2stack`  
5. `pynt applicationip` / SSM health checks  
6. Full STORE/RETRIEVE + dashboard E2E on Application URL  

Then Phase C cutover, Phase D remove legacy dual instances.

---

## 29. Report files

- Updated: `2026_08_11_ec2_application_stack_design.md` (Phase A Implementation Result)  
- Updated: `docs/EC2_DEPLOYMENT.md` (ApplicationInstance Parallel Migration)  
- Created: `2026_08_11_ec2_application_stack_phase_a_result.md` (this file)  

---

## 30. Commit

**Nothing was committed.**

---

## Greenfield Pivot (supersedes parallel-migration default)

Phase A originally added Application* **alongside** WebUi/Kpi for parallel migration.

Preflight later found **no live** `video-analyzer-ec2-stack`. Creating that template
greenfield would have launched **three** EC2s. That path was rejected.

**Follow-up (code):** legacy WebUi/Kpi CFN resources were **removed** from
`aws-infra/aws-infra-ec2-cfn.yaml`. Default create is Application-only. See
`2026_08_11_ec2_application_stack_greenfield_result.md`.

