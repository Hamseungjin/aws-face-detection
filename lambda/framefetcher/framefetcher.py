# Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# Licensed under the Amazon Software License (the "License"). You may not use this file except in compliance with the License. A copy of the License is located at
#     http://aws.amazon.com/asl/
# or in the "license" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and limitations under the License.

from __future__ import print_function

import boto3
from botocore.client import Config
from boto3.dynamodb.conditions import Key, Attr
import datetime
import time
import json
import decimal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo



class DecimalEncoder(json.JSONEncoder):
    def default(self, o): # pylint: disable=E0202
        if isinstance(o, decimal.Decimal):
            if o % 1 > 0:
                return float(o)
            else:
                return int(o)
        return super(DecimalEncoder, self).default(o)

def load_config():

    with open('framefetcher-params.json', 'r') as conf_file:
        conf_json = conf_file.read()
        return json.loads(conf_json)

def respond(err, res=None):
    return {
        'statusCode': '400' if err else '200',
        'body': str(err) if err else json.dumps(res, cls=DecimalEncoder),
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': "*"
        },
    }


def fetch_frames(event, context):

    #Initialize clients
    dynamodb = boto3.resource('dynamodb')
    # Generate presigned URLs against the REGIONAL virtual-hosted S3 endpoint
    # (bucket.s3.<region>.amazonaws.com) using SigV4. Without this, boto3 may emit the
    # global "s3.amazonaws.com" endpoint, whose signature does NOT validate for a bucket
    # outside us-east-1 -> the browser <img> load fails with HTTP 403 SignatureDoesNotMatch.
    s3_client = boto3.client('s3', config=Config(signature_version='s3v4', s3={'addressing_style': 'virtual'}))
    
    #Load config
    config = load_config()

    ddb_table = dynamodb.Table(config['ddb_table'])
    ddb_gsi_name = config['ddb_gsi_name']
    fetch_horizon_hrs = float(config['fetch_horizon_hrs'])
    fetch_limit = config['fetch_limit']

    #Process "GET" request
    if event['httpMethod'] == "GET":
        now = datetime.datetime.now(ZoneInfo(config["timezone"]))
        year = now.strftime("%Y")
        mon = now.strftime("%m")

        ts_at_fetch_horizon = time.time() - (fetch_horizon_hrs * 60 * 60)

        query_start = time.perf_counter()
        ddb_resp = ddb_table.query(
            IndexName=ddb_gsi_name,

            KeyConditionExpression=Key('processed_year_month').eq(year + mon)
            & Key('processed_timestamp').gt(decimal.Decimal(ts_at_fetch_horizon)),
            Limit=fetch_limit,
            ScanIndexForward=False #Sort descendingly -- show most recent captured frames first.
        )
        query_ms = round((time.perf_counter() - query_start) * 1000.0, 1)

        presign_start = time.perf_counter()
        presigned_sample = None  # masked diagnostic sample of the first generated URL
        for item in ddb_resp["Items"]:

            s3_key = item["s3_key"]
            s3_bucket = item["s3_bucket"]
            # Note the following. 
            # (1) even if the url expires in days or weeks, the presigned 
            # url is usable only if the temporary IAM credentials that generated 
            # it haven't expired. These are the credentials assumed by this lambda function.
            # (2) Your bucket policy needs to allow "read" access to "authenticated AWS users"
            # (3) Ensure this Lambda function's role has S3FullAccess policy attached to it. 
            s3_presigned_url_expiry = config["s3_pre_signed_url_expiry"]

            s3_presigned_url = s3_client.generate_presigned_url(
                ClientMethod='get_object',
                Params={
                    'Bucket' : s3_bucket,
                    'Key' : s3_key
                },
                ExpiresIn=s3_presigned_url_expiry
            )

            item['s3_presigned_url'] = s3_presigned_url

            # Diagnostic only: capture host/region/key-tail of the FIRST URL so we can
            # confirm in CloudWatch which bucket/region the browser is being pointed at.
            # urlsplit().netloc drops the query string, so the SigV4 signature and
            # credentials are NEVER logged. Host looks like:
            #   <bucket>.s3.<region>.amazonaws.com
            if presigned_sample is None:
                netloc = urlsplit(s3_presigned_url).netloc
                region = None
                parts = netloc.split('.')
                # Pull the segment right after the literal "s3" label, if present.
                if 's3' in parts:
                    i = parts.index('s3')
                    if i + 1 < len(parts) and parts[i + 1] != 'amazonaws':
                        region = parts[i + 1]
                presigned_sample = {
                    "s3_host": netloc,
                    "s3_region_from_host": region,
                    "s3_key_tail": s3_key[-16:]
                }

        presigned_url_generation_ms = round((time.perf_counter() - presign_start) * 1000.0, 1)

        # Structured KPI log (CloudWatch). Do NOT log presigned URLs or full items
        # (they contain signed S3 access); only counts/timings.
        print(json.dumps({
            "component": "framefetcher",
            "event": "fetch_complete",
            "request_id": getattr(context, "aws_request_id", None),
            "framefetcher_dynamodb_query_ms": query_ms,
            "presigned_url_generation_ms": presigned_url_generation_ms,
            "returned_frame_count": len(ddb_resp["Items"]),
            "fetch_limit": fetch_limit,
            "presigned_sample": presigned_sample
        }, default=str))

        return respond(None, ddb_resp["Items"])

def handler(event, context):
    return fetch_frames(event, context)
    
