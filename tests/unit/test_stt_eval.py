"""scripts/stt_eval.py: STT profiles scored against hand transcripts kept
outside the repo, capped by minutes, each call sent once and never retried."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.units.call_intelligence.fake_transcriber import FakeTranscriber
from dodeal_ai.units.call_intelligence.transcriber import Segment, TranscriptionError
from scripts import stt_eval


def _segment(start: float, speaker: str, text: str) -> Segment:
    return Segment(
        start_s=start,
        end_s=start + 5,
        speaker=speaker,
        text=text,
        language="en",
        confidence=None,
    )


HEARD = (
    _segment(0, "speaker_1", "Good morning, this is the sales office."),
    _segment(5, "speaker_2", "I want a villa by the sea."),
)
MARKED = {
    "segments": [
        {"speaker": "agent", "start_s": 0, "end_s": 5, "text": HEARD[0].text},
        {"speaker": "client", "start_s": 5, "end_s": 10, "text": HEARD[1].text},
    ]
}


def test_a_repo_path_is_refused(tmp_path: Path, monkeypatch) -> None:
    """The manifest, its files and the CSV all stay outside the repository."""
    monkeypatch.setenv("DODEAL_CALL_STT_PROFILES", json.dumps({"g1": _profile()}))
    get_settings.cache_clear()
    inside = stt_eval.REPO_ROOT / "manifest.json"
    assert stt_eval.main([str(inside), str(tmp_path / "out.csv")]) == 2
    with pytest.raises(stt_eval.RepoPathRefused):
        stt_eval.outside_repo(stt_eval.REPO_ROOT / "tests" / "out.csv")


def test_a_known_pair_gives_the_exact_wer() -> None:
    assert stt_eval.wer("the cat sat on the mat", "the cat sat on mat") == 1 / 6
    assert stt_eval.wer("أريد شقة", "اريد شقه") == 0
    assert stt_eval.cer("شقة", "شقق") == 1 / 3


def _profile() -> dict[str, str]:
    return {"provider": "gemini", "model": "m", "api_key_env": "EVAL_STT_KEY"}


def test_each_call_goes_once_to_each_profile_until_the_cap(
    tmp_path: Path, monkeypatch
) -> None:
    """A failure is a row, never a retry; the call past the cap is never sent."""
    profiles = {"g1": _profile(), "g2": _profile()}
    monkeypatch.setenv("DODEAL_CALL_STT_PROFILES", json.dumps(profiles))
    get_settings.cache_clear()
    engines = {
        "g1": FakeTranscriber(HEARD),
        "g2": FakeTranscriber(
            fail=TranscriptionError("stt_unavailable", retryable=True)
        ),
    }
    monkeypatch.setattr(stt_eval, "build_profile", lambda name, *_: engines[name])
    (tmp_path / "c.wav").write_bytes(b"RIFF" + b"\x00" * 64)
    (tmp_path / "c.json").write_text(json.dumps(MARKED), encoding="utf-8")
    call = {"audio": "c.wav", "reference": "c.json", "duration_seconds": 60}
    calls = [{"id": "c1", **call}, {"id": "c2", **call}]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"calls": calls}), encoding="utf-8")
    out = tmp_path / "out.csv"

    code = stt_eval.main([str(manifest), str(out), "--max-minutes", "3"])

    assert code == 0
    assert [len(engine.calls) for engine in engines.values()] == [1, 1]
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [(r["call_id"], r["profile"]) for r in rows] == [
        ("c1", "g1"),
        ("c1", "g2"),
        ("ALL", "g1"),
        ("ALL", "g2"),
    ]
    first, failed = rows[0], rows[1]
    assert (first["wer"], first["speaker_accuracy"]) == ("0.0", "1.0")
    assert (first["language_profile"], first["cer"]) == ("mostly_en", "")
    assert (failed["error"], failed["wer"]) == ("stt_unavailable", "")
