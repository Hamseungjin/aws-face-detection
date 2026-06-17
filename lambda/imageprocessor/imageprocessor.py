# Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# Licensed under the Amazon Software License (the "License"). You may not use this file except in compliance with the License. A copy of the License is located at
#     http://aws.amazon.com/asl/
# or in the "license" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and limitations under the License.

from __future__ import print_function
import base64
import datetime
import time
import os
import traceback
from decimal import Decimal
import uuid
import json
import pickle
import boto3
import pytz
from pytz import timezone
from copy import deepcopy
import log_util  # structured JSON-Lines logging (NOT the stdlib "logging" module)

# Set once when the execution environment is first initialized; flipped to False
# after the first invocation so we can report cold starts.
_COLD_START = True

def load_config():
    '''Load configuration from file.'''
    with open('imageprocessor-params.json', 'r') as conf_file:
        conf_json = conf_file.read()
        return json.loads(conf_json)

def _resolve_enable_detect_labels(config):
    '''Decide whether to call Rekognition DetectLabels (the per-frame cost driver).

    Precedence: ENABLE_DETECT_LABELS env var > config "enable_detect_labels" > True.
    Defaulting to True preserves the original behaviour; set the env var or config
    key to a falsey value to skip DetectLabels and avoid the per-image charge while
    still storing the frame in S3 + DynamoDB. Mirrors the env-var pattern used by
    log_util (ENABLE_S3_LOGGING).'''
    env = os.environ.get("ENABLE_DETECT_LABELS")
    if env is not None:
        return env.strip().lower() in ("true", "1", "yes")
    return bool(config.get("enable_detect_labels", True))

def convert_ts(ts, config):
    '''Converts a timestamp to the configured timezone. Returns a localized datetime object.'''
    #lambda_tz = timezone('US/Pacific')
    tz = timezone(config['timezone'])
    utc = pytz.utc
    
    utc_dt = utc.localize(datetime.datetime.utcfromtimestamp(ts))

    localized_dt = utc_dt.astimezone(tz)

    return localized_dt


def process_image(event, context):

    global _COLD_START
    cold_start = _COLD_START
    _COLD_START = False

    handler_start = time.perf_counter()
    request_id = getattr(context, "aws_request_id", None)

    # Per-invocation log buffer; flushed to S3 once at the end (if enabled).
    buffer = []

    def log(event_name, **fields):
        '''Local shortcut: tag every line with component + request_id and buffer it.'''
        log_util.log_event("imageprocessor", event_name, buffer=buffer,
                           request_id=request_id, **fields)

    #Initialize clients
    rekog_client = boto3.client('rekognition')
    sns_client = boto3.client('sns')
    s3_client = boto3.client('s3')
    dynamodb = boto3.resource('dynamodb')

    #Load config
    config = load_config()

    s3_bucket = config["s3_bucket"]
    s3_key_frames_root = config["s3_key_frames_root"]

    ddb_table = dynamodb.Table(config["ddb_table"])

    rekog_max_labels = config["rekog_max_labels"]
    rekog_min_conf = float(config["rekog_min_conf"])

    label_watch_list = config["label_watch_list"]
    label_watch_min_conf = float(config["label_watch_min_conf"])
    label_watch_phone_num = config.get("label_watch_phone_num", "")
    label_watch_sns_topic_arn = config.get("label_watch_sns_topic_arn", "")

    # Cost switch: when disabled, skip the per-frame Rekognition DetectLabels call
    # (still store the frame in S3 + DynamoDB). Defaults to enabled.
    enable_detect_labels = _resolve_enable_detect_labels(config)

    record_count = len(event['Records'])
    log("handler_start", record_count=record_count, cold_start=cold_start,
        s3_logging_enabled=log_util.s3_logging_enabled(),
        enable_detect_labels=enable_detect_labels)

    succeeded = 0

    #Iterate on frames fetched from Kinesis
    for record_index, record in enumerate(event['Records']):

        record_start = time.perf_counter()

        # Wrap each record so one bad record cannot abort the whole batch.
        try:
            kinesis_meta = record.get('kinesis', {})
            frame_package_b64 = kinesis_meta['data']
            payload_bytes = len(frame_package_b64)

            log("record_start", record_index=record_index,
                partition_key=kinesis_meta.get('partitionKey'),
                approx_arrival=kinesis_meta.get('approximateArrivalTimestamp'),
                payload_bytes=payload_bytes)

            #Decode the frame package (do NOT log raw image bytes / base64)
            try:
                frame_package = pickle.loads(base64.b64decode(frame_package_b64))
                img_bytes = frame_package["ImageBytes"]
                approx_capture_ts = frame_package["ApproximateCaptureTime"]
                frame_count = frame_package["FrameCount"]
                log("frame_decode_success", record_index=record_index,
                    frame_count=frame_count, image_bytes=len(img_bytes))
            except Exception as e:
                log("frame_decode_failed", record_index=record_index,
                    error=str(e), traceback=traceback.format_exc())
                continue

            now_ts = time.time()

            frame_id = str(uuid.uuid4())
            processed_timestamp = Decimal(now_ts)
            approx_capture_timestamp = Decimal(approx_capture_ts)

            now = convert_ts(now_ts, config)
            year = now.strftime("%Y")
            mon = now.strftime("%m")
            day = now.strftime("%d")
            hour = now.strftime("%H")

            # These two feed the DynamoDB item below. When DetectLabels is disabled
            # they stay at their cost-free defaults (no labels, no rotation hint).
            labels = []
            orientation_correction = 'ROTATE_0'

            if enable_detect_labels:
                #Rekognition: detect labels
                rekog_start = time.perf_counter()
                log("rekognition_detect_labels_start", record_index=record_index, frame_id=frame_id)
                try:
                    rekog_response = rekog_client.detect_labels(
                        Image={
                            'Bytes': img_bytes
                        },
                        MaxLabels=rekog_max_labels,
                        MinConfidence=rekog_min_conf
                    )
                except Exception as e:
                    #Log error and skip this frame; remaining records keep processing.
                    #You might want to add that frame to a dead-letter queue.
                    rekognition_ms = round((time.perf_counter() - rekog_start) * 1000.0, 1)
                    log("rekognition_detect_labels_failed", record_index=record_index,
                        frame_id=frame_id, rekognition_ms=rekognition_ms,
                        error=str(e), traceback=traceback.format_exc())
                    print(e)
                    continue

                rekognition_ms = round((time.perf_counter() - rekog_start) * 1000.0, 1)
                labels = rekog_response['Labels']
                orientation_correction = rekog_response.get('OrientationCorrection', 'ROTATE_0')
                #Summarize labels only (names + count) -- no image data.
                label_count = len(labels)
                top_labels = [l['Name'] for l in sorted(
                    labels, key=lambda x: x['Confidence'], reverse=True)[:5]]
                log("rekognition_detect_labels_success", record_index=record_index,
                    frame_id=frame_id, payload_bytes=payload_bytes, rekognition_ms=rekognition_ms,
                    label_count=label_count, top_labels=top_labels)

                #Iterate on rekognition labels. Enrich and prep them for storage in DynamoDB
                labels_on_watch_list = []
                for label in labels:

                    lbl = label['Name']
                    conf = label['Confidence']
                    label['OnWatchList'] = False

                    #Print labels and confidence to lambda console
                    print('{} .. conf %{:.2f}'.format(lbl, conf))

                    #Check label watch list and trigger action
                    if (lbl.upper() in (label.upper() for label in label_watch_list)
                        and conf >= label_watch_min_conf):

                        label['OnWatchList'] = True
                        labels_on_watch_list.append(deepcopy(label))

                    #Convert from float to decimal for DynamoDB
                    label['Confidence'] = Decimal(conf)

                    for instance in label['Instances']:
                        instance['BoundingBox']['Width'] = Decimal(instance['BoundingBox']['Width'])
                        instance['BoundingBox']['Height'] = Decimal(instance['BoundingBox']['Height'])
                        instance['BoundingBox']['Left'] = Decimal(instance['BoundingBox']['Left'])
                        instance['BoundingBox']['Top'] = Decimal(instance['BoundingBox']['Top'])
                        instance['Confidence'] = Decimal(instance['Confidence'])

                #Send out notification(s), if needed
                if len(labels_on_watch_list) > 0 \
                        and (label_watch_phone_num or label_watch_sns_topic_arn):

                    notification_txt = 'On {}...\n'.format(now.strftime('%x, %-I:%M %p %Z'))

                    for label in labels_on_watch_list:

                        notification_txt += '- "{}" was detected with {}% confidence.\n'.format(
                            label['Name'],
                            round(label['Confidence'], 2))

                    print(notification_txt)

                    #Alert delivery must never block frame persistence (S3/DynamoDB below).
                    try:
                        if label_watch_phone_num:
                            sns_client.publish(PhoneNumber=label_watch_phone_num, Message=notification_txt)

                        if label_watch_sns_topic_arn:
                            resp = sns_client.publish(
                                TopicArn=label_watch_sns_topic_arn,
                                Message=json.dumps(
                                    {
                                        "message": notification_txt,
                                        "labels": labels_on_watch_list
                                    }
                                )
                            )

                            if resp.get("MessageId", ""):
                                print("Successfully published alert message to SNS.")
                        log("sns_alert_published", record_index=record_index, frame_id=frame_id,
                            watch_label_count=len(labels_on_watch_list))
                    except Exception as e:
                        #Log and continue; a failed alert should not drop the frame.
                        print("Alert/SNS publish failed (non-fatal): {}".format(e))
                        log("sns_alert_failed", record_index=record_index, frame_id=frame_id,
                            error=str(e))
            else:
                # Cost-saving path: no Rekognition call. The frame is still stored
                # (S3 + DynamoDB) so facecompare and the Web UI keep working; labels
                # are simply empty. watch-list/SNS alerts are skipped (no labels).
                log("rekognition_detect_labels_skipped", record_index=record_index,
                    frame_id=frame_id, payload_bytes=payload_bytes, reason="enable_detect_labels=false")

            #Store frame image in S3 (log the key only, never the image body)
            s3_key = (s3_key_frames_root + '{}/{}/{}/{}/{}.jpg').format(year, mon, day, hour, frame_id)

            s3_start = time.perf_counter()
            log("s3_put_start", record_index=record_index, frame_id=frame_id, s3_key=s3_key)
            try:
                s3_client.put_object(
                    Bucket=s3_bucket,
                    Key=s3_key,
                    Body=img_bytes
                )
                s3_put_ms = round((time.perf_counter() - s3_start) * 1000.0, 1)
                log("s3_put_success", record_index=record_index, frame_id=frame_id,
                    s3_key=s3_key, s3_put_ms=s3_put_ms)
            except Exception as e:
                s3_put_ms = round((time.perf_counter() - s3_start) * 1000.0, 1)
                log("s3_put_failed", record_index=record_index, frame_id=frame_id,
                    s3_key=s3_key, s3_put_ms=s3_put_ms,
                    error=str(e), traceback=traceback.format_exc())
                continue

            #Persist frame data in dynamodb

            item = {
                'frame_id': frame_id,
                'processed_timestamp' : processed_timestamp,
                'approx_capture_timestamp' : approx_capture_timestamp,
                'rekog_labels' : labels,
                'rekog_orientation_correction' : orientation_correction,
                #Lets downstream (Web UI) tell "no objects detected" apart from
                #"DetectLabels was disabled to save cost".
                'labels_disabled' : not enable_detect_labels,
                'processed_year_month' : year + mon, #To be used as a Hash Key for DynamoDB GSI
                's3_bucket' : s3_bucket,
                's3_key' : s3_key,
                #TTL: DynamoDB auto-deletes this item after expire_at (epoch seconds).
                #Driven by the table's TimeToLiveSpecification (AttributeName: expire_at).
                'expire_at' : int(now_ts) + int(config.get("ddb_ttl_days", 30)) * 86400
            }

            ddb_start = time.perf_counter()
            log("dynamodb_put_start", record_index=record_index, frame_id=frame_id)
            try:
                ddb_table.put_item(Item=item)
                ddb_put_ms = round((time.perf_counter() - ddb_start) * 1000.0, 1)
                log("dynamodb_put_success", record_index=record_index, frame_id=frame_id,
                    dynamodb_put_ms=ddb_put_ms)
            except Exception as e:
                ddb_put_ms = round((time.perf_counter() - ddb_start) * 1000.0, 1)
                log("dynamodb_put_failed", record_index=record_index, frame_id=frame_id,
                    dynamodb_put_ms=ddb_put_ms,
                    error=str(e), traceback=traceback.format_exc())
                continue

            record_total_ms = round((time.perf_counter() - record_start) * 1000.0, 1)
            succeeded += 1
            log("record_complete", record_index=record_index, frame_id=frame_id,
                s3_key=s3_key, record_total_ms=record_total_ms)

        except Exception as e:
            #Unexpected per-record failure: log and keep processing remaining records.
            record_total_ms = round((time.perf_counter() - record_start) * 1000.0, 1)
            log("record_failed", record_index=record_index, record_total_ms=record_total_ms,
                error=str(e), traceback=traceback.format_exc())
            print(e)
            continue

    total_ms = round((time.perf_counter() - handler_start) * 1000.0, 1)
    log("handler_complete", record_count=record_count, succeeded=succeeded,
        failed=record_count - succeeded, total_ms=total_ms)

    print('Successfully processed {} of {} records.'.format(succeeded, record_count))

    #Flush this invocation's logs to S3 as a single per-invocation object (if enabled).
    log_util.flush_to_s3(s3_client, buffer, request_id, config)

    return

def handler(event, context):
    return process_image(event, context)
