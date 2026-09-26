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
    assert "negations" not in rows[0]
    assert (first["negation_recall"], first["negation_by_word"]) == ("", "")


# --- negation recall (D-76) ----------------------------------------------------------


def test_a_lost_ma_is_recall_0_for_ma() -> None:
    """The guard: "I never visited" heard as "I visited" is one word of WER
    but the meaning reversed; the aligned position shows it lost."""
    heard = stt_eval.negations_heard("عمري ما زرت", "عمري زرت")
    assert heard == {"ما": (0, 1)}
    assert stt_eval.negation_recall(heard) == 0
    assert stt_eval.negation_by_word(heard) == "ما 0/1"


def test_the_alignment_pairs_each_reference_word_or_marks_it_deleted() -> None:
    assert stt_eval.alignment(["عمري", "ما", "زرت"], ["عمري", "زرت"]) == [0, None, 1]
    assert stt_eval.alignment(["a", "b"], ["x", "a", "c"]) == [1, 2]
    assert stt_eval.alignment([], ["a"]) == []
    assert stt_eval.alignment(["a"], []) == [None]


def test_a_negation_counts_only_as_the_same_word_where_it_was_said() -> None:
    """Heard, substituted, and moved: only the first is recall."""
    reference = "لا ما أبي شقة، مش الحين"
    heard = stt_eval.negations_heard(reference, "لا ما ابي شقه مو الحين")
    assert heard == {"لا": (1, 1), "ما": (1, 1), "مش": (0, 1)}
    assert stt_eval.negation_recall(heard) == 2 / 3
    assert stt_eval.negation_by_word(heard) == "ما 1/1; مش 0/1; لا 1/1"
    moved = stt_eval.negations_heard("I do not want it", "not I do want it")
    assert moved == {"not": (0, 1)}


def test_a_contraction_is_heard_only_whole() -> None:
    assert stt_eval.negations_heard("I don't want it", "I don't want it") == {
        "don't": (1, 1)
    }
    assert stt_eval.negations_heard("I don't want it", "I don want it") == {
        "don't": (0, 1)
    }
    assert stt_eval.negations_heard("I won the villa", "I won the villa") == {}


def test_no_negation_said_is_no_recall() -> None:
    heard = stt_eval.negations_heard("I want a villa", "I want a villa")
    assert (heard, stt_eval.negation_recall(heard)) == ({}, None)
    assert stt_eval.negation_by_word(heard) is None


def test_every_negation_is_one_run_of_normalised_words() -> None:
    assert stt_eval.NEGATIONS[:10] == (
        "ما",
        "مش",
        "مو",
        "مب",
        "لا",
        "ليس",
        "لم",
        "لن",
        "مافي",
        "مفيش",
    )
    assert stt_eval.NEGATIONS[10:] == (
        "not",
        "never",
        "no",
        "don't",
        "doesn't",
        "didn't",
        "won't",
        "can't",
    )
    for negation in stt_eval.NEGATIONS:
        assert stt_eval.negations_heard(negation, negation) == {negation: (1, 1)}


def test_the_all_rows_pool_the_counts_word_by_word() -> None:
    """ "ولا" counts as لا once its و is taken off."""
    rows = []
    for call, (reference, hypothesis) in enumerate(
        [("ما زرت", "زرت"), ("ما زرت ولا شفت لا", "ما زرت ولا شفت لا")]
    ):
        row = stt_eval.Row(f"c{call}", "g1", "gemini", "m", "mostly_ar", 1, 0)
        row.counted(stt_eval.negations_heard(reference, hypothesis))
        rows.append(row)
    assert [row.negation_recall for row in rows] == [0, 1]
    (pooled,) = stt_eval.summarise(rows)
    assert pooled.negations == {"ما": (1, 2), "لا": (2, 2)}
    assert (pooled.negation_recall, pooled.negation_by_word) == (
        3 / 4,
        "ما 1/2; لا 2/2",
    )


# --- prefixes and fused forms (D-76) --------------------------------------------------


@pytest.mark.parametrize(
    ("reference", "hypothesis", "heard"),
    [
        ("عمري وما زرت", "عمري وما زرت", {"ما": (1, 1)}),
        ("عمري وما زرت", "عمري ما زرت", {"ما": (1, 1)}),
        ("عمري وما زرت", "عمري زرت", {"ما": (0, 1)}),
        ("فلا تقلق", "فلا تقلق", {"لا": (1, 1)}),
        ("ومش هيك", "مش هيك", {"مش": (1, 1)}),
    ],
    ids=["wa-ma-heard", "wa-ma-heard-bare", "wa-ma-lost", "fa-la", "wa-mish"],
)
def test_one_leading_wa_or_fa_is_taken_off_first(
    reference: str, hypothesis: str, heard: dict[str, tuple[int, int]]
) -> None:
    assert stt_eval.negations_heard(reference, hypothesis) == heard


@pytest.mark.parametrize(
    ("reference", "hypothesis", "heard"),
    [
        ("انا ماعرفتش", "انا ماعرفتش", (1, 1)),
        ("انا ماعرفتش", "انا عرفت", (0, 1)),
        ("هم مبيبقوش هنا", "هم مبيبقوش هنا", (1, 1)),
        ("وماكنتش هناك", "ماكنتش هناك", (1, 1)),
    ],
    ids=["heard", "lost", "mb-prefix", "with-wa"],
)
def test_an_egyptian_fused_negation_counts(
    reference: str, hypothesis: str, heard: tuple[int, int]
) -> None:
    counted = stt_eval.negations_heard(reference, hypothesis)
    assert counted == {stt_eval.FUSED: heard}
    assert stt_eval.negation_by_word(counted) == f"م…ش {heard[0]}/{heard[1]}"


@pytest.mark.parametrize(
    "reference",
    ["مشروع جديد", "وقت الشغل", "في مشروعهم", "مش"],
    ids=["mashrou", "waqt", "fi", "mish-is-listed-not-fused"],
)
def test_a_word_that_only_looks_like_one_is_not_fused(reference: str) -> None:
    counted = stt_eval.negations_heard(reference, reference)
    assert stt_eval.FUSED not in counted
