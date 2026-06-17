# Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# Licensed under the Amazon Software License (the "License"). You may not use this file except in compliance with the License. A copy of the License is located at
#     http://aws.amazon.com/asl/
# or in the "license" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and limitations under the License.

import os
import sys
import pickle
import datetime
import cv2
import boto3
import time
import traceback
from multiprocessing import Pool
from botocore.config import Config
import pytz
import log_util  # structured JSON-Lines logging (NOT the stdlib "logging" module)

# Explicit timeouts + retry policy so a slow/failing PutRecord is bounded, and so
# botocore's (otherwise invisible) retry behaviour is predictable. read_timeout caps
# a single attempt; standard retry mode adds bounded exponential backoff. The actual
# retry count is surfaced per-call via ResponseMetadata.RetryAttempts (logged below),
# which is what lets us tell "slow network" apart from "internal retries" next time.
_KINESIS_CONFIG = Config(
    connect_timeout=3,
    read_timeout=8,
    retries={"max_attempts": 3, "mode": "standard"},
    tcp_keepalive=True,
)
kinesis_client = boto3.client("kinesis", config=_KINESIS_CONFIG)
rekog_client = boto3.client("rekognition")

STREAM_NAME = "FrameStream"
PARTITION_KEY = "partitionkey"

camera_index = 0 # 0 is usually the built-in webcam
capture_rate = 60 # Frame capture rate.. every X frames. Positive integer. Cost-friendly default.
rekog_max_labels = 123
rekog_min_conf = 50.0

#Send frame to Kinesis stream
def encode_and_send_frame(frame, frame_count, enable_kinesis=True, enable_rekog=False, write_file=False, capture_rate=None):
    # capture_rate is passed explicitly (not read from the module global) so it is
    # correct in worker processes on platforms that spawn rather than fork (Windows).
    # Timing is measured with perf_counter (monotonic, high resolution).
    total_start = time.perf_counter()
    log_util.log_event("video_capture", "frame_capture_target",
                       frame_count=frame_count, capture_rate=capture_rate)
    try:
        #convert opencv Mat to jpg image
        #print "----FRAME---"
        encode_start = time.perf_counter()
        log_util.log_event("video_capture", "jpeg_encode_start", frame_count=frame_count)
        retval, buff = cv2.imencode(".jpg", frame)

        if not retval:
            log_util.log_event("video_capture", "jpeg_encode_failed",
                               frame_count=frame_count, reason="cv2.imencode returned False")
            return

        img_bytes = bytearray(buff)
        encode_ms = round((time.perf_counter() - encode_start) * 1000.0, 1)
        jpeg_bytes = len(img_bytes)
        log_util.log_event("video_capture", "jpeg_encode_success",
                           frame_count=frame_count, jpeg_bytes=jpeg_bytes, encode_ms=encode_ms)

        # Use a timezone-aware UTC datetime so ApproximateCaptureTime is a real
        # UTC epoch. The previous pytz.utc.localize(datetime.now()) mislabeled the
        # machine's LOCAL (KST) time as UTC, inflating the epoch by ~9h and making
        # the Web UI "frame age" negative.
        utc_dt = datetime.datetime.now(datetime.timezone.utc)
        now_ts_utc = utc_dt.timestamp()

        frame_package = {
            'ApproximateCaptureTime' : now_ts_utc,
            'FrameCount' : frame_count,
            'ImageBytes' : img_bytes
        }

        if write_file:
            print("Writing file img_{}.jpg".format(frame_count))
            target = open("img_{}.jpg".format(frame_count), 'w')
            target.write(img_bytes)
            target.close()

        #put encoded image in kinesis stream
        if enable_kinesis:
            print("Sending image to Kinesis")
            put_start = time.perf_counter()
            log_util.log_event("video_capture", "kinesis_put_start",
                               frame_count=frame_count, stream_name=STREAM_NAME, jpeg_bytes=jpeg_bytes)
            try:
                response = kinesis_client.put_record(
                    StreamName=STREAM_NAME,
                    Data=pickle.dumps(frame_package),
                    PartitionKey=PARTITION_KEY
                )
                put_record_ms = round((time.perf_counter() - put_start) * 1000.0, 1)
                total_ms = round((time.perf_counter() - total_start) * 1000.0, 1)
                # ResponseMetadata.RetryAttempts is the count of botocore internal
                # retries that happened INSIDE this (successful) put_record call. A
                # non-zero value here means the multi-second latency was backoff/retry,
                # not a single slow network round-trip.
                meta = response.get("ResponseMetadata", {})
                log_util.log_event("video_capture", "kinesis_put_success",
                                   frame_count=frame_count, capture_rate=capture_rate,
                                   stream_name=STREAM_NAME, jpeg_bytes=jpeg_bytes,
                                   encode_ms=encode_ms, put_record_ms=put_record_ms, total_ms=total_ms,
                                   shard_id=response.get("ShardId"),
                                   sequence_number=response.get("SequenceNumber"),
                                   retry_attempts=meta.get("RetryAttempts"),
                                   http_status=meta.get("HTTPStatusCode"),
                                   process_id=os.getpid(),
                                   partition_key=PARTITION_KEY)
                print(response)
            except Exception as e:
                # Kinesis failure must not kill the capture loop; log and continue.
                # error_type distinguishes ReadTimeoutError / ConnectTimeoutError /
                # throttling exceptions from one another when a call finally gives up.
                put_record_ms = round((time.perf_counter() - put_start) * 1000.0, 1)
                log_util.log_event("video_capture", "kinesis_put_failed",
                                   frame_count=frame_count, stream_name=STREAM_NAME,
                                   jpeg_bytes=jpeg_bytes, put_record_ms=put_record_ms,
                                   process_id=os.getpid(), partition_key=PARTITION_KEY,
                                   error_type=type(e).__name__,
                                   error=str(e), traceback=traceback.format_exc())
                print(e)

        if enable_rekog:
            response = rekog_client.detect_labels(
                Image={
                    'Bytes': img_bytes
                },
                MaxLabels=rekog_max_labels,
                MinConfidence=rekog_min_conf
            )
            print(response)

    except Exception as e:
        log_util.log_event("video_capture", "exception",
                           frame_count=frame_count, error=str(e), traceback=traceback.format_exc())
        print(e)


def main():
    global capture_rate

    argv_len = len(sys.argv)

    if argv_len > 1 and sys.argv[1].isdigit():
        capture_rate = int(sys.argv[1])

    # Optional 2nd arg: auto-stop after N seconds (cost safety). 0/absent = run
    # until 'q' / camera EOF, preserving the original behaviour.
    max_seconds = None
    if argv_len > 2 and sys.argv[2].isdigit():
        parsed = int(sys.argv[2])
        if parsed > 0:
            max_seconds = parsed

    try:
        region = boto3.session.Session().region_name
    except Exception:
        region = None

    cap = cv2.VideoCapture(camera_index) #Use 0 for built-in camera. Use 1, 2, etc. for attached cameras.

    if not cap.isOpened():
        # Webcam open failure is fatal: log clearly and exit gracefully.
        log_util.log_event("video_capture", "webcam_open_failed",
                           camera_index=camera_index, capture_rate=capture_rate,
                           stream_name=STREAM_NAME, region=region)
        sys.stderr.write(
            "ERROR: Could not open webcam at camera index {}. "
            "Check that a camera is connected and not in use.\n".format(camera_index))
        cap.release()
        return

    # Cost estimate: nominal camera fps / capture_rate = frames actually sent to
    # Kinesis (and each sent frame triggers one imageprocessor invocation, plus one
    # Rekognition DetectLabels call IF that feature is enabled server-side).
    source_fps = cap.get(cv2.CAP_PROP_FPS)
    if not source_fps or source_fps <= 0 or source_fps != source_fps:  # 0 / NaN guard
        source_fps = 30.0
    est_frames_per_sec = round(source_fps / capture_rate, 3)
    est_frames_per_min = round(est_frames_per_sec * 60, 1)
    est_frames_per_hour = int(round(est_frames_per_sec * 3600))

    log_util.log_event("video_capture", "startup",
                       capture_rate=capture_rate, stream_name=STREAM_NAME, region=region,
                       camera_index=camera_index, webcam_open=True,
                       frame_width=cap.get(cv2.CAP_PROP_FRAME_WIDTH),
                       frame_height=cap.get(cv2.CAP_PROP_FRAME_HEIGHT),
                       max_seconds=max_seconds,
                       estimated_source_fps=round(source_fps, 1),
                       est_frames_per_sec=est_frames_per_sec,
                       est_frames_per_min=est_frames_per_min,
                       est_frames_per_hour=est_frames_per_hour)

    # Human-readable cost summary on the console.
    sys.stderr.write(
        "[cost] capture_rate={} (1 frame every {} frames), source ~{:.0f} fps\n"
        "[cost] estimated send: ~{}/sec, ~{}/min, ~{}/hour frames to Kinesis\n"
        "[cost] WARNING: if DetectLabels is enabled in imageprocessor, each sent frame\n"
        "[cost]          triggers 1 Rekognition call -> ~{} calls/hour at this rate.\n"
        "[cost] auto-stop: {}\n".format(
            capture_rate, capture_rate, source_fps,
            est_frames_per_sec, est_frames_per_min, est_frames_per_hour,
            est_frames_per_hour,
            "{}s".format(max_seconds) if max_seconds else "disabled (press 'q' to quit)"))

    pool = Pool(processes=3)

    # Throughput tracking: measure ACTUAL frames sent vs wall-clock, logged
    # periodically so the real (not just estimated) fps is observable.
    loop_start = time.perf_counter()
    last_report = loop_start
    frames_sent = 0
    stop_reason = "camera_eof"

    frame_count = 0
    while True:
        # Capture frame-by-frame
        ret, frame = cap.read()
        #cv2.resize(frame, (640, 360));

        if ret is False:
            break

        if frame_count % capture_rate == 0:
            result = pool.apply_async(encode_and_send_frame, (frame, frame_count, True, False, False, capture_rate,))
            frames_sent += 1

            # Periodic throughput report (every ~50 sent frames).
            if frames_sent % 50 == 0:
                now = time.perf_counter()
                elapsed = now - loop_start
                actual_sent_fps = round(frames_sent / elapsed, 3) if elapsed > 0 else 0
                log_util.log_event("video_capture", "throughput",
                                   frames_sent=frames_sent, elapsed_sec=round(elapsed, 1),
                                   actual_sent_fps=actual_sent_fps, capture_rate=capture_rate)
                last_report = now

        frame_count += 1

        # Auto-stop after max_seconds (cost safety).
        if max_seconds is not None and (time.perf_counter() - loop_start) >= max_seconds:
            stop_reason = "auto_stop"
            log_util.log_event("video_capture", "auto_stop",
                               max_seconds=max_seconds, frames_sent=frames_sent,
                               frame_count=frame_count, capture_rate=capture_rate)
            sys.stderr.write("[cost] auto-stop reached ({}s) -- shutting down.\n".format(max_seconds))
            break

        # Display the resulting frame
        cv2.imshow('frame', frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            stop_reason = "user_quit"
            break

    # Shutdown summary: total frames sent, wall-clock, average actual fps.
    total_elapsed = round(time.perf_counter() - loop_start, 1)
    avg_sent_fps = round(frames_sent / total_elapsed, 3) if total_elapsed > 0 else 0
    log_util.log_event("video_capture", "shutdown",
                       stop_reason=stop_reason, frames_sent=frames_sent,
                       total_frames_read=frame_count, elapsed_sec=total_elapsed,
                       avg_sent_fps=avg_sent_fps, capture_rate=capture_rate)
    sys.stderr.write(
        "[cost] done: sent {} frames in {}s (avg ~{} frames/sec). reason={}\n".format(
            frames_sent, total_elapsed, avg_sent_fps, stop_reason))

    # When everything done, release the capture
    cap.release()
    cv2.destroyAllWindows()
    return

if __name__ == '__main__':
    main()

