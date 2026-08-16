import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_kiosk_sends_face_bytes_and_does_not_call_capture_frame():
    subprocess.run(
        [
            "node",
            str(ROOT / "tests" / "kiosk_correlation_test.js"),
            str(ROOT / "web-ui" / "src" / "kiosk.js"),
        ],
        check=True,
        cwd=ROOT,
    )
