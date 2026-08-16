import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "web-ui" / "backend"
sys.path.insert(0, str(BACKEND))

import face_collection  # noqa: E402


def test_match_expected_face_scans_all_candidates():
    matches = [
        {"face_id": "other-a", "similarity": 99.0},
        {"face_id": "wanted", "similarity": 93.5},
    ]
    result = face_collection.match_expected_face(matches, "wanted", threshold=90)
    assert result["matched"] is True
    assert result["face_id"] == "wanted"
    assert result["reason"] == "SIMILARITY_ABOVE_THRESHOLD"


def test_match_expected_face_rejects_high_scoring_other_id():
    matches = [{"face_id": "other-a", "similarity": 99.9}]
    assert face_collection.match_expected_face(matches, "wanted", threshold=90) is None


def test_match_expected_face_below_threshold():
    matches = [{"face_id": "wanted", "similarity": 40.0}]
    result = face_collection.match_expected_face(matches, "wanted", threshold=90)
    assert result["matched"] is False
    assert result["reason"] == "SIMILARITY_BELOW_THRESHOLD"


def test_mask_face_id_does_not_print_full_value():
    masked = face_collection.mask_face_id("abcdefgh-ijkl-mnop")
    assert masked.startswith("abcdefgh")
    assert "ijkl" not in masked
