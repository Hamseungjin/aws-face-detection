# Application Stack Live E2E Result

Date: 2026-08-11  
Account: `115019372648`  
Region: `ap-northeast-2`

This run stopped at the mandatory pre-creation auth/IAM gate. No Application
CloudFormation change set was created or executed, and no EC2/Application resources
were created. Existing uncommitted repository work was preserved and no commit was
made.

## 1. Classification

`APPLICATION_STACK_FAILED_IAM`

## 2. Account/region

- Account: `115019372648`
- Region: `ap-northeast-2`
- Caller: `AwsFaceDetectionKioskRole` on the out-of-band development instance

## 3. Data stack status

- `video-analyzer-stack`: `CREATE_COMPLETE`
- `FrameStream`: `ACTIVE`
- `EnrichedFrame`: `ACTIVE`
- `facecompare`: `Active`, last update successful
- `aws-face-detection-frames-115019372648-apne2`: exists in `ap-northeast-2`

The data stack was read only during this run.

## 4. Application stack status

`video-analyzer-ec2-stack`: does not exist. Creation was intentionally not started
because the auth/IAM gate failed.

## 5. CloudFormation change-set review

No change set was created or executed. Before the stop, the current template passed
`aws cloudformation validate-template` and reported `CAPABILITY_IAM`.

Static template counts:

- `AWS::EC2::Instance`: 1
- `AWS::EC2::EIP`: 1
- `AWS::EC2::Volume` data volume: 1
- `ApplicationInstance`: 1
- `WebUiInstance`: 0
- `KpiInstance`: 0
- legacy WebUi/Kpi resource logical IDs: 0

## 6. Created resource counts

- Application EC2: 0
- Application EIP: 0
- Application data EBS: 0
- Application security groups: 0
- Application runtime roles/profiles: 0
- Scheduler resources: 0

## 7. Application Instance ID

Not created.

## 8. Application EIP

Not allocated.

## 9. Scheduler deployed value

The prepared, gitignored local parameter value is
`EnableSchedulerParameter=false`. Nothing was deployed.

## 10. SG ports

Static template review: inbound TCP 80 and 443 only, sourced from the explicitly
prepared demo value `0.0.0.0/0`. No rules expose 22, 8000, 8080, or code-server.
No Application security group was created.

## 11. Runtime IAM status

The template's `ApplicationInstanceRole` is runtime-only and includes the required
scoped Kinesis, DynamoDB, S3 `frames/*` and `locker-references/*`, facecompare invoke,
imageprocessor configuration, data-stack/API-key read, KPI metrics/log query, SSM auth
read, and artifact-read permissions. It does not include CloudFormation create/update/
delete, `iam:*`, Lambda create/delete, broad S3 access, or broad DynamoDB access.

Deployment is blocked by the current caller IAM:

- observed denial: `ssm:GetParameters` on
  `arn:aws:ssm:ap-northeast-2:115019372648:parameter/video-analyzer/webui/auth-username`
- observed denial: `ssm:DescribeParameters` in `ap-northeast-2`
- visible caller inline policies contain no `ssm:PutParameter`
- observed denial: `iam:ListAttachedRolePolicies`, so no unseen managed grant could be
  verified
- observed denial: `iam:SimulatePrincipalPolicy`, so write access could not be proven
- visible caller policies contain no CloudFormation change-set actions
- read-only EC2 network APIs were denied: `ec2:DescribeVpcs`,
  `ec2:DescribeSubnets`, `ec2:DescribeRouteTables`, and
  `ec2:DescribeInternetGateways`

No IAM policy was broadened. `pynt setwebuiauth` was not run because no plaintext
tester credential was provided and the task forbids printing or inventing credentials.
Local `.env` values were not read or copied into SSM.

## 12. EBS volume/mount status

Not created; `/data/kiosk` Application-volume verification was not applicable.

## 13. SQLite path

Prepared template/runtime value: `/data/kiosk/kiosk.db`. Not deployed.

## 14. Nginx status

Not run; Application instance not created.

## 15. FastAPI status

Not run; Application instance not created.

## 16. KPI status

Not run; Application instance not created.

## 17. TLS/EIP SAN status

Not run; no Application EIP or certificate exists.

## 18. `/healthz`

Not run; Application instance not created.

## 19. Login/session

Not run. Auth SSM readiness could not be established with the current principal.

## 20. `/api/config`

Not run.

## 21. Capture-frame

Not run.

## 22. Exact frame correlation

Not run.

## 23. STORE positive E2E

Not run. No controlled test identity/reference image and live subject were supplied.

## 24. FRAME-A reference result

Not run.

## 25. Reference S3 result

Not run. No biometric object was read, written, displayed, or downloaded.

## 26. RETRIEVE positive E2E

Not run.

## 27. FRAME-B result

Not run.

## 28. Reference cleanup result

Not run.

## 29. Negative-face E2E

Not run; controlled biometric subjects were unavailable.

## 30. Code-only bypass test

Not run because the Application endpoint was not deployed.

## 31. Wrong-code test

Not run because the Application endpoint was not deployed.

## 32. SQLite persistence

Not run; no Application service or data volume exists.

## 33. Dashboard E2E

Not run; Application instance not created.

## 34. Public-port security check

Static template check passed: only 80 and 443 are configured for ingress; 22, 8000,
and 8080 are absent. Live reachability was not applicable because no stack resources
were created.

## 35. Final architecture status

- Data plane: healthy (`video-analyzer-stack` is `CREATE_COMPLETE`)
- Application plane: not created due to the mandatory auth/IAM gate
- Out-of-band development host: unchanged

## 36. Remaining blockers

1. Use a deployment principal that can safely confirm or seed the three auth SSM
   parameters. Seeding requires narrowly scoped `ssm:PutParameter`; verification needs
   narrowly scoped SSM read access without printing values.
2. The deployment principal needs the CloudFormation CREATE change-set workflow
   (`CreateChangeSet`, `DescribeChangeSet`, and `ExecuteChangeSet`, plus normal event/
   stack reads) and the narrowly scoped EC2/IAM actions required by the reviewed
   template. Do not add these permissions to the runtime Application role.
3. Re-run from the change-set gate only after auth presence is proven. The prepared
   VPC/subnet candidate is `vpc-05311e6f3180243c0` /
   `subnet-06563af7aa7d98464`; it came from IMDS on the current public,
   outbound-working development host and must still pass typed CloudFormation
   validation during change-set creation.
4. Positive/negative biometric E2E requires controlled authorized testers and a test
   reference image; none was fabricated or reused automatically.

## 37. AWS mutations performed

Uploaded the current Application artifacts only to the configured project bucket:

- `apps/webui/web-ui.tgz`
- `apps/webui/bootstrap.sh`
- `apps/webui/webui-genconfig.sh`
- `apps/webui/webui.service`
- `apps/kpi/kpi-dashboard.tgz`
- `apps/kpi/bootstrap.sh`
- `apps/kpi/kpi-dashboard.service`
- `apps/application/bootstrap.sh`
- `apps/application/kiosk-fastapi.service`
- `apps/application/kpi-dashboard.service`
- `apps/application/nginx-application.conf`
- `apps/application/application-tls-refresh.sh`
- `apps/application/application-tls-refresh.service`

Required artifact ETags matched current local files/packages. The packages contained no
`.env` files. No data stack mutation, biometric S3 access, EC2 mutation, EBS mutation,
EIP allocation, security-group mutation, SSM write, or change-set creation occurred.

## 38. Git commit

`NONE`
