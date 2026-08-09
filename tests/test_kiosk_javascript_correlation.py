import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_kiosk_reuses_capture_id_and_not_ready_is_not_an_attempt():
    subprocess.run(
        [
            "node",
            str(ROOT / "tests" / "kiosk_correlation_test.js"),
            str(ROOT / "web-ui" / "src" / "kiosk.js"),
        ],
        check=True,
        cwd=ROOT,
    )
