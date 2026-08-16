# Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# Licensed under the Amazon Software License (the "License"). You may not use this file except in compliance with the License. A copy of the License is located at
#     http://aws.amazon.com/asl/
# or in the "license" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and limitations under the License.
import os
import sys
import shutil
import zipfile
import tarfile
import time
from pynt import task
import boto3
import botocore
from botocore.exceptions import ClientError
import json
from subprocess import call
import http.server
import socketserver

def write_dir_to_zip(src, zf):
    '''Write a directory tree to an open ZipFile object.'''
    abs_src = os.path.abspath(src)
    for dirname, subdirs, files in os.walk(src):
        for filename in files:
            absname = os.path.abspath(os.path.join(dirname, filename))
            arcname = absname[len(abs_src) + 1:]
            print('zipping %s as %s' % (os.path.join(dirname, filename),
                                        arcname))
            zf.write(absname, arcname)

def read_json(jsonf_path):
    '''Read a JSON file into a dict.'''
    with open(jsonf_path, 'r') as jsonf:
        json_text = jsonf.read()
        return json.loads(json_text)

_YUNET_MODEL_REL = os.path.join(
    "web-ui", "backend", "models", "face_detection_yunet_2023mar.onnx"
)
_YUNET_SHA256_REL = _YUNET_MODEL_REL + ".sha256"


def _require_yunet_model():
    """Fail packaging when the official YuNet ONNX is missing or tampered."""
    import hashlib

    model_path = _YUNET_MODEL_REL
    checksum_path = _YUNET_SHA256_REL
    if not os.path.isfile(model_path):
        raise SystemExit(
            "YuNet model missing: %s (run python3 scripts/fetch_yunet_model.py)"
            % model_path
        )
    if not os.path.isfile(checksum_path):
        raise SystemExit("YuNet checksum file missing: %s" % checksum_path)
    expected = open(checksum_path, "r", encoding="utf-8").read().strip().split()[0]
    digest = hashlib.sha256()
    with open(model_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual.lower() != expected.lower():
        raise SystemExit(
            "YuNet checksum mismatch expected=%s actual=%s" % (expected, actual)
        )
    print("YuNet model ok sha256=%s" % actual)


def _exclude_dev_files(tarinfo):
    '''tarfile filter: never package local dev secrets/venvs/caches into app artifacts.

    Without this, a developer's web-ui/backend/.env (login secrets) or .venv could be
    tarred by publishapps and uploaded to S3 + extracted onto the instance.'''
    parts = tarinfo.name.split('/')
    base = parts[-1]
    # Exclude ALL env files (.env, .env.example, .env.local, ...): deploy artifacts must
    # never carry env files -- the instance is configured from /etc/webui.env (systemd).
    if base.startswith('.env') or base.endswith('.pyc') or '.venv' in parts or '__pycache__' in parts:
        return None
    return tarinfo

def check_bucket_exists(bucketname):
    s3 = boto3.resource('s3')
    bucket = s3.Bucket(bucketname)
    exists = True
    try:
        s3.meta.client.head_bucket(Bucket=bucketname)
    except botocore.exceptions.ClientError as e:
        # If a client error is thrown, then check that it was a 404 error.
        # If it was a 404 error, then the bucket does not exist.
        error_code = int(e.response['Error']['Code'])
        if error_code == 404:
            exists = False
    return exists

@task()
def clean():
    '''Clean build directory.'''
    print('Cleaning build directory...')

    if os.path.exists('build'):
        shutil.rmtree('build')

    os.mkdir('build')

@task()
def packagelambda(* functions):
    '''No-op. Kiosk-only data stack has no Lambda functions.'''
    print("Kiosk-only: Lambda packaging removed.")
    return


@task()
def updatelambda(*functions):
    '''No-op. Kiosk-only data stack has no Lambda functions.'''
    print("Kiosk-only: Lambda update removed.")
    return

@task()
def setlogretention(*functions, **kwargs):
    '''Set CloudWatch Logs retention (days) on each Lambda's log group.

    Idempotent and safe whether or not the log group already exists:
      - creates the log group if missing (ignores "already exists"),
      - then sets the retention policy.
    This avoids the CloudFormation "log group already exists" failure that
    occurs when declaring AWS::Logs::LogGroup resources for functions whose
    groups were already auto-created. Pass days via kwarg, default 30.
    Usage: pynt setlogretention            (all functions, 30 days)
           pynt setlogretention
    '''
    days = int(kwargs.get("days", 30))

    if(len(functions) == 0):
        functions = ("framefetcher", "imageprocessor", "facecompare")

    logs_client = boto3.client("logs")

    for function in functions:
        log_group_name = "/aws/lambda/%s" % function
        try:
            logs_client.create_log_group(logGroupName=log_group_name)
            print("Created log group '%s'." % log_group_name)
        except logs_client.exceptions.ResourceAlreadyExistsException:
            print("Log group '%s' already exists." % log_group_name)

        logs_client.put_retention_policy(
            logGroupName=log_group_name,
            retentionInDays=days
        )
        print("Set retention of '%s' to %d days." % (log_group_name, days))

    return

@task()
def deploylambda(* functions, **kwargs):
    '''No-op. Kiosk-only data stack has no Lambda artifacts.'''
    print("Kiosk-only: Lambda S3 upload removed.")
    return


@task()
def createstack(**kwargs):
    '''Create the Amazon Rekognition Video Analyzer stack using CloudFormation.'''

    cfn_path = kwargs.get("cfn_path", "aws-infra/aws-infra-cfn.yaml") 
    global_params_path = kwargs.get("global_params_path", "config/global-params.json") 
    cfn_params_path = kwargs.get("cfn_params_path", "config/cfn-params.json")

    global_params_dict = read_json(global_params_path)
    stack_name = global_params_dict["StackName"]

    cfn_params_dict = read_json(cfn_params_path)
    cfn_params = []
    for key, value in cfn_params_dict.items():
        cfn_params.append({
            'ParameterKey' : key,
            'ParameterValue' : value
            })

    cfn_file = open(cfn_path, 'r')
    cfn_template = cfn_file.read(51200) #Maximum size of a cfn template

    cfn_client = boto3.client('cloudformation')

    print("Attempting to CREATE '%s' stack using CloudFormation." % (stack_name))
    start_t = time.time()
    response = cfn_client.create_stack(
        StackName=stack_name,
        TemplateBody=cfn_template,
        Parameters=cfn_params,
        Capabilities=[
        	'CAPABILITY_NAMED_IAM',
        ],
    )

    print("Waiting until '%s' stack status is CREATE_COMPLETE" % stack_name)
    cfn_stack_delete_waiter = cfn_client.get_waiter('stack_create_complete')
    cfn_stack_delete_waiter.wait(StackName=stack_name)

    print("Stack CREATED in approximately %d secs." % int(time.time() - start_t))

@task()
def updatestack(**kwargs):
    '''Update the Amazon Rekognition Video Analyzer CloudFormation stack.'''
    cfn_path = kwargs.get("cfn_path", "aws-infra/aws-infra-cfn.yaml") 
    global_params_path = kwargs.get("global_params_path", "config/global-params.json") 
    cfn_params_path = kwargs.get("cfn_params_path", "config/cfn-params.json")

    global_params_dict = read_json(global_params_path)
    stack_name = global_params_dict["StackName"]

    cfn_params_dict = read_json(cfn_params_path)
    cfn_params = []
    for key, value in cfn_params_dict.items():
        cfn_params.append({
            'ParameterKey' : key,
            'ParameterValue' : value
            })

    cfn_file = open(cfn_path, 'r')
    cfn_template = cfn_file.read(51200) #Maximum size of a cfn template

    cfn_client = boto3.client('cloudformation')

    print("Attempting to UPDATE '%s' stack using CloudFormation." % (stack_name))
    try:
        start_t = time.time()
        response = cfn_client.update_stack(
            StackName=stack_name,
            TemplateBody=cfn_template,
            Parameters=cfn_params,
            Capabilities=[
                'CAPABILITY_NAMED_IAM',
            ],
        )

        print("Waiting until '%s' stack status is UPDATE_COMPLETE" % stack_name)
        cfn_stack_update_waiter = cfn_client.get_waiter('stack_update_complete')
        cfn_stack_update_waiter.wait(StackName=stack_name)

        print("Stack UPDATED in approximately %d secs." % int(time.time() - start_t))
    except ClientError as e:
        print("EXCEPTION: " + e.response["Error"]["Message"])


@task()
def stackstatus(global_params_path="config/global-params.json"):
    '''Check the status of the Amazon Rekognition Video Analyzer CloudFormation stack.'''
    global_params_dict = read_json(global_params_path)
    stack_name = global_params_dict["StackName"]

    cfn_client = boto3.client('cloudformation')

    try:
        response = cfn_client.describe_stacks(
            StackName=stack_name
        )

        if(response["Stacks"][0]):
            print("Stack '%s' has the status '%s'" % (stack_name, response["Stacks"][0]["StackStatus"]))
    
    except ClientError as e:
        print("EXCEPTION: " + e.response["Error"]["Message"])


@task()
def deletestack(** kwargs):
    '''Delete Amazon Rekognition Video Analyzer infrastructure using CloudFormation.'''

    cfn_path = kwargs.get("cfn_path", "aws-infra/aws-infra-cfn.yaml") 
    global_params_path = kwargs.get("global_params_path", "config/global-params.json") 
    cfn_params_path = kwargs.get("cfn_params_path", "config/cfn-params.json")

    global_params_dict = read_json(global_params_path)
    cfn_params_dict = read_json(cfn_params_path)

    stack_name = global_params_dict["StackName"]
    cfn_client = boto3.client('cloudformation')

    print("Attempting to DELETE '%s' stack using CloudFormation." % stack_name)
    start_t = time.time()
    response = cfn_client.delete_stack(
        StackName=stack_name
    )

    print("Waiting until '%s' stack status is DELETE_COMPLETE" % stack_name)
    cfn_stack_delete_waiter = cfn_client.get_waiter('stack_delete_complete')
    cfn_stack_delete_waiter.wait(StackName=stack_name)
    print("Stack DELETED in approximately %d secs." % int(time.time() - start_t))


@task()
def webui(webdir="web-ui/", global_params_path="config/global-params.json", cfn_params_path="config/cfn-params.json"):
    '''Build the Amazon Rekognition Video Analyzer Web UI.'''

    # Clean web-ui build directory
    if not os.path.exists('build'):
        os.mkdir('build')

    web_build_dir = 'build/%s' % webdir

    if os.path.exists(web_build_dir):
        shutil.rmtree(web_build_dir)

    # Copy web-ui source
    print("Copying Web UI source from '%s' to build directory." % webdir)
    shutil.copytree(webdir, web_build_dir)
    print("Kiosk-only: no API Gateway key is injected into the UI.")



@task()
def webuiserver(webdir="web-ui/",port=8080):
    '''Start a local lightweight HTTP server to serve the Web UI.'''
    web_build_dir = 'build/%s' % webdir

    os.chdir(web_build_dir)
    
    Handler = http.server.SimpleHTTPRequestHandler

    httpd = socketserver.TCPServer(("0.0.0.0", port), Handler)

    print("Starting local Web UI Server in directory '%s' on port %s" % (web_build_dir, port))
    
    httpd.serve_forever()
    
    return

@task()
def videocaptureip(videouri, capturerate="30", clientdir="client"):
    '''Run the IP camera video capture client using parameters video URI and frame capture rate.'''
    os.chdir(clientdir)
    
    call([sys.executable, "video_cap_ipcam.py", videouri, capturerate])

    os.chdir("..")

    return

@task()
def videocapture(capturerate="60", maxseconds="", clientdir="client"):
    '''Run the video capture client with built-in camera.

    Default capture rate is 1 every 60 frames (cost-friendly default for demos).
    Recommended: videocapture[60] for dev/demo, videocapture[90] for low-cost tests,
    videocapture[20] only when high-speed capture is needed.
    Optional maxseconds auto-stops the client after N seconds, e.g. videocapture[60,300].'''
    os.chdir(clientdir)

    # Pass maxseconds through only when supplied so the no-arg / single-arg
    # behaviour (run until 'q' / camera EOF) is preserved.
    cmd = [sys.executable, "video_cap.py", capturerate]
    if maxseconds:
        cmd.append(maxseconds)
    call(cmd)

    os.chdir("..")

    return

@task()
def deletedata(global_params_path="config/global-params.json", cfn_params_path="config/cfn-params.json", image_processor_params_path="config/imageprocessor-params.json"):
    '''DELETE ALL collected frames and metadata in Amazon S3 and Amazon DynamoDB. Use with caution!'''
    
    cfn_params_dict = read_json(cfn_params_path)
    img_processor_params_dict = read_json(image_processor_params_path)

    frame_s3_bucket_name = cfn_params_dict["FrameS3BucketNameParameter"]
    frame_ddb_table_name = img_processor_params_dict["ddb_table"]

    proceed = input("This command will DELETE ALL DATA in S3 bucket '%s' and DynamoDB table '%s'.\nDo you wish to continue? [Y/N] " \
        % (frame_s3_bucket_name, frame_ddb_table_name))

    if(proceed.lower() != 'y'):
        print("Aborting deletion.")
        return


    print("Attempting to DELETE ALL OBJECTS in '%s' S3 bucket." % frame_s3_bucket_name)
    
    s3 = boto3.resource('s3')
    s3.Bucket(frame_s3_bucket_name).objects.delete()

    print("Attempting to DELETE ALL ITEMS in '%s' DynamoDB table." % frame_ddb_table_name)
    dynamodb = boto3.client('dynamodb')
    ddb_table = boto3.resource('dynamodb').Table(frame_ddb_table_name)

    last_eval_key = None
    keep_scanning = True
    batch_count = 0
    while keep_scanning:
        batch_count += 1

        if(keep_scanning and last_eval_key):
            response = dynamodb.scan(
                TableName=frame_ddb_table_name,
                Select='SPECIFIC_ATTRIBUTES',
                AttributesToGet=[
                    'frame_id',
                ],
                ExclusiveStartKey=last_eval_key
            )
        else:
            response = dynamodb.scan(
                TableName=frame_ddb_table_name,
                Select='SPECIFIC_ATTRIBUTES',
                AttributesToGet=[
                    'frame_id',
                ]
            )

        last_eval_key = response.get('LastEvaluatedKey', None)
        keep_scanning = True if last_eval_key else False

        with ddb_table.batch_writer() as batch:
            for item in response["Items"]:
                print("Deleting Item with 'frame_id': %s" % item['frame_id']['S'])
                batch.delete_item(
                    Key={
                        'frame_id': item['frame_id']['S']
                    }
                )
    print("Deleted %s batches of items from DynamoDB." % batch_count)

    return

# ---------------------------------------------------------------------------
# EC2 Application stack (greenfield: ONE ApplicationInstance).
#
# These tasks manage a SEPARATE CloudFormation stack (video-analyzer-ec2-stack)
# from the data pipeline stack (video-analyzer-stack). The EC2 template creates
# exactly one host: Nginx + FastAPI + KPI + SQLite EBS — no WebUi/Kpi dual hosts.
# createec2stack / updateec2stack reuse createstack / updatestack with the EC2
# template and config paths. deleteec2stack does NOT touch the data stack or
# frames bucket.
# ---------------------------------------------------------------------------

@task()
def createec2stack():
    '''Create the greenfield Application-only EC2 stack (ONE instance).

    Prerequisites:
      - video-analyzer-stack CREATE_COMPLETE (data plane)
      - config/ec2-global-params.json + config/ec2-params.json
        (copy from config/*.example.json; fill VPC/subnet/FrameS3Bucket/artifact bucket)
      - pynt setwebuiauth
      - pynt publishapps

    Creates: ApplicationInstance + EIP + data volume (+ optional scheduler).
    Does NOT create WebUiInstance or KpiInstance.

    After verify, stop to save compute cost (EIP + EBS still bill):
        aws ec2 stop-instances --instance-ids <ApplicationInstanceId>
    (also printed as stack output StopInstancesCommand).'''
    print("Creating GREENFIELD Application-only stack (expected EC2 count = 1).")
    createstack(
        cfn_path="aws-infra/aws-infra-ec2-cfn.yaml",
        global_params_path="config/ec2-global-params.json",
        cfn_params_path="config/ec2-params.json",
    )

@task()
def updateec2stack():
    '''Update the Application-only EC2 stack.

    Example: lock inbound to your IP via AllowedIngressCidrParameter in
    config/ec2-params.json, then run this task.'''
    updatestack(
        cfn_path="aws-infra/aws-infra-ec2-cfn.yaml",
        global_params_path="config/ec2-global-params.json",
        cfn_params_path="config/ec2-params.json",
    )

@task()
def deleteec2stack(global_params_path="config/ec2-global-params.json"):
    '''Delete the EC2 deployment stack ONLY.

    Does NOT touch the data pipeline stack, the frames S3 bucket, or any collected
    data. Safe to run repeatedly.'''
    stack_name = read_json(global_params_path)["StackName"]

    cfn_client = boto3.client('cloudformation')

    print("Attempting to DELETE '%s' stack using CloudFormation." % stack_name)
    start_t = time.time()
    cfn_client.delete_stack(StackName=stack_name)

    print("Waiting until '%s' stack status is DELETE_COMPLETE" % stack_name)
    cfn_client.get_waiter('stack_delete_complete').wait(StackName=stack_name)
    print("Stack DELETED in approximately %d secs." % int(time.time() - start_t))

@task()
def ec2ip(global_params_path="config/ec2-global-params.json"):
    '''Print public IPs of EC2 stack instances (greenfield: ApplicationInstance only).

    Uses stack EIP association when present (stable across stop/start).'''
    stack_name = read_json(global_params_path)["StackName"]

    cfn_client = boto3.client('cloudformation')
    ec2_client = boto3.client('ec2')

    try:
        resources = cfn_client.describe_stack_resources(StackName=stack_name)["StackResources"]
    except ClientError as e:
        print("Could not describe stack '%s': %s" % (stack_name, e))
        return

    instance_ids = [r["PhysicalResourceId"] for r in resources
                    if r["ResourceType"] == "AWS::EC2::Instance"]

    if not instance_ids:
        print("No EC2 instances found in stack '%s'." % stack_name)
        return

    print("EC2 instance count in stack: %d (greenfield target = 1)" % len(instance_ids))
    for reservation in ec2_client.describe_instances(InstanceIds=instance_ids)["Reservations"]:
        for inst in reservation["Instances"]:
            name = next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"),
                        inst["InstanceId"])
            print("%-26s %-20s state=%-10s public_ip=%s" % (
                name,
                inst["InstanceId"],
                inst["State"]["Name"],
                inst.get("PublicIpAddress", "(none - not running)")))

@task()
def ec2vpcinfo():
    '''Print the default VPC id and its subnets to help fill config/ec2-params.json.

    Pick the default VPC id for "VpcIdParameter" and a PUBLIC subnet id for
    "SubnetIdParameter". Requires ec2:DescribeVpcs/DescribeSubnets (admin role).
    The kiosk/dev instance role often lacks these APIs — use an admin principal.'''
    ec2_client = boto3.client('ec2')

    try:
        vpcs = ec2_client.describe_vpcs(
            Filters=[{"Name": "isDefault", "Values": ["true"]}]
        )["Vpcs"]
    except ClientError as e:
        print("DescribeVpcs failed: %s" % e)
        print("Fallback (reference only): read this host's network from IMDS if on EC2:")
        print("  curl -s -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \\")
        print("    http://169.254.169.254/latest/api/token")
        print("  # then mac → ENI → subnet/vpc via authorized principal")
        print("Do NOT bake IMDS values into the CFN template automatically.")
        return

    if not vpcs:
        print("No default VPC found in this region. Specify any VPC + public subnet manually.")
        return

    vpc_id = vpcs[0]["VpcId"]
    print("Default VPC: %s" % vpc_id)

    subnets = ec2_client.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    )["Subnets"]

    for s in subnets:
        visibility = "PUBLIC" if s.get("MapPublicIpOnLaunch") else "private"
        print("  subnet %-24s az=%-16s %-7s" % (
            s["SubnetId"], s["AvailabilityZone"], visibility))

    return

@task()
def publishapps(*apps, **kwargs):
    '''Package and upload Application stack artifacts to S3 for instance boot.

    Default (all three):
      webui:       apps/webui/web-ui.tgz (+ optional legacy bootstrap files)
      kpi:         apps/kpi/kpi-dashboard.tgz (+ optional standalone kpi unit)
      application: apps/application/* bootstrap, systemd units, nginx, TLS refresh

    ApplicationInstance bootstrap reuses web-ui.tgz and kpi-dashboard.tgz.

    Artifact bucket/prefix from config/ec2-params.json.

    DEPLOYMENT ORDER:
        pynt publishapps
        pynt createec2stack
        # https://<ApplicationElasticIp>/
        # https://<ApplicationElasticIp>/dashboard/
    '''
    ec2_params_path = kwargs.get("ec2_params_path", "config/ec2-params.json")
    ec2_params = read_json(ec2_params_path)
    bucket = ec2_params["AppArtifactS3BucketParameter"]
    prefix = ec2_params["AppArtifactS3KeyPrefixParameter"]

    if(len(apps) == 0):
        apps = ("webui", "kpi", "application")

    if not os.path.exists("build"):
        os.mkdir("build")

    s3_client = boto3.client("s3")

    for app in apps:
        if app == "webui":
            _require_yunet_model()
            tar_path = "build/web-ui.tgz"
            print("Packaging web-ui/ -> %s" % tar_path)
            with tarfile.open(tar_path, "w:gz") as tar:
                tar.add("web-ui", arcname=".", filter=_exclude_dev_files)

            uploads = [
                (tar_path, "%swebui/web-ui.tgz" % prefix),
                # Legacy standalone webui host artifacts (optional; not used by Application-only stack)
                ("aws-infra/userdata/webui-bootstrap.sh", "%swebui/bootstrap.sh" % prefix),
                ("aws-infra/userdata/webui-genconfig.sh", "%swebui/webui-genconfig.sh" % prefix),
                ("aws-infra/userdata/webui.service", "%swebui/webui.service" % prefix),
            ]
            for local_path, key in uploads:
                if not os.path.isfile(local_path) and not local_path.endswith(".tgz"):
                    print("Skip missing optional %s" % local_path)
                    continue
                print("Uploading %s -> s3://%s/%s" % (local_path, bucket, key))
                s3_client.upload_file(local_path, bucket, key)
        elif app == "kpi":
            tar_path = "build/kpi-dashboard.tgz"
            print("Packaging kpi-dashboard/ -> %s" % tar_path)
            with tarfile.open(tar_path, "w:gz") as tar:
                tar.add("kpi-dashboard", arcname=".", filter=_exclude_dev_files)

            uploads = [
                (tar_path, "%skpi/kpi-dashboard.tgz" % prefix),
                ("kpi-dashboard/deploy/kpi-bootstrap.sh", "%skpi/bootstrap.sh" % prefix),
                ("kpi-dashboard/deploy/kpi-dashboard.service", "%skpi/kpi-dashboard.service" % prefix),
            ]
            for local_path, key in uploads:
                print("Uploading %s -> s3://%s/%s" % (local_path, bucket, key))
                s3_client.upload_file(local_path, bucket, key)
        elif app == "application":
            print("Publishing ApplicationInstance artifacts (apps/application/*)")
            uploads = [
                ("aws-infra/userdata/application-bootstrap.sh",
                 "%sapplication/bootstrap.sh" % prefix),
                ("aws-infra/userdata/kiosk-fastapi.service",
                 "%sapplication/kiosk-fastapi.service" % prefix),
                ("aws-infra/userdata/kpi-dashboard-loopback.service",
                 "%sapplication/kpi-dashboard.service" % prefix),
                ("aws-infra/nginx/application.conf",
                 "%sapplication/nginx-application.conf" % prefix),
                ("aws-infra/userdata/application-tls-refresh.sh",
                 "%sapplication/application-tls-refresh.sh" % prefix),
                ("aws-infra/userdata/application-tls-refresh.service",
                 "%sapplication/application-tls-refresh.service" % prefix),
            ]
            for local_path, key in uploads:
                if not os.path.isfile(local_path):
                    print("ERROR: missing %s — cannot publish application artifacts." % local_path)
                    raise SystemExit(1)
                print("Uploading %s -> s3://%s/%s" % (local_path, bucket, key))
                s3_client.upload_file(local_path, bucket, key)
            print("Note: also needs apps/webui/web-ui.tgz and apps/kpi/kpi-dashboard.tgz "
                  "(included when running pynt publishapps with no args).")
        else:
            print("Unknown app '%s' (expected 'webui', 'kpi', or 'application'). Skipping." % app)

    return

@task()
def applicationip(global_params_path="config/ec2-global-params.json"):
    '''Print ApplicationInstance public IP / EIP (read-only).'''
    stack_name = read_json(global_params_path)["StackName"]
    cfn_client = boto3.client("cloudformation")
    ec2_client = boto3.client("ec2")

    try:
        outputs = {
            o["OutputKey"]: o["OutputValue"]
            for o in cfn_client.describe_stacks(StackName=stack_name)["Stacks"][0].get("Outputs", [])
        }
    except ClientError as e:
        print("Could not describe stack '%s': %s" % (stack_name, e))
        return

    app_id = outputs.get("ApplicationInstanceId")
    app_eip = outputs.get("ApplicationElasticIp")
    app_url = outputs.get("ApplicationUrl")
    dash_url = outputs.get("ApplicationDashboardUrl")

    if not app_id:
        print("No ApplicationInstanceId output on stack '%s'." % stack_name)
        return

    state = "(unknown)"
    public_ip = app_eip or "(none)"
    try:
        inst = ec2_client.describe_instances(InstanceIds=[app_id])["Reservations"][0]["Instances"][0]
        state = inst["State"]["Name"]
        if inst.get("PublicIpAddress"):
            public_ip = inst["PublicIpAddress"]
    except (ClientError, IndexError, KeyError) as e:
        print("Instance describe warning: %s" % e)

    print("ApplicationInstanceId=%s" % app_id)
    print("state=%s" % state)
    print("public_ip=%s" % public_ip)
    if app_eip:
        print("ApplicationElasticIp=%s" % app_eip)
    if app_url:
        print("ApplicationUrl=%s" % app_url)
    if dash_url:
        print("ApplicationDashboardUrl=%s" % dash_url)
    print("Note: demo path is https://<eip>/  — NOT /proxy/8080/")

@task()
def applicationstatus(global_params_path="config/ec2-global-params.json"):
    '''Summarize Application stack resources (read-only greenfield check).'''
    stack_name = read_json(global_params_path)["StackName"]
    cfn_client = boto3.client("cloudformation")
    want = {
        "ApplicationInstance",
        "ApplicationInstanceRole",
        "ApplicationInstanceProfile",
        "ApplicationSecurityGroup",
        "ApplicationDataVolume",
        "ApplicationDataVolumeAttachment",
        "ApplicationElasticIp",
        "ApplicationElasticIpAssociation",
    }
    try:
        resources = cfn_client.describe_stack_resources(StackName=stack_name)["StackResources"]
    except ClientError as e:
        print("Could not describe stack resources for '%s': %s" % (stack_name, e))
        return

    found = 0
    ec2_count = 0
    eip_count = 0
    volume_count = 0
    legacy_hits = []
    for r in sorted(resources, key=lambda x: x["LogicalResourceId"]):
        lid = r["LogicalResourceId"]
        rtype = r["ResourceType"]
        if rtype == "AWS::EC2::Instance":
            ec2_count += 1
        if rtype == "AWS::EC2::EIP":
            eip_count += 1
        if rtype == "AWS::EC2::Volume":
            volume_count += 1
        if lid in ("WebUiInstance", "KpiInstance", "WebUiElasticIp", "KpiElasticIp"):
            legacy_hits.append(lid)
        if lid in want or lid.startswith("Application") or lid.startswith("Schedule") or lid == "SchedulerRole":
            found += 1
            print("%-40s %-28s %s" % (
                lid, rtype.split("::")[-1], r["ResourceStatus"]))
    print("EC2_count=%d EIP_count=%d Volume_count=%d Application_related=%d" % (
        ec2_count, eip_count, volume_count, found))
    if legacy_hits:
        print("WARNING: unexpected legacy logical IDs still in stack: %s" % ", ".join(legacy_hits))
    else:
        print("legacy WebUi/Kpi resources present=false (greenfield OK)")

@task()
def setwebuiauth(**kwargs):
    '''Store Web UI login credentials in SSM Parameter Store (SecureString).

    Prompts for username + password, computes a PBKDF2-SHA256 hash (the plaintext
    password is NEVER stored or printed), generates a random session secret, and writes
    three SSM parameters under the prefix (default /video-analyzer/webui):
        <prefix>/auth-username   (String)
        <prefix>/password-hash   (SecureString)
        <prefix>/session-secret  (SecureString)
    The webui instance reads these at boot (webui-bootstrap.sh). Run this BEFORE
    'pynt createec2stack' / 'pynt updateec2stack', and re-run + reboot to rotate.

    AWS: writes 3 SSM parameters (standard tier = free). Needs ssm:PutParameter.
    Usage: pynt setwebuiauth   or   pynt "setwebuiauth[ssm_prefix=/my/prefix]"
    '''
    import getpass, hashlib, base64, secrets as _secrets

    prefix = kwargs.get("ssm_prefix", "/video-analyzer/webui")

    username = input("Web UI username: ").strip()
    if not username:
        print("Empty username. Aborting.")
        return
    pw1 = getpass.getpass("Web UI password: ")
    pw2 = getpass.getpass("Confirm password: ")
    if not pw1 or pw1 != pw2:
        print("Passwords empty or do not match. Aborting.")
        return

    iterations = 200000
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw1.encode("utf-8"), salt, iterations)
    pw_hash = "pbkdf2_sha256$%d$%s$%s" % (
        iterations, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())
    session_secret = _secrets.token_urlsafe(48)

    ssm = boto3.client("ssm")
    for name, value, ptype in [
        ("%s/auth-username" % prefix, username, "String"),
        ("%s/password-hash" % prefix, pw_hash, "SecureString"),
        ("%s/session-secret" % prefix, session_secret, "SecureString"),
    ]:
        ssm.put_parameter(Name=name, Value=value, Type=ptype, Overwrite=True)
        print("Put %s (%s)" % (name, ptype))

    print("Done. Stored under '%s'. Re-run bootstrap / reboot the webui instance to apply." % prefix)

