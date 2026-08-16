# EC2 Application Stack Design — WebUi + Dashboard → Single ApplicationInstance

**Date:** 2026-08-11  
**Branch:** `devhsj`  
**Scope:** Architecture inspection and migration **planning only**  
**Explicit non-actions:** no CloudFormation edits, no AWS create/delete/update, no IAM/SG changes, no app restart, no git commit  

---

## Executive summary

| Question | Answer |
|---|---|
| One-EC2 merge feasible? | **FEASIBLE WITH SMALL CHANGES** |
| Recommended instance role | **ApplicationInstance** (single host) |
| Dashboard on same host? | **Yes** — keep separate process (Strategy A) |
| code-server in app path? | **No** (final demo path) |
| Stack rename? | **Keep** `video-analyzer-ec2-stack` name; change internal resources |
| Data stack? | **Keep** separate `video-analyzer-stack` |
| SQLite? | Keep on **dedicated EBS** at `/data/kiosk/kiosk.db` |

The historical CFN design (`WebUiInstance` + `KpiInstance`) no longer matches the product: FastAPI is a real application backend (sessions, SQLite kiosk state, STORE/RETRIEVE face gate, S3 reference faces, Lambda invoke). Collapsing to one Application EC2 is safe for hackathon/demo scale if ports, IAM, systemd, Nginx, and EBS are designed deliberately and migration is **parallel**, not in-place destructive.

---

## 1. Current EC2 stack architecture

### 1.1 Stack boundary

| Stack | Name (concept) | Owns |
|---|---|---|
| Data plane | `video-analyzer-stack` | Kinesis `FrameStream`, DynamoDB `EnrichedFrame`, S3 frames bucket, Lambdas (`imageprocessor`, `framefetcher`, `facecompare`), API Gateway, Rekognition integration |
| Compute / app | `video-analyzer-ec2-stack` | Two EC2s, two SGs, two IAM roles/profiles, two EIPs, EventBridge start/stop schedules |

Source of truth for compute: `aws-infra/aws-infra-ec2-cfn.yaml`.  
Deployment tooling: `build.py` (`createec2stack`, `updateec2stack`, `deleteec2stack`, `publishapps`, `ec2ip`, `setwebuiauth`).  
Ops docs: `docs/EC2_DEPLOYMENT.md`.

**Operational note (live account, from prior reports / cost docs):** the CFN EC2 stack has historically been deleted or only partially used; a separate **outside-CFN** kiosk/dev host (`t3.small`, code-server on 443, FastAPI via `/proxy/8080`) has carried product work. This design still targets the **CFN EC2 template** as the durable deployment model. The out-of-band host remains a migration concern, not a CFN resource.

### 1.2 Current resource inventory

| Logical ID | Type | Purpose | Dependencies | Still needed? |
|---|---|---|---|---|
| `WebUiSecurityGroup` | `AWS::EC2::SecurityGroup` | Ingress TCP `WebUiPort` (default **8080**) from `AllowedIngressCidr` | VPC | **Replace** with app SG (443/80) |
| `KpiSecurityGroup` | `AWS::EC2::SecurityGroup` | Ingress TCP `KpiPort` (default **8000**) | VPC | **Remove** after merge |
| `WebUiInstanceRole` | `AWS::IAM::Role` | Runtime role for webui: SSM core, artifact S3 read, CFN DescribeStackResource, API Gateway GET key, Kinesis PutRecord(s), SSM auth params, Lambda Get/Update `imageprocessor` | Artifact bucket, data stack names | **Merge + extend** into Application role (see §10–11) |
| `WebUiInstanceProfile` | `AWS::IAM::InstanceProfile` | Profile for webui | Role | **Replace** |
| `KpiInstanceRole` | `AWS::IAM::Role` | Runtime role for KPI: SSM, artifact S3, Logs Insights, CloudWatch metrics, DynamoDB Query/DescribeTable | Log groups, DDB table/GSI | **Merge** into Application role |
| `KpiInstanceProfile` | `AWS::IAM::InstanceProfile` | Profile for KPI | Role | **Remove** after merge |
| `WebUiInstance` | `AWS::EC2::Instance` | Host for web-ui FastAPI/uvicorn (HTTPS self-signed) | SG, profile, subnet, AMI, UserData→S3 bootstrap | **Replace** with ApplicationInstance |
| `KpiInstance` | `AWS::EC2::Instance` | Host for KPI FastAPI/uvicorn HTTP | Same pattern | **Remove** after merge |
| `WebUiElasticIp` + Association | `AWS::EC2::EIP` (+Assoc) | Stable public IP for webui | Instance | **Collapse to 1** EIP |
| `KpiElasticIp` + Association | `AWS::EC2::EIP` (+Assoc) | Stable public IP for KPI | Instance | **Remove** |
| `SchedulerRole` | `AWS::IAM::Role` | `ec2:StartInstances` / `StopInstances` on both instance ARNs | Both instances | **Update** ARN list to one instance |
| `ScheduleStartMorning` / `ScheduleStopMorning` / `ScheduleStartAfternoon` / `ScheduleStopAfternoon` | `AWS::Scheduler::Schedule` | Mon–Fri KST 09/11/13/15 start/stop | SchedulerRole | **Keep**, retarget to one instance |
| Outputs | WebUi/Kpi IDs, EIPs, URLs, schedule summary | Operator convenience | Instances/EIPs | **Rewrite** for Application |

### 1.3 Parameters (current)

| Parameter | Default | Notes |
|---|---|---|
| `VpcIdParameter` / `SubnetIdParameter` | (required) | Default VPC public subnet |
| `ImageIdParameter` | AL2023 latest x86_64 SSM | |
| `InstanceTypeParameter` | `t3.micro` | May be tight for merged host; see §6 |
| `AllowedIngressCidrParameter` | `0.0.0.0/0` | Demo open |
| `WebUiPortParameter` | `8080` | Currently public |
| `KpiPortParameter` | `8000` | Currently public |
| `RootVolumeSizeGbParameter` | `8` | Root only; no data volume |
| `AppArtifactS3BucketParameter` / prefix | code bucket / `apps/` | Bootstrap + tarballs |
| `DataStackNameParameter` | `video-analyzer-stack` | Runtime read |
| `DataApiStageNameParameter` | `development` | |
| `DataDdbTableNameParameter` / GSI | `EnrichedFrame` + GSI | KPI |
| `KinesisStreamNameParameter` | `FrameStream` | Webui PutRecord |
| `WebUiAuthSsmPrefixParameter` | `/video-analyzer/webui` | Login secrets |

### 1.4 Storage / networking behavior

| Item | Current behavior |
|---|---|
| Root EBS | gp3, encrypted, **DeleteOnTermination: true**, 8 GiB × 2 |
| Data EBS | **None** — SQLite defaults to app directory on root FS |
| Public IP | Auto-assign at launch + **EIP association** (stable URL) |
| SSH | **Not opened** — SSM Session Manager only |
| HTTPS | Webui: **uvicorn + self-signed cert** on :8080. KPI: **plain HTTP** :8000. **No Nginx, no ALB, no ACM** in CFN |
| code-server | **Not in CFN**. Present only on the out-of-band dev/kiosk host used during recent product work |

### 1.5 UserData / bootstrap (duplication)

Both instances use a **thin UserData loader**:

1. Ensure AWS CLI  
2. `aws s3 cp …/bootstrap.sh`  
3. Run bootstrap with env (ports, data stack, artifact bucket)

| Concern | Web UI host | KPI host | Duplicated? |
|---|---|---|---|
| Package install (python3.12, pip) | Yes | Yes | **Yes** |
| Artifact download from S3 | `apps/webui/*` | `apps/kpi/*` | Parallel pattern |
| Python venv | `/opt/webui/venv` | `/opt/kpi/venv` | **Yes** (separate trees — good for merge) |
| Systemd unit | `webui.service` | `kpi-dashboard.service` | Separate names — good |
| Process user | `webui` | `kpi` | Separate — good |
| TLS | Self-signed `/etc/webui/tls.*` | None | Webui-only |
| Env file | `/etc/webui.env` | `/etc/kpi-dashboard.env` | Separate — good |
| Bind address | `0.0.0.0:${WEBUI_PORT}` + SSL | `0.0.0.0:${KPI_PORT}` | Both public today |
| IAM | WebUi role | Kpi role | Overlapping SSM; different data APIs |
| Ingress | 8080 | 8000 | Both public |

**Conclusion:** the two hosts largely **duplicate bootstrap/runtime scaffolding** but already isolate install trees (`/opt/webui` vs `/opt/kpi`). That makes co-location straightforward.

### 1.6 Historical vs product reality gap

CFN + `docs/EC2_DEPLOYMENT.md` still describe an older model:

- webui as mostly static SPA + optional capture  
- KPI as separate monitoring box  

Current `web-ui/backend` is a **full application**:

- session auth, kiosk SQLite, STORE/RETRIEVE orchestration, reference-face S3, facecompare invoke, DynamoDB GetItem, etc.

**CFN `WebUiInstanceRole` is therefore incomplete for the current product** (missing DynamoDB GetItem, S3 frames/locker-references R/W/D, Lambda Invoke facecompare). Those permissions exist on some live hackathon roles / reports, but **not** in the EC2 CFN template.

---

## 2. Current Web UI EC2 responsibilities

Confirmed from `web-ui/backend/app.py`, `config.py`, `kiosk_store.py`, `reference_face.py`, systemd unit, bootstrap.

### 2.1 Surfaces served

| Path / concern | Implementation | Needs |
|---|---|---|
| `/` Operator UI | `FileResponse(index.html)` + `/src` static | Local filesystem |
| `/kiosk` Citizen kiosk UI | `FileResponse(kiosk.html)` + shared static | Local filesystem |
| Root assets | logo/favicon whitelist | Local filesystem |
| `/api/login`, `/logout`, `/me` | SessionMiddleware + PBKDF2 | Secrets (SSM→env), cookie config |
| code-server cookie compat | `CodeServerSessionCookieCompatMiddleware` | Harmless if unused |
| `/api/config` | CFN DescribeStackResource + API Gateway GetApiKey | IAM runtime |
| `/api/capture-frame` | Kinesis PutRecord (pickled frame + CaptureId) | IAM + network |
| `/api/detect-labels` GET/POST | Lambda Get/UpdateFunctionConfiguration on `imageprocessor` | IAM |
| `/api/kiosk/*` | SQLite locker/transaction state machine | **Persistent local disk** |
| STORE face-verify | Lambda Invoke `facecompare` (ID vs FRAME-A) | IAM |
| STORE complete | DynamoDB GetItem + S3 copy to `locker-references/<tx>/reference.jpg` + SQLite STORED | IAM + disk |
| RETRIEVE start | SQLite lookup + Lambda Invoke (reference S3 vs FRAME-B) + status RETRIEVING | IAM + disk |
| RETRIEVE complete | SQLite RETRIEVED + optional S3 delete reference | IAM + disk |
| `/healthz` | liveness | none |

### 2.2 Requirements matrix

| Capability | Local persistent state | IAM | Local FS | Network egress |
|---|---|---|---|---|
| Static/operator/kiosk UI | No | No | App tree | No |
| Session auth | Cookie only (no server session store) | SSM read at boot | `/etc/webui.env` | No (after boot) |
| Kiosk SQLite APIs | **Yes** (`kiosk.db`) | No | DB path | No |
| Capture → Kinesis | No | `kinesis:PutRecord(s)` | Temp only | AWS API |
| API config | Cache in memory | `cloudformation:DescribeStackResource`, `apigateway:GET` | No | AWS API |
| DetectLabels toggle | No | `lambda:Get/UpdateFunctionConfiguration` | No | AWS API |
| Face verify / retrieve gate | Session binds verified frame_id | `lambda:InvokeFunction` facecompare | No image persist | AWS API |
| Reference face durable copy | SQLite stores keys | S3 Get/Head/Copy/Put/Delete + DDB GetItem | No image on disk | AWS API |

### 2.3 SQLite fields that must not be lost

Critical durable fields (from `kiosk_store.py` / product flow):

- `retrieval_code`, `locker_id`, `status`  
- `reference_frame_id`, `reference_face_s3_key`  
- timestamps / reservation expiry  

Default path today: `web-ui/backend/data/kiosk.sqlite3` (root volume).  
`.env.example` already contemplates `KIOSK_DB_PATH=/var/lib/webui/kiosk.sqlite3` — not yet a dedicated volume in CFN.

### 2.4 Process binding (today)

```text
uvicorn app:app --host 0.0.0.0 --port ${WEBUI_PORT} \
  --ssl-keyfile /etc/webui/tls.key --ssl-certfile /etc/webui/tls.crt
```

- Public bind + TLS in process  
- Camera (`getUserMedia`) needs secure context → self-signed HTTPS chosen for demo  

---

## 3. Current Dashboard (KPI) EC2 responsibilities

Sources: `kpi-dashboard/backend/app.py`, `kpis.py`, `config.py`, `deploy/*`.

| Topic | Finding |
|---|---|
| Language/runtime | Python 3.12 preferred, FastAPI + uvicorn + boto3 |
| Listening port | Default **8000** (`KPI_PORT`) |
| Bind today | `0.0.0.0` (public) |
| Static vs dynamic | Hybrid: FastAPI API + `StaticFiles` mount of `frontend/` at `/` |
| Needs own EC2? | **No technical need** for separate host at demo scale |
| CPU/memory | Light: TTL-cached polls (default 60s), 1h Logs Insights lookback; low concurrent load |
| Local state | In-memory TTL cache only — **no durable local DB** |
| Startup | `uvicorn app:app --host 0.0.0.0 --port ${KPI_PORT}` from `/opt/kpi/backend` |
| AWS permissions | Logs Insights Start/Get/Stop; `cloudwatch:GetMetricData`; DynamoDB Query + DescribeTable on EnrichedFrame + GSI |
| Coexist with FastAPI? | **Yes** — separate port, user, venv, unit, working directory |

### 3.1 Frontend path behavior (important for Nginx)

- `index.html` uses relative `href="src/app.css"`  
- JS uses relative `fetch("api/kpis")`  

Under Nginx prefix `/dashboard/` with proxy strip to upstream root, relative URLs work **if** the browser URL is `/dashboard/` (trailing slash). Need redirect `/dashboard` → `/dashboard/`.

No Node build step; pure static + Python backend.

---

## 4. Whether one-EC2 merge is feasible

### Classification: **FEASIBLE WITH SMALL CHANGES**

| Conflict area | Assessment |
|---|---|
| Ports | **No conflict** if FastAPI **8080** and dashboard **8000** stay loopback; public only 443/80 |
| Runtime deps | Both Python FastAPI/uvicorn/boto3 — compatible; **separate venvs** already |
| Node | Not required for either runtime path |
| systemd names | `webui` / `kpi-dashboard` already distinct; propose rename webui → `kiosk-fastapi` |
| Working dirs | `/opt/webui` vs `/opt/kpi` — no clash |
| IAM | Must **union** roles and **add** missing kiosk runtime actions (not a hard blocker) |
| CPU/memory | `t3.micro` may be tight under concurrent capture + KPI Logs Insights; prefer **`t3.small`** for demo reliability (small change) |
| Filesystem | Separate `/opt/*`; add `/data/kiosk` mount |
| Security groups | Collapse to one SG; **close** 8080/8000 to internet |

Why not “FEASIBLE with zero changes”: Nginx introduction, loopback binds, EBS mount, IAM gaps for current product, systemd env `KIOSK_DB_PATH`, HTTPS terminal move from uvicorn→Nginx, scheduler/EIP collapse, bootstrap rewrite.

Why not “NOT RECOMMENDED”: no multi-tenant scale requirement; dashboard is read-only monitoring; single-writer SQLite matches single host; hackathon prefers one public URL.

---

## 5. Recommended target architecture

### 5.1 Logical resources (proposed)

Only resources actually needed:

| Logical ID | Type | Purpose |
|---|---|---|
| `ApplicationSecurityGroup` | SecurityGroup | 443 (+ optional 80) from demo CIDR; no 8080/8000 public |
| `ApplicationInstanceRole` | IAM Role | **Runtime-only** least privilege (union of app needs) |
| `ApplicationInstanceProfile` | InstanceProfile | |
| `ApplicationInstance` | EC2 Instance | Single host: Nginx + FastAPI + Dashboard |
| `ApplicationElasticIp` | EIP | One stable demo URL |
| `ApplicationElasticIpAssociation` | EIPAssociation | |
| `ApplicationDataVolume` | EBS Volume | Persistent SQLite (and optional app data) |
| `ApplicationDataVolumeAttachment` | VolumeAttachment | e.g. `/dev/xvdf` → mount `/data/kiosk` |
| `SchedulerRole` + 4 schedules | existing pattern | Retarget to `ApplicationInstance` only |

Optional (not required for demo): ALB, ACM, Route53, second EIP, separate code-server SG (if code-server stays only on the out-of-band host, leave it out of this stack).

### 5.2 Instance naming / role

- **Name tag:** `video-analyzer-app`  
- **Logical ID:** `ApplicationInstance`  
- **Instance profile role:** `ApplicationInstanceRole`  
- **Suggested type:** `t3.small` (demo), parameter still overridable  

### 5.3 Process layout

```text
Application EC2
├── nginx          :443 (public), optional :80 → 443
├── kiosk-fastapi  127.0.0.1:8080  (HTTP to Nginx; TLS terminated at Nginx)
├── kpi-dashboard  127.0.0.1:8000  (HTTP loopback)
├── systemd units
└── /data/kiosk/kiosk.db  (EBS)
```

Data plane remains in `video-analyzer-stack`. Application talks to AWS APIs with instance role.

---

## 6. Nginx routing

### 6.1 Preferred public path

```text
Browser / Kiosk
    │
  HTTPS :443
    │
  Nginx (TLS terminate)
    │
    ├── /              → http://127.0.0.1:8080/          (operator UI)
    ├── /kiosk         → http://127.0.0.1:8080/kiosk
    ├── /src/          → http://127.0.0.1:8080/src/
    ├── /api/          → http://127.0.0.1:8080/api/
    ├── /healthz       → http://127.0.0.1:8080/healthz
    └── /dashboard/    → http://127.0.0.1:8000/          (strip prefix)
```

### 6.2 Internal ports

| Service | Bind | Public? |
|---|---|---|
| Nginx | `0.0.0.0:443` (and optional `:80`) | Yes |
| FastAPI / kiosk | `127.0.0.1:8080` | **No** |
| KPI dashboard | `127.0.0.1:8000` | **No** |
| SQLite | file path only | **No** |

### 6.3 Frontend relative URL compatibility

| Client | API base behavior | Behind Nginx at site root |
|---|---|---|
| Operator `app.js` | `getApiBaseUrl()` → `'' + '/api'` unless `/proxy/<port>` | **Works** at `/` |
| Kiosk `kiosk.js` | same | **Works** at `/kiosk` |
| KPI `app.js` | relative `api/kpis` | **Works** under `/dashboard/` with prefix strip + trailing-slash redirect |

**Do not require** `/proxy/8080/` in the final demo path.

**Proxy headers:** set `Host`, `X-Forwarded-Proto https`, `X-Forwarded-For` so session `Secure` cookies and future absolute URL generation remain correct. With TLS at Nginx and app HTTP loopback, set `WEBUI_SESSION_HTTPS_ONLY=true` still — browser sees HTTPS origin.

**Optional cookie Path:** keep default `/` so one session works for `/` and `/kiosk`.

### 6.4 Do not expose uvicorn

- No security-group rule for 8080 or 8000  
- systemd bind addresses must be loopback (change from current `0.0.0.0`)  

---

## 7. code-server separation

| Concern | Decision |
|---|---|
| Final demo traffic path | Browser → **Nginx** → app (**not** code-server `/proxy/8080`) |
| code-server on ApplicationInstance | **Not required** for production/demo path. May remain only on the existing **development** host |
| Remove code-server from this env? | **No** (explicit non-goal of this task) |
| Cookie compat middleware | **Keep temporarily** — harmless on direct Nginx path; classify later for removal after direct-Nginx E2E |
| Frontend `/proxy/\d+` prefix | Can remain for back-compat; unused when path has no `/proxy/` |

Session cookie must not **depend** on the compatibility workaround for the demo path; middleware is a safety net only.

---

## 8. systemd design

### 8.1 Units (conceptual)

#### `kiosk-fastapi.service` (rename from `webui.service`)

```ini
[Unit]
Description=Kiosk/Operator FastAPI (loopback)
After=network-online.target data-kiosk.mount
Wants=network-online.target
Requires=data-kiosk.mount

[Service]
Type=simple
User=webui
WorkingDirectory=/opt/webui/backend
EnvironmentFile=/etc/webui.env
# Loopback only; TLS at Nginx
ExecStart=/opt/webui/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8080
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

`/etc/webui.env` must include at least:

- `KIOSK_DB_PATH=/data/kiosk/kiosk.db`  
- `WEBUI_SESSION_HTTPS_ONLY=true`  
- `AWS_DEFAULT_REGION`, `KINESIS_STREAM`, `DATA_STACK`, `DATA_API_STAGE`  
- auth secrets from SSM  
- frame bucket / table / facecompare names as needed  

#### `kpi-dashboard.service` (keep name; change bind)

```ini
ExecStart=/opt/kpi/venv/bin/uvicorn app:app --host 127.0.0.1 --port ${KPI_PORT}
```

`After=network-online.target` is enough (no SQLite dependency).

#### `nginx.service`

- Starts after network  
- Optional soft dependency: `Wants=` app units, but Nginx can start first and return 502 until backends are up  

#### Mount unit (example)

`data-kiosk.mount` or fstab UUID entry:

```text
UUID=<vol-uuid>  /data/kiosk  xfs  defaults,nofail  0  2
```

For hackathon safety, prefer **fail closed** for the app (`Requires=` mount) rather than silent root-disk SQLite if mount fails — or use `nofail` only if operators understand fallback risk. Recommendation: **require mount** for kiosk-fastapi.

### 8.2 Do not

- Run uvicorn manually for demo  
- Bind app ports on `0.0.0.0`  
- Put secrets in UserData plaintext (keep SSM → env file mode)  

---

## 9. Dedicated EBS / SQLite design

### 9.1 Resources

| Item | Proposal |
|---|---|
| Volume | `ApplicationDataVolume` gp3, encrypted, modest size (e.g. **8–10 GiB** — SQLite is small; headroom for logs/backups) |
| Attach | `/dev/xvdf` (or NVMe name handled in bootstrap) |
| Mount | `/data/kiosk` |
| DB path | **`/data/kiosk/kiosk.db`** |
| Owner | `webui:webui`, mode `750` dir / `640` db after create |
| Env | `KIOSK_DB_PATH=/data/kiosk/kiosk.db` |

### 9.2 Lifecycle policies

| Policy | Recommendation | Why |
|---|---|---|
| `DeletionPolicy` | **`Snapshot`** (preferred) or **`Retain`** | Instance/stack delete must not silently destroy transaction state without a recovery artifact |
| `UpdateReplacePolicy` | **`Snapshot`** or **`Retain`** | Instance replacement must not wipe SQLite |
| Root volume | Keep `DeleteOnTermination: true` | Disposable OS disk |

**Operational impact of Retain:** stack delete can leave **orphan volumes** (and snapshots if Snapshot policy). Operators must list/delete unused volumes/snapshots after demos to control cost. Document cleanup checklist; do not silently Retain forever without ops ownership.

**CloudFormation note:** changing instance logical ID or immutable properties replaces the instance; **attached data volume with Retain/Snapshot can be reattached** if attachment is designed carefully. Parallel migration (new instance + new volume, optional copy of DB) is still safer than in-place replace.

### 9.3 Bootstrap ordering

1. Wait for block device  
2. If unformatted → `mkfs` once (marker file or blkid check — **idempotent**)  
3. Mount `/data/kiosk`  
4. `chown` for app user  
5. Write env with `KIOSK_DB_PATH`  
6. Start `kiosk-fastapi` only after mount  

### 9.4 Future production evolution (not now)

```text
SQLite (single ApplicationInstance)
    →  RDS PostgreSQL (multi-AZ / multi-host)
```

Do **not** introduce RDS in this migration.

---

## 10. IAM runtime policy design (ApplicationInstanceRole)

### 10.1 Runtime permissions required by **current code**

| Area | Actions | Resource scope |
|---|---|---|
| SSM admin access | Managed `AmazonSSMManagedInstanceCore` | — |
| Artifact bootstrap | `s3:GetObject` | `apps/webui/*`, `apps/kpi/*` (and optional `apps/common/*`) |
| Auth secrets boot | `ssm:GetParameter(s)` + `kms:Decrypt` via SSM | `/video-analyzer/webui/*` |
| Kinesis capture | `kinesis:PutRecord`, `PutRecords` | stream `FrameStream` |
| API config | `cloudformation:DescribeStackResource` | stack `video-analyzer-stack` |
| API config | `apigateway:GET` | `/apikeys/*` |
| DetectLabels switch | `lambda:GetFunctionConfiguration`, `UpdateFunctionConfiguration` | function `imageprocessor` only |
| Face compare | **`lambda:InvokeFunction`** | function `facecompare` only |
| Frame metadata | **`dynamodb:GetItem`** | table `EnrichedFrame` |
| Reference faces | **`s3:GetObject`, `HeadObject`, `PutObject`, `DeleteObject`** (Copy uses Get+Put) | frames bucket `frames/*` + `locker-references/*` |
| KPI Logs | `logs:StartQuery` | log groups imageprocessor/framefetcher/facecompare |
| KPI Logs | `logs:GetQueryResults`, `StopQuery` | `*` (IAM limitation) |
| KPI metrics | `cloudwatch:GetMetricData` | `*` (IAM limitation) |
| KPI DDB | `dynamodb:Query`, `DescribeTable` | EnrichedFrame + GSI |

Optional hardening: split `ListBucket` only if needed for tooling; app code uses head/get/copy/delete on known keys — prefer object-level ARNs with prefix conditions where possible.

### 10.2 Parameterize bucket/name (do not hardcode only in IAM)

CFN should accept `FrameS3BucketNameParameter` (or discover carefully) so the role does not rely solely on the default string in `config.py`.

---

## 11. Deployment-vs-runtime IAM separation

### Runtime role (ApplicationInstanceRole) — **include only §10**

### Must **not** put on ApplicationInstanceRole

| Forbidden / avoid on runtime | Why |
|---|---|
| `cloudformation:CreateStack` / `DeleteStack` / `UpdateStack` | Deployment, not serving traffic |
| `lambda:CreateFunction` / `DeleteFunction` / publish zip | Deployment |
| `iam:*` or broad PassRole | Privilege escalation |
| `s3:*` on unrelated buckets | Overbroad |
| Unscoped `dynamodb:*` / `kinesis:*` | Overbroad |
| Account-wide admin patterns used on some **dev** kiosk hosts (`AwsFaceDetectionKioskRole` style broad perms) | Dev convenience ≠ demo runtime |

### Developer / deployment role (separate concern)

- Run `pynt publishapps`, `createec2stack`, data-stack deploys from **operator workstation** or a dedicated CI/deploy principal  
- If someone still deploys **from** an EC2 (historical habit), use a **separate** instance profile / assumed role — never merge into ApplicationInstanceRole  

**Bootstrap artifact read** (`s3:GetObject` on `apps/*`) is acceptable on the instance as **install-time runtime**, distinct from stack-authoring permissions.

---

## 12. Security group design

### Inbound (smallest reasonable demo set)

| Port | Source | Purpose |
|---|---|---|
| **443/tcp** | `AllowedIngressCidr` | HTTPS demo users |
| **80/tcp** (optional) | same | Redirect to HTTPS only |
| SSH 22 | **Do not open** | Keep SSM-only ops model |

### Explicitly do **not** open

| Port / service | Reason |
|---|---|
| 8080 | FastAPI internal |
| 8000 | Dashboard internal |
| SQLite | Local file |
| Kinesis/DynamoDB/S3 | AWS public endpoints via instance egress, not inbound |

### Egress

- Default allow (or restrict to HTTPS 443 for AWS APIs + S3) — for demo, default egress is acceptable  

### code-server

- If kept only on the **existing dev host**, leave Application SG free of code-server ports  
- If ever installed on ApplicationInstance for emergency coding, treat as **separate operational risk** (auth, IP allowlist) — not part of app security model  

---

## 13. Dashboard merge strategy

### Decision: **A — separate process on same EC2**

| Option | Verdict |
|---|---|
| **A. Separate process, same EC2** | **Recommended** — proven unit, independent restart, existing IAM/read path |
| B. Pure static under Nginx/FastAPI | **Not recommended now** — needs live AWS aggregation backend |
| C. Cannot merge | **Rejected** — no hard conflict found |

Nginx:

```text
/dashboard/  →  127.0.0.1:8000/
```

Do **not** rewrite the KPI application beyond loopback bind + reverse-proxy headers.

---

## 14. Data-stack boundary

Keep **`video-analyzer-stack`** owning:

- `FrameStream` (Kinesis)  
- `EnrichedFrame` (+ GSI)  
- Frames S3 bucket (lifecycle on `frames/`, `logging/` only; `locker-references/` durable)  
- Lambdas: imageprocessor, framefetcher, facecompare  
- API Gateway + API key  
- Rekognition usage inside Lambdas  

EC2 / Application stack:

- **References by name/ARN parameters** only  
- **No** ImportValue hard coupling required (current design already uses runtime Describe + fixed names)  
- **Must not** duplicate data-plane resources into the EC2 template  

---

## 15. CloudFormation logical-resource proposal

### Keep stack **name**: `video-analyzer-ec2-stack`

**Recommendation:** preserve stack name for tooling compatibility (`createec2stack` / docs / operator muscle memory). Rename **conceptually** to “app stack” in docs; optional future physical rename is a separate migration.

Reason: renaming a deployed stack is not a light operation; internal resource rewrites already force careful migration.

### Conceptual resource map

| Add / keep (new IDs) | Remove later (old IDs) |
|---|---|
| `ApplicationInstance` | `WebUiInstance`, `KpiInstance` |
| `ApplicationInstanceRole` / `Profile` | `WebUiInstanceRole`/`Profile`, `KpiInstanceRole`/`Profile` |
| `ApplicationSecurityGroup` | `WebUiSecurityGroup`, `KpiSecurityGroup` |
| `ApplicationElasticIp` (+ assoc) | `WebUiElasticIp*`, `KpiElasticIp*` |
| `ApplicationDataVolume` (+ attachment) | (none today) |
| Schedules retargeted | same schedule names OK if inputs updated |

---

## 16. Migration / replacement risks

### Critical CloudFormation behaviors

| Change | Effect |
|---|---|
| Rename `WebUiInstance` → `ApplicationInstance` | CFN treats as **delete old + create new** (replacement) |
| Delete `KpiInstance` | **Terminates** KPI EC2 on update |
| New EBS + attachment | Generally additive; AZ must match instance subnet AZ |
| SG / role logical ID renames | Replacement of those resources; instance may replace if profile/SG force new |
| EIP swap | Public endpoint change unless association carefully staged |
| UserData change | Often **instance replacement** depending on property update policy |

### Data-loss risks

| Data | Risk |
|---|---|
| SQLite on **root** volume | **Lost** on instance terminate/replace |
| SQLite on **dedicated EBS** with Retain/Snapshot | Survivable if reattached / restored |
| S3 `locker-references/*` | Safe (data stack bucket) if app cleans only on RETRIEVED |
| In-memory KPI cache | Disposable |
| Session cookies | Reset on secret rotation / domain-IP change |

### Downtime / public IP

- Parallel ApplicationInstance + new EIP → new URL until DNS/bookmark cutover  
- Reusing one existing EIP requires disassociate/associate choreography (brief reachability gap)  
- Self-signed cert SAN must include final public IP (regenerate on IP change)  

### Rollback

- Keep old instances running until Phase B E2E passes  
- Do not delete old logical resources until traffic cutover confirmed  
- Data volume Retain allows rollback without SQLite loss once data lives on EBS  

**Do not perform stack update in this task.**

---

## 17. Phased migration plan (safe)

### Phase A — Parallel ApplicationInstance

1. Publish updated artifacts (webui + kpi bootstrap that support co-location, Nginx, loopback, EBS)  
2. Add Application* resources **alongside** existing WebUi/Kpi (temporary dual-run template or temporary second stack — prefer **same stack additive resources** if capacity allows)  
3. Attach dedicated EBS; bootstrap Nginx + both apps  
4. Smoke: `/healthz`, KPI `/dashboard/`, SSM access  

### Phase B — Full application E2E on ApplicationInstance

1. Login / session on HTTPS (Nginx path, **not** `/proxy/8080`)  
2. `/` operator capture + `/api/config`  
3. `/kiosk` STORE (face-verify → complete → SQLite STORED + S3 reference)  
4. RETRIEVE (code + FRAME-B → open → cleanup)  
5. Dashboard live KPIs  
6. Confirm IAM least-privilege (no AccessDenied on happy path; no deployment actions present)  
7. Confirm SQLite path is on `/data/kiosk` and survives app restart  

### Phase C — Switch demo traffic / public endpoint

1. Publish Application EIP / URL as the demo entry  
2. Update operator docs / bookmarks  
3. Optionally re-point any remaining EIP if reusing addresses  

### Phase D — Remove old dual-instance resources

1. Remove `WebUiInstance`, `KpiInstance`, dual SGs/roles/EIPs from template  
2. Update scheduler to only ApplicationInstance  
3. Stop/disable out-of-band dev host path for demos (optional ops; not automatic)  

**Prefer parallel migration over in-place destructive replacement.**

---

## 18. Expected resources removed (later)

- `WebUiInstance`, `KpiInstance`  
- `WebUiInstanceRole` / `Profile`, `KpiInstanceRole` / `Profile`  
- `WebUiSecurityGroup`, `KpiSecurityGroup`  
- `WebUiElasticIp` (+assoc), `KpiElasticIp` (+assoc)  
- Public exposure of ports 8080/8000  
- Dual root-volume ongoing cost for second instance  
- (Eventually, after E2E) reliance on code-server as the app ingress  

---

## 19. Expected resources added

- `ApplicationInstance` (+ role/profile/SG/EIP)  
- `ApplicationDataVolume` + attachment + mount  
- Nginx packages + TLS material (self-signed demo)  
- systemd: `kiosk-fastapi`, updated `kpi-dashboard`, mount dependency  
- Runtime IAM additions for facecompare invoke, DDB GetItem, S3 reference prefixes  
- Docs / outputs: single `ApplicationUrl` (`https://<eip>/`)  

---

## 20. Open questions / blockers

| # | Question | Impact |
|---|---|---|
| 1 | Is `video-analyzer-ec2-stack` currently CREATE_COMPLETE or DELETE_COMPLETE in the target account? | Parallel-add vs greenfield create |
| 2 | Should the **out-of-band t3.small + code-server** host be decommissioned after cutover, or retained as pure dev? | Cost + confusion |
| 3 | Final instance type: keep `t3.micro` or standardize on **`t3.small`**? | Stability under capture+KPI |
| 4 | EIP strategy: new EIP vs reclaim one existing EIP | URL continuity |
| 5 | Cert SAN regeneration automation when EIP changes | Camera secure context |
| 6 | Exact frames bucket name parameter source of truth | IAM correctness |
| 7 | Whether DetectLabels toggle remains required on demo host | IAM surface |
| 8 | Scheduler windows still appropriate for single always-demo day? | Ops |
| 9 | Volume `DeletionPolicy` choice: Snapshot vs Retain — who owns orphan cleanup? | Cost/ops |
| 10 | When to remove `CodeServerSessionCookieCompatMiddleware` | Code cleanup only after Nginx E2E |

**Blocker for live E2E (known from prior work):** data-stack resources must exist (`video-analyzer-stack`) for STORE/RETRIEVE; design does not recreate them.

---

## HTTPS design (Step 9 detail)

| Option | Fit | Tradeoffs |
|---|---|---|
| **B. Nginx self-signed demo cert (Recommended)** | No domain today; matches getUserMedia need; moves TLS off uvicorn | Browser warning; regenerate SAN on IP change |
| A. Reuse existing uvicorn cert files at Nginx | Possible short-term | Still self-signed; need to stop double-TLS |
| C. ALB + ACM | Only if real domain/cert exists | Extra cost/complexity; **not justified** without domain |

Current CFN: **no ALB/ACM/Nginx**; webui self-signed on uvicorn; KPI HTTP.  
Dev host: code-server cert on 443 with `/proxy/8080` — **not** the target path.

**Recommendation:** Option **B** — Nginx terminates TLS with a first-boot self-signed cert (SAN: public IP + localhost). HTTP→HTTPS redirect optional. No invented domain.

---

## Cost analysis (structural only — no live price quotes)

| Current structure | Proposed structure |
|---|---|
| 2× EC2 compute (webui + KPI) | **1×** Application EC2 |
| 2× root EBS | **1×** root EBS + **1×** small data EBS |
| 2× EIP (CFN) (+ possible 3rd on out-of-band host) | **1×** EIP for app stack |
| Duplicated runtime overhead | Single OS, two small processes |

Structural savings: fewer running instances, fewer root volumes, fewer EIPs.  
Added: modest data volume (persistent even when stopped — intentional).  
Data-plane costs (Kinesis shard, etc.) **unchanged**.

---

## Final proposed architecture (ASCII)

```text
Kiosk / Browser
       |
      HTTPS :443
       |
     Nginx  (TLS terminate, public SG)
       |
+----------------------------+
| Application EC2            |
|                            |
| FastAPI 127.0.0.1:8080     |
|  /  /kiosk  /api/*         |
| Operator UI + Kiosk UI     |
|                            |
| Dashboard 127.0.0.1:8000   |
|  /dashboard/ via Nginx     |
|                            |
| systemd:                   |
|  kiosk-fastapi.service     |
|  kpi-dashboard.service     |
|  nginx.service             |
|                            |
| /data/kiosk/kiosk.db       |
+--------------+-------------+
               |
        ApplicationDataVolume (EBS)
               |
               +------------------------------+
               |                              |
        video-analyzer-stack              AWS APIs (runtime role)
               |                              |
        Kinesis FrameStream            lambda:Invoke facecompare
        DynamoDB EnrichedFrame         lambda:Get/Update imageprocessor
        S3 frames/*                    kinesis:PutRecord
        S3 locker-references/*         dynamodb:GetItem/Query
        API Gateway (config read)      s3:Get/Put/Delete (scoped)
                                       cfn:DescribeStackResource
                                       apigateway:GET apikeys
                                       logs/cloudwatch (KPI)
```

---

## Files inspected

| Path | Why |
|---|---|
| `aws-infra/aws-infra-ec2-cfn.yaml` | Current EC2 stack resources, IAM, SG, EIP, scheduler, UserData |
| `aws-infra/aws-infra-cfn.yaml` | Data-stack boundary and resource names |
| `aws-infra/userdata/webui-bootstrap.sh` | Webui install, TLS, env, systemd enable |
| `aws-infra/userdata/webui-genconfig.sh` | Legacy apigw.js generator (superseded by `/api/config` in app) |
| `aws-infra/userdata/webui.service` | Current public bind + uvicorn TLS |
| `kpi-dashboard/deploy/kpi-bootstrap.sh` | KPI install |
| `kpi-dashboard/deploy/kpi-dashboard.service` | KPI public bind |
| `kpi-dashboard/backend/app.py`, `config.py`, `kpis.py`, `requirements.txt` | Dashboard runtime needs |
| `kpi-dashboard/frontend/src/app.js`, `index.html` | Relative URL behavior |
| `web-ui/backend/app.py`, `config.py`, `kiosk_store.py`, `reference_face.py` | App responsibilities + IAM needs |
| `web-ui/backend/session_cookie_compat.py` | code-server cookie workaround (keep for now) |
| `web-ui/backend/requirements.txt`, `.env.example` | Deps + `KIOSK_DB_PATH` guidance |
| `web-ui/src/app.js`, `kiosk.js` | API base URL / `/proxy` handling |
| `build.py` (EC2 tasks, `publishapps`, `setwebuiauth`) | Deploy workflow |
| `docs/EC2_DEPLOYMENT.md` | Operator model, schedule, cost notes |
| `cost-explain.md` | Live-account structural inventory notes |
| Prior result reports (auth, cookie, reference face) | Product flow + IAM gap context |
| Working tree: `git status` / `git diff --stat` | Preserve uncommitted product work |

---

## Confirmation of non-actions

| Action | Status |
|---|---|
| CloudFormation template edit | **Not done** |
| AWS create/update/delete | **Not done** |
| IAM / SG live changes | **Not done** |
| Application restart | **Not done** |
| Git commit | **Not done** |
| Uncommitted work preserved | **Yes** |

---

## Quick reference — design decisions

1. **Current WebUiInstance role (CFN):** runtime webui + incomplete for kiosk face/S3/DDB/invoke  
2. **Current DashboardInstance role (CFN `KpiInstanceRole`):** read-only logs/CW/DDB GSI + artifact/SSM  
3. **Feasibility:** FEASIBLE WITH SMALL CHANGES  
4. **Final instance:** `ApplicationInstance` / role `ApplicationInstanceRole`  
5. **Dashboard shares instance:** Yes (Strategy A)  
6. **Internal ports:** FastAPI `127.0.0.1:8080`, Dashboard `127.0.0.1:8000`  
7. **Nginx routes:** `/`, `/kiosk`, `/api/`, `/src/`, `/dashboard/`  
8. **EBS mount:** `/data/kiosk`  
9. **SQLite path:** `/data/kiosk/kiosk.db`  
10. **Runtime IAM:** §10 table  
11. **Remove from runtime:** stack mutate, lambda create/delete, iam*, unscoped s3/ddb, broad dev role  
12. **Inbound SG:** 443 (+ optional 80); not 8080/8000/22  
13. **code-server in app path:** No  
14. **CFN add:** Application* + data volume (+ Nginx bootstrap)  
15. **CFN remove later:** dual WebUi/Kpi instances, dual SG/role/EIP  
16. **Risks:** replacement deletes old EC2; SQLite loss without EBS; EIP/URL change; downtime if not parallel  
17. **Phases:** A parallel → B E2E → C cutover → D delete old  
18. **Report file:** this document  

---

## Phase A Implementation Result

**Status:** CODE COMPLETE — static validation only (no AWS stack update/create, no commit)  
**Date:** 2026-08-11  
**Invariant:** `old resources preserved=true`

### Application resources added (CloudFormation)

| Logical ID | Type |
|---|---|
| `ApplicationSecurityGroup` | `AWS::EC2::SecurityGroup` |
| `ApplicationInstanceRole` | `AWS::IAM::Role` |
| `ApplicationInstanceProfile` | `AWS::IAM::InstanceProfile` |
| `ApplicationInstance` | `AWS::EC2::Instance` |
| `ApplicationDataVolume` | `AWS::EC2::Volume` |
| `ApplicationDataVolumeAttachment` | `AWS::EC2::VolumeAttachment` |
| `ApplicationElasticIp` | `AWS::EC2::EIP` |
| `ApplicationElasticIpAssociation` | `AWS::EC2::EIPAssociation` |

Legacy **unchanged and still present:** `WebUiInstance`, `KpiInstance`, dual SGs, dual roles/profiles, dual EIPs, EventBridge schedules targeting **only** legacy instance IDs.

### New parameters

| Parameter | Default | Notes |
|---|---|---|
| `ApplicationInstanceTypeParameter` | `t3.small` | Does not resize legacy `InstanceTypeParameter` (still `t3.micro`) |
| `ApplicationDataVolumeSizeGbParameter` | `8` | Encrypted gp3 |
| `ApplicationFastApiPortParameter` | `8080` | Loopback only |
| `ApplicationKpiPortParameter` | `8000` | Loopback only |
| `FrameS3BucketNameParameter` | *(required)* | Explicit frames bucket for IAM + env |
| `ImageprocessorFunctionNameParameter` | `imageprocessor` | |
| `FaceCompareFunctionNameParameter` | `facecompare` | |

### IAM policy (runtime)

- Artifact `s3:GetObject`: `apps/webui/*`, `apps/kpi/*`, `apps/application/*`
- SSM auth read + conditional `kms:Decrypt` via SSM
- Kinesis PutRecord(s) on `FrameStream`
- CFN DescribeStackResource on data stack; `apigateway:GET` `/apikeys/*`
- Lambda Get/Update `imageprocessor`; Invoke `facecompare`
- DynamoDB GetItem on EnrichedFrame; Query/DescribeTable + GSI (KPI)
- S3 Get/Put/Delete on `frames/*` and `locker-references/*` only
- Logs StartQuery (3 Lambda log groups); GetQueryResults/StopQuery `*`; GetMetricData `*`

**Excluded (deployment):** Create/Update/DeleteStack, iam:*, Lambda create/delete, broad s3/ddb/kinesis.

### EBS strategy

- Dedicated volume, encrypted gp3, size parameter (default 8 GiB)
- AZ: `!GetAtt ApplicationInstance.AvailabilityZone` (attachment after instance)
- Device hint: `/dev/xvdf` (bootstrap Nitro-safe discovery; **no** UserData `!Ref` volume — avoids circular dependency)
- Mount: `/data/kiosk` via UUID fstab
- SQLite: `/data/kiosk/kiosk.db` (`KIOSK_DB_PATH`, fail closed)
- `DeletionPolicy: Snapshot` / `UpdateReplacePolicy: Snapshot`
- Operator owns snapshot/orphan cleanup cost

### Bootstrap / systemd / Nginx

| Artifact | Path |
|---|---|
| Bootstrap | `aws-infra/userdata/application-bootstrap.sh` |
| FastAPI unit | `aws-infra/userdata/kiosk-fastapi.service` → `127.0.0.1:8080` |
| KPI unit (loopback) | `aws-infra/userdata/kpi-dashboard-loopback.service` → `127.0.0.1:8000` |
| Nginx conf | `aws-infra/nginx/application.conf` |
| TLS | Nginx self-signed; `/opt/app/regenerate-tls.sh` after EIP SAN mismatch |

Routes: `/` `/kiosk` `/api/` `/src/` `/healthz` → FastAPI; `/dashboard/` → KPI; `:80` → 308 HTTPS.

### build.py

- `publishapps` default: `webui`, `kpi`, **`application`**
- New read-only helpers: `applicationip`, `applicationstatus`
- Legacy commands unchanged (`createec2stack`, `updateec2stack`, `ec2ip`, `setwebuiauth`)

### Scheduler (Phase A)

- **Not** retargeted to ApplicationInstance
- Legacy Mon–Fri 09/11/13/15 still start/stop **only** WebUi + Kpi
- Application: manual stop via output `ApplicationStopCommand`

### Outputs added

`ApplicationInstanceId`, `ApplicationElasticIp`, `ApplicationUrl`, `ApplicationDashboardUrl`, `ApplicationDataVolumeId`, `ApplicationStopCommand`, `PhaseANote`

### Next phases (not executed)

- **B:** `pynt publishapps` → `updateec2stack`/`createec2stack` → E2E on Application EIP  
- **C:** cutover demo traffic  
- **D:** remove WebUi/Kpi resources; scheduler → Application only  

### AWS mutations this phase

**NONE** (template + scripts + docs only).

---

## Greenfield Pivot

**Date:** 2026-08-11  
**Trigger:** Phase B preflight proved `video-analyzer-ec2-stack` **does not exist** (historical DELETE_COMPLETE only) and `video-analyzer-stack` was also absent (separate restore path).

### Decision

| Option | Outcome |
|---|---|
| Parallel update (WebUi+Kpi preserved + Application) | **Not applicable** — no live dual-host stack |
| Greenfield create of Phase A template (3 EC2s) | **Rejected** — wasteful and not the demo target |
| **Remove legacy WebUi/Kpi CFN resources** | **Selected** — default create = **one ApplicationInstance** |

### Final greenfield shape

```text
video-analyzer-ec2-stack
└── ApplicationInstance (Nginx + FastAPI + KPI)
    + ApplicationElasticIp (1)
    + ApplicationDataVolume (1) → /data/kiosk/kiosk.db
```

- Data plane remains **`video-analyzer-stack`** (separate).  
- Development/kiosk EC2 with code-server remains **outside CFN**.  
- Scheduler (optional via `EnableSchedulerParameter`) targets **ApplicationInstance only**.  
- TLS/EIP first-boot race fixed with **`application-tls-refresh.service`** (bounded oneshot).

### Resource counts (default create)

| Resource | Count |
|---|---|
| ApplicationInstance | 1 |
| ApplicationElasticIp | 1 |
| ApplicationDataVolume | 1 |
| WebUiInstance / KpiInstance | 0 |
| WebUiElasticIp / KpiElasticIp | 0 |

