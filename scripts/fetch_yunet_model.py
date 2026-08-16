#!/usr/bin/env python3
"""Fetch the official OpenCV Zoo YuNet ONNX and verify SHA-256.

Source: https://github.com/opencv/opencv_zoo
  models/face_detection_yunet/face_detection_yunet_2023mar.onnx

Does not upload to S3. Does not call AWS. Used for local artifact preparation.
EC2 UserData must not download this file; package it into web-ui.tgz instead.
"""
from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

OFFICIAL_URL = (
    "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/"
    "models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
MODEL_NAME = "face_detection_yunet_2023mar.onnx"
REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = REPO_ROOT / "web-ui" / "backend" / "models"
MODEL_PATH = MODELS_DIR / MODEL_NAME
CHECKSUM_PATH = MODELS_DIR / (MODEL_NAME + ".sha256")


def expected_sha256():
    text = CHECKSUM_PATH.read_text(encoding="utf-8").strip().split()
    if not text:
        raise SystemExit("empty checksum file: %s" % CHECKSUM_PATH)
    return text[0].lower()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    expected = expected_sha256()
    if MODEL_PATH.is_file() and sha256_file(MODEL_PATH) == expected:
        print("already present sha256=%s path=%s" % (expected, MODEL_PATH))
        return 0

    print("downloading official OpenCV Zoo YuNet: %s" % OFFICIAL_URL)
    tmp = MODEL_PATH.with_suffix(".onnx.partial")
    try:
        with urllib.request.urlopen(OFFICIAL_URL) as response:
            data = response.read()
    except Exception as exc:
        raise SystemExit("download failed: %s" % exc)
    tmp.write_bytes(data)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        tmp.unlink(missing_ok=True)
        raise SystemExit(
            "checksum mismatch official=%s expected=%s actual=%s"
            % (OFFICIAL_URL, expected, actual)
        )
    tmp.replace(MODEL_PATH)
    print("wrote %s bytes=%s sha256=%s" % (MODEL_PATH, len(data), actual))
    return 0


if __name__ == "__main__":
    sys.exit(main())
