# Application Deployment IAM Plan

Date: 2026-08-11  
Account: `115019372648`  
Region: `ap-northeast-2`  
Target stack: `video-analyzer-ec2-stack`  
Data stack: `video-analyzer-stack` (`CREATE_COMPLETE`)  
Scheduler deployment value: `false`

This document is a policy proposal only. No IAM policy, role, SSM value, CloudFormation
change set, or stack resource was created or modified during this task.

## 1. Selected deployment IAM architecture

Select **Option B**:

```text
Auth operator (short-lived auth-bootstrap permission)
  └─ ssm:PutParameter + narrowly conditioned kms:Encrypt

ApplicationDeployer
  ├─ read-only network/quota preflight
  ├─ apps/* artifact publish/read
  ├─ CREATE change-set lifecycle for video-analyzer-ec2-stack only
  └─ iam:PassRole for ApplicationStackExecutionRole only
          │
          ▼
ApplicationStackExecutionRole
  ├─ trusted only by cloudformation.amazonaws.com
  ├─ creates/rolls back the reviewed Application stack resources
  └─ creates and passes the generated ApplicationInstanceRole
          │
          ▼
ApplicationInstanceRole
  └─ runtime-only access to the existing data plane and auth/artifact reads
```

AWS documents that a CloudFormation service role supplies the credentials used for stack
resource operations and that the caller must be allowed to pass it. AWS also warns that
the role becomes permanently associated with the stack and can be used by any principal
that can operate that stack. This is why the deployer policy is restricted to one stack,
one execution role, and one CREATE change-set name prefix. See the
[CloudFormation service-role documentation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-iam-servicerole.html).

The current `build.py` does **not** implement this flow. `createec2stack` delegates to
`createstack`, which calls `CreateStack` directly with the caller's credentials and then
waits with `DescribeStacks`. It has no change-set or `RoleARN` support. Therefore the
approved Option B workflow must use the explicit AWS CLI commands in section 15; do not
run `pynt createec2stack` for this deployment.

## 2. Current caller IAM blockers

Caller: `arn:aws:sts::115019372648:assumed-role/AwsFaceDetectionKioskRole/...`

Observed or code/policy-proven gaps:

- denied `ssm:GetParameters` on `/video-analyzer/webui/auth-username`;
- denied `ssm:DescribeParameters`;
- no `ssm:PutParameter` in the visible inline policies;
- denied `ec2:DescribeVpcs`, `DescribeSubnets`, `DescribeRouteTables`,
  `DescribeInternetGateways`, and related preflight reads;
- no `cloudformation:CreateChangeSet`, `DescribeChangeSet`, or `ExecuteChangeSet` in
  the visible inline policies;
- no safe EC2/IAM execution-role boundary for the Application template;
- denied `iam:ListAttachedRolePolicies` and `iam:SimulatePrincipalPolicy`, so unseen
  managed grants could not be established;
- denied the read-only `access-analyzer:ValidatePolicy` action.

Do not add broad deployment permissions to this instance role. Use a separately trusted
human/deployment principal and the dedicated execution role described here.

## 3. Exact SSM auth parameter names

`pynt setwebuiauth` writes exactly:

| Parameter | Type | Source in helper |
|---|---|---|
| `/video-analyzer/webui/auth-username` | `String` | interactive username |
| `/video-analyzer/webui/password-hash` | `SecureString` | PBKDF2-SHA256 hash computed locally |
| `/video-analyzer/webui/session-secret` | `SecureString` | locally generated random secret |

All three calls use `Overwrite=True`. The helper supplies no `KeyId`, `Tier`, tags,
policies, description, or readback request. Therefore the two SecureStrings are standard
parameters encrypted with the account's AWS-managed `alias/aws/ssm` key. AWS documents
that omitting `KeyId` selects `aws/ssm` and that Parameter Store uses KMS `Encrypt` for
standard SecureStrings. See
[SSM SecureString encryption](https://docs.aws.amazon.com/systems-manager/latest/userguide/secure-string-parameter-kms-encryption.html).

The helper never calls `DescribeParameters`, `GetParameter`, or `GetParameters`.

## 4. Auth-bootstrap required actions

Policy: `docs/iam/webui-auth-bootstrap-policy.json`

Required:

- `ssm:PutParameter` on the three exact parameter ARNs;
- `kms:Encrypt` only for the KMS key having `alias/aws/ssm`, only through the Seoul SSM
  endpoint, only for this account, and only when the Parameter Store encryption context
  names `password-hash` or `session-secret`.

Not required by the current helper:

- `ssm:DescribeParameters`;
- `ssm:GetParameter` / `ssm:GetParameters`;
- `kms:Decrypt`;
- `ssm:AddTagsToResource`;
- any quota, IAM, or CloudFormation action.

KMS key ARNs cannot be represented by an alias ARN in an IAM `Resource`; the proposal
uses the account's key ARN pattern plus `kms:ResourceAliases=alias/aws/ssm`,
`kms:ViaService`, caller-account, and SSM `PARAMETER_ARN` encryption-context conditions.
AWS documents both the alias condition and the SSM encryption context:
[KMS alias authorization](https://docs.aws.amazon.com/kms/latest/developerguide/alias-authorization.html),
[Parameter Store encryption context](https://docs.aws.amazon.com/systems-manager/latest/userguide/secure-string-parameter-kms-encryption.html#secure-string-parameter-encryption-context).

## 5. Deployer CloudFormation actions

Policy: `docs/iam/application-deployer-policy.json`

Immediate CREATE workflow:

- `cloudformation:ValidateTemplate` (`Resource: "*"`, because that API has no stack
  resource yet);
- `cloudformation:CreateChangeSet` on
  `stack/video-analyzer-ec2-stack/*`, conditioned on the exact execution-role ARN and
  change-set prefix `application-greenfield-create-*`;
- `cloudformation:DescribeChangeSet` and `ExecuteChangeSet` on the same stack and
  change-set prefix;
- `cloudformation:DescribeStacks`;
- `cloudformation:DescribeStackEvents`;
- `cloudformation:DescribeStackResource` and `DescribeStackResources`;
- `cloudformation:ListStackResources`;
- `cloudformation:GetTemplate`.

`CreateStack`, `UpdateStack`, and `DeleteStack` are deliberately absent. `ListChangeSets`
is unnecessary because the operator uses a known change-set name. `DeleteChangeSet` is
also unnecessary for the approved create/review/execute path; an abandoned change set
must be cleaned up by an administrator or through a separately approved narrow grant.

The artifact bucket's live default encryption was checked read-only and is SSE-S3
`AES256`, so the deployer needs no artifact-bucket KMS permission.

The CloudFormation authorization reference confirms stack-level scoping and the
`cloudformation:RoleArn` and `cloudformation:ChangeSetName` condition keys:
[CloudFormation actions and condition keys](https://docs.aws.amazon.com/service-authorization/latest/reference/list_awscloudformation.html).

The current template requires `CAPABILITY_IAM`, not `CAPABILITY_NAMED_IAM`, because it
does not set fixed IAM `RoleName` or instance-profile names.

## 6. Deployer EC2 read-only actions

The separate preflight statement contains only:

- `ec2:DescribeAccountAttributes`;
- `ec2:DescribeAddresses`;
- `ec2:DescribeAvailabilityZones`;
- `ec2:DescribeInstances`;
- `ec2:DescribeInternetGateways`;
- `ec2:DescribeNetworkAcls`;
- `ec2:DescribeNetworkInterfaces`;
- `ec2:DescribeRouteTables`;
- `ec2:DescribeSecurityGroups`;
- `ec2:DescribeSubnets`;
- `ec2:DescribeVpcs`.

These Describe operations use `Resource: "*"`, as required by EC2 API authorization,
and are region-conditioned to `ap-northeast-2`. They grant no EC2 mutation.

`ec2:DescribeAddresses` shows currently allocated addresses but does not return the
applied quota. For exact EIP capacity the policy separately grants only
`servicequotas:GetServiceQuota` on
`arn:aws:servicequotas:ap-northeast-2:115019372648:ec2/L-0263D0A3`.
`L-0263D0A3` is the EC2-VPC Elastic IP quota; no quota mutation action is included.
See the [Service Quotas authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_service-quotas.html)
and [EC2 quota reference](https://docs.aws.amazon.com/general/latest/gr/ec2-service.html).

## 7. CloudFormation execution-role trust policy

File: `docs/iam/application-cfn-execution-trust.json`

The only trusted principal is:

```json
{
  "Service": "cloudformation.amazonaws.com"
}
```

The only trust action is `sts:AssumeRole`. The role is not trusted by EC2, the current
development instance, a user, or the Application runtime principal.

## 8. CloudFormation execution-role resource actions

Policy: `docs/iam/application-cfn-execution-policy.json`

Derived from the template's enabled resources:

| Template resource | Execution-role capability |
|---|---|
| `AWS::EC2::SecurityGroup` | create, tag, authorize/revoke ingress, describe, rollback/delete |
| `AWS::IAM::Role` | create/get/tag/update trust, attach only `AmazonSSMManagedInstanceCore`, manage inline policies, rollback/delete |
| `AWS::IAM::InstanceProfile` | create/get/tag, add/remove the generated role, rollback/delete |
| `AWS::EC2::Instance` | resolve/describe AMI and EC2 state, `RunInstances`, tag, modify/terminate for update or rollback |
| `AWS::EC2::Volume` | create only encrypted gp3-compatible volume in the candidate AZ, describe/modify/delete |
| `AWS::EC2::VolumeAttachment` | attach/detach the stack-tagged instance and volume |
| `AWS::EC2::EIP` | allocate/tag/describe/release the stack EIP |
| `AWS::EC2::EIPAssociation` | associate/disassociate the EIP and Application instance |

The `RunInstances` grant is restricted to `t3.small`, Seoul, the candidate subnet,
Amazon AMI resources, and the required EC2 resource types. The standalone data-volume
grant requires `ec2:Encrypted=true` and candidate AZ `ap-northeast-2c`. Destructive
rollback/update actions use the CloudFormation stack-name tag where the API supports it.

The template's `ImageIdParameter` is
`AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>` with the public AL2023 path, so the
execution role has `ssm:GetParameters` only on that exact public parameter ARN.
CloudFormation documents that it retrieves current SSM values for SSM parameter types
and that such references require `GetParameters`:
[CloudFormation SSM parameter types](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/cloudformation-supplied-parameter-types.html),
[SSM reference permissions](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/dynamic-references-ssm.html#dynamic-references-ssm-permissions).

`Resource: "*"` appears only for EC2 Describe handler calls. No `ec2:*` or `iam:*`
action is present.

The proposal intentionally contains **no** `scheduler:*` actions and no permission to
create `SchedulerRole`. With `EnableSchedulerParameter=false`, CloudFormation evaluates
those conditioned resources out. Setting it to `true` fails closed and requires a new
reviewed scheduler policy.

The execution policy is currently bound to the IMDS candidates
`vpc-05311e6f3180243c0`, `subnet-06563af7aa7d98464`, and `ap-northeast-2c`. It must not
be installed until section 12 confirms them. If any value differs, regenerate those
resource scopes first.

## 9. PassRole requirements

Two distinct pass operations exist:

1. `ApplicationDeployer` may pass only
   `arn:aws:iam::115019372648:role/ApplicationStackExecutionRole`, with
   `iam:PassedToService=cloudformation.amazonaws.com`.
2. `ApplicationStackExecutionRole` may pass only the generated
   `video-analyzer-ec2-stack-ApplicationInstanceRole-*` role. This exact generated-name
   pattern is also the only role on which it can create or manage inline policies.

The second PassRole statement intentionally has no `iam:PassedToService` condition.
`AWS::IAM::InstanceProfile` creation calls `AddRoleToInstanceProfile`, whose documented
dependent permission is `iam:PassRole`; that request does not itself carry a service
parameter. Safety comes from the exact role-name pattern and the role's EC2-only trust.
The later EC2 launch cannot use a different role.

The execution role does not pass itself. The deployer does not pass
`ApplicationInstanceRole` directly.

## 10. ApplicationInstanceRole remains runtime-only

**Yes.** No template change was made.

Confirmed runtime targets:

- Kinesis: `arn:aws:kinesis:ap-northeast-2:115019372648:stream/FrameStream`;
- DynamoDB table: `arn:aws:dynamodb:ap-northeast-2:115019372648:table/EnrichedFrame`;
- DynamoDB GSI:
  `...:table/EnrichedFrame/index/processed_year_month-processed_timestamp-index`;
- frames objects:
  `arn:aws:s3:::aws-face-detection-frames-115019372648-apne2/frames/*`;
- durable references:
  `arn:aws:s3:::aws-face-detection-frames-115019372648-apne2/locker-references/*`;
- Lambda invoke:
  `arn:aws:lambda:ap-northeast-2:115019372648:function:facecompare`;
- Lambda configuration:
  `arn:aws:lambda:ap-northeast-2:115019372648:function:imageprocessor`;
- data-stack resource lookup:
  `arn:aws:cloudformation:ap-northeast-2:115019372648:stack/video-analyzer-stack/*`;
- auth read: `/video-analyzer/webui/*`;
- artifact reads: only the configured `apps/webui/*`, `apps/kpi/*`, and
  `apps/application/*` prefixes.

The role does not contain CloudFormation create/update/delete, `ec2:RunInstances`,
`ec2:CreateVolume`, IAM role creation/PassRole, SSM writes, Lambda creation/deletion, or
broad data-bucket/DynamoDB permissions.

The template's API-key lookup is region-scoped to API Gateway `/apikeys/*`, because the
key's physical ID is resolved at runtime from the existing data stack. It never puts the
key value into CloudFormation or SSM parameters.

## 11. Generated policy files

- `docs/iam/application-deployer-policy.json`
- `docs/iam/application-cfn-execution-policy.json`
- `docs/iam/application-cfn-execution-trust.json`
- `docs/iam/webui-auth-bootstrap-policy.json`

All four pass local `python -m json.tool` parsing. Static checks found no wildcard
service actions and no forbidden Application runtime deployment action. AWS Access
Analyzer `ValidatePolicy` was attempted read-only but denied for the current caller, so
an administrator should run that validator before installation.

These are proposals, not attached policies. The execution policy is initial-CREATE and
rollback focused. Future stack update or deletion should receive a separate review; the
deployer policy deliberately has neither `UpdateStack` nor `DeleteStack`.

## 12. VPC/subnet verification plan

Candidates from the current development host's IMDS:

- VPC: `vpc-05311e6f3180243c0`
- subnet: `subnet-06563af7aa7d98464`
- candidate AZ: `ap-northeast-2c`

Before an administrator installs the candidate-bound execution policy or a deployer
creates a change set:

1. `DescribeVpcs`: confirm the VPC exists, is `available`, and its CIDR is expected.
2. `DescribeSubnets`: confirm the subnet exists, is `available`, belongs to that VPC,
   is in the expected AZ, and has sufficient free IPv4 addresses.
3. `DescribeRouteTables`: identify the explicit or main route table applying to the
   subnet and verify an active `0.0.0.0/0` route to an internet gateway.
4. `DescribeInternetGateways`: confirm that gateway is attached to the candidate VPC.
5. `DescribeNetworkAcls`: ensure outbound HTTPS and return traffic are not blocked.
6. Confirm the template still sets `AssociatePublicIpAddress: true`; subnet-level
   `MapPublicIpOnLaunch` is useful evidence but the template explicitly requests a
   public address.
7. `DescribeAddresses`: count current EIP allocations.
8. `GetServiceQuota(ec2, L-0263D0A3)`: confirm at least one EIP remains.
9. Re-resolve the AL2023 public SSM parameter and confirm the AMI architecture is x86_64.
10. Confirm `config/ec2-params.json` still uses scheduler `false`, the verified data
    bucket, and the confirmed VPC/subnet.

An EIP alone does not make a private subnet public; AWS requires the instance to be in a
public subnet with internet-gateway routing. See
[Associate an EIP with an instance](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/working-with-eips.html).

## 13. Secret-handling procedure

Use a short-lived session for the auth-bootstrap principal in an interactive TTY:

```bash
AWS_PROFILE=<auth-bootstrap-profile> AWS_REGION=ap-northeast-2 pynt setwebuiauth
```

The command takes no password, hash, or session secret in command-line arguments or
environment variables:

- username: ordinary interactive `input()`;
- password: entered twice through `getpass`, so it is not echoed;
- password hash: derived locally with a random salt and PBKDF2-SHA256;
- session secret: generated locally with `secrets.token_urlsafe(48)`;
- output: parameter names and types only.

Do not pipe values, use a heredoc, place them in shell history, enable shell tracing,
capture the terminal session, store them in CloudFormation parameters, or add them to
git/report files. Run from a trusted terminal, close the auth-bootstrap session when the
task succeeds, and do not verify by printing decrypted values. The helper's successful
three `Put` messages are sufficient write confirmation.

The username is stored as `String`; the hash and session secret are `SecureString`.
No plaintext password is retained by the helper.

## 14. Exact one-time admin setup

The administrator should perform these actions using an existing IAM administration
path, not `AwsFaceDetectionKioskRole`:

1. Complete section 12 with read-only EC2 and Service Quotas APIs.
2. If VPC, subnet, or AZ differs, update/regenerate
   `application-cfn-execution-policy.json` before creating any role.
3. Run AWS IAM Access Analyzer `ValidatePolicy` on the three identity policy files and
   review all findings. Validate the trust document as an assume-role policy.
4. Create `ApplicationStackExecutionRole` with
   `application-cfn-execution-trust.json`, and attach only the permissions in
   `application-cfn-execution-policy.json`.
5. Create or authorize a human/SSO-backed `ApplicationDeployer` role and attach
   `application-deployer-policy.json`. Its trust must name the organization's exact
   human/SSO administrator identity; no open account or service trust is proposed here.
6. Create or authorize a short-lived auth-bootstrap role/permission set and attach
   only `webui-auth-bootstrap-policy.json`.
7. Do not attach any of these policies to `ApplicationInstanceRole`; CloudFormation
   creates that role from the reviewed runtime-only template.
8. Do not grant the deployer direct assumption of the execution role. It receives only
   `iam:PassRole` to CloudFormation.
9. Keep `EnableSchedulerParameter=false`. Enabling scheduler requires a separate policy
   and change-set review.

Equivalent administrator CLI actions for the one fixed execution role are shown below.
They are instructions only and were not run:

```bash
aws iam create-role \
  --role-name ApplicationStackExecutionRole \
  --assume-role-policy-document file://docs/iam/application-cfn-execution-trust.json

aws iam put-role-policy \
  --role-name ApplicationStackExecutionRole \
  --policy-name ApplicationStackExecutionPolicy \
  --policy-document file://docs/iam/application-cfn-execution-policy.json

# Use existing organization-controlled human/SSO roles, or first create them with
# exact organization trust policies. Do not invent/open their trust relationships.
aws iam put-role-policy \
  --role-name <APPLICATION_DEPLOYER_HUMAN_OR_SSO_ROLE> \
  --policy-name ApplicationDeployerPolicy \
  --policy-document file://docs/iam/application-deployer-policy.json

aws iam put-role-policy \
  --role-name <AUTH_BOOTSTRAP_HUMAN_OR_SSO_ROLE> \
  --policy-name WebUiAuthBootstrapPolicy \
  --policy-document file://docs/iam/webui-auth-bootstrap-policy.json
```

Before these commands, replace no resource ID blindly: section 12 must confirm the
candidate VPC/subnet/AZ embedded in the execution policy. The two placeholder human role
names must be selected by the administrator and backed by the organization's exact SSO
or human trust configuration.

Security note: the execution role necessarily has `iam:PutRolePolicy` on the one
CloudFormation-generated runtime-role name pattern because the current template embeds
runtime inline policies. The change-set review is therefore a real security boundary,
not a formality. For a stronger long-term control, add an administrator-owned permissions
boundary to `ApplicationInstanceRole` in a separate reviewed template change. That is
not required or applied by this analysis task.

## 15. Exact next deployment commands after IAM setup

Commands below are instructions only; they were not run in this task.

First seed auth interactively under the auth-bootstrap principal:

```bash
AWS_PROFILE=<auth-bootstrap-profile> AWS_REGION=ap-northeast-2 pynt setwebuiauth
```

Then use the dedicated deployer session:

```bash
export AWS_PROFILE=<application-deployer-profile>
export AWS_REGION=ap-northeast-2
export AWS_DEFAULT_REGION=ap-northeast-2

aws sts get-caller-identity

# Re-run the read-only VPC/subnet/route/IGW/NACL/EIP/quota checks in section 12.
# Stop if they do not confirm the candidate-bound execution policy.

jq -e '.EnableSchedulerParameter == "false"' config/ec2-params.json
pynt publishapps

aws cloudformation validate-template \
  --template-body file://aws-infra/aws-infra-ec2-cfn.yaml \
  --region ap-northeast-2

jq 'to_entries | map({ParameterKey:.key,ParameterValue:(.value|tostring)})' \
  config/ec2-params.json > /tmp/application-ec2-parameters.json

aws cloudformation create-change-set \
  --stack-name video-analyzer-ec2-stack \
  --change-set-name application-greenfield-create-20260811-01 \
  --change-set-type CREATE \
  --template-body file://aws-infra/aws-infra-ec2-cfn.yaml \
  --parameters file:///tmp/application-ec2-parameters.json \
  --capabilities CAPABILITY_IAM \
  --role-arn arn:aws:iam::115019372648:role/ApplicationStackExecutionRole \
  --region ap-northeast-2

aws cloudformation wait change-set-create-complete \
  --stack-name video-analyzer-ec2-stack \
  --change-set-name application-greenfield-create-20260811-01 \
  --region ap-northeast-2

aws cloudformation describe-change-set \
  --stack-name video-analyzer-ec2-stack \
  --change-set-name application-greenfield-create-20260811-01 \
  --include-property-values \
  --region ap-northeast-2
```

Review every change and re-count exactly one EC2 instance, one EIP, one data volume,
zero legacy WebUi/Kpi resources, and zero scheduler resources. Only then:

```bash
aws cloudformation execute-change-set \
  --stack-name video-analyzer-ec2-stack \
  --change-set-name application-greenfield-create-20260811-01 \
  --region ap-northeast-2

aws cloudformation wait stack-create-complete \
  --stack-name video-analyzer-ec2-stack \
  --region ap-northeast-2

aws cloudformation describe-stacks \
  --stack-name video-analyzer-ec2-stack \
  --region ap-northeast-2
```

Do not substitute `pynt createec2stack`; it bypasses the reviewed CREATE change-set flow
and does not supply the execution-role ARN.

## 16. AWS IAM mutations

`NONE`

No role, policy, trust policy, attachment, PassRole operation, or permission change was
made.

## 17. CloudFormation mutations

`NONE`

No change set, stack, or Application resource was created, updated, executed, or
deleted.

## 18. Git commit

`NONE`
