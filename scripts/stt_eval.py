"""Speech-to-text evaluation (Unit B): each STT profile on the lead's own
recordings, scored against hand transcripts that never enter this repo.

    uv run python -m scripts.stt_eval MANIFEST OUT.csv [--profiles a,b]
                                      [--max-minutes N]

THE MANIFEST is JSON, outside the repository (a repo path is refused, as is
every file it names and the CSV):
    {"calls": [{"id": "c1", "audio": "c1.wav", "reference": "c1.json",
                "duration_seconds": 312, "language_hint": "ar"}]}
Paths are relative to the manifest. A reference is {"segments": [{"speaker",
"start_s", "end_s", "text"}]}, speakers named however the marker likes.

THE PROFILES are DODEAL_CALL_STT_PROFILES (name -> provider, base_url, model,
api_key_env), each key read from the variable it names (stt.py); all of them
unless --profiles names some.

PAID AND CAPPED: each call goes once to each profile and is never retried; a
failure is a row with its fixed reason. --max-minutes caps the audio sent,
summed over profiles: the call that would pass it, and every one after it, is
not sent.

THE CSV: one row per call and profile, then one per profile and language
profile (call_id ALL) with the means and the totals. wer is word error rate;
cer, character error rate, for Arabic references only; speaker_accuracy, the
share of the reference's speech time whose voice the engine told apart (each
engine label read as the reference speaker it overlaps most); agreement, one
less the WER against each other profile's text, averaged. Words and letters
are normalised as the alarm matcher normalises them.

NEGATION RECALL (D-76): after the minimum-edit alignment of the reference's
words to the engine's (alignment), the share of the reference's negations
(NEGATIONS) the engine heard as the same word at the aligned position:
negation_recall overall, negation_by_word as "word found/said" for each one
said. A lost negation flips a sentence ("عمري ما زرت" heard "عمري زرت"), so it
counts beyond its one word of WER. The ALL rows pool the counts.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from dodeal_ai.core.config import SttProfile, get_settings
from dodeal_ai.units.call_intelligence.alarms import words
from dodeal_ai.units.call_intelligence.evidence import script_letters
from dodeal_ai.units.call_intelligence.stt import build_profile
from dodeal_ai.units.call_intelligence.transcriber import (
    Segment,
    Transcriber,
    TranscriptionError,
    profile_of,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ALL = "ALL"

# The negations whose loss reverses what was said, Arabic then English. Each
# is matched as its normalised words in a row, so "don't" is don, t; a word
# with a proclitic ("وما") is not one of them.
NEGATIONS = (
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
    "not",
    "never",
    "no",
    "don't",
    "doesn't",
    "didn't",
    "won't",
    "can't",
)
_NEGATION_WORDS = {negation: tuple(words(negation)) for negation in NEGATIONS}

# The alignment's moves back from a cell: a reference word paired with an
# engine word (a match or a substitution), deleted, or an engine word inserted.
_PAIR, _DELETE, _INSERT = 0, 1, 2

# Per negation: (heard at its aligned position, said in the reference).
type Negations = dict[str, tuple[int, int]]


class RepoPathRefused(ValueError):
    """A manifest, a file it names or the CSV inside this repository."""


class _Said(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speaker: str = Field(min_length=1)
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    text: str


class _Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segments: list[_Said]


class _Call(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    audio: Path
    reference: Path
    duration_seconds: float = Field(gt=0)
    language_hint: str | None = None


class _Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calls: list[_Call]


@dataclass(slots=True)
class Row:
    """One call on one profile, or one profile's means (call_id ALL)."""

    call_id: str
    profile: str
    provider: str
    model: str
    language_profile: str
    minutes: float
    seconds_taken: float
    wer: float | None = None
    cer: float | None = None
    speaker_accuracy: float | None = None
    agreement: float | None = None
    error: str | None = None
    negation_recall: float | None = None
    negation_by_word: str | None = None
    # The counts behind the two columns above, pooled into the ALL rows.
    negations: Negations = field(default_factory=dict, metadata={"csv": False})

    def counted(self, negations: Negations) -> None:
        """The row's negation counts, and the two columns written from them."""
        self.negations = negations
        self.negation_recall = negation_recall(negations)
        self.negation_by_word = negation_by_word(negations)


def outside_repo(path: Path) -> Path:
    """`path` resolved, or RepoPathRefused when it is inside this repository."""
    resolved = path.resolve()
    if resolved.is_relative_to(REPO_ROOT):
        raise RepoPathRefused(str(path))
    return resolved


def edit_distance(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    """Levenshtein distance: substitutions, deletions and insertions."""
    before = list(range(len(hypothesis) + 1))
    for i, wanted in enumerate(reference, start=1):
        now = [i]
        for j, heard in enumerate(hypothesis, start=1):
            now.append(
                min(before[j] + 1, now[j - 1] + 1, before[j - 1] + (wanted != heard))
            )
        before = now
    return before[-1]


def alignment(reference: Sequence[str], hypothesis: Sequence[str]) -> list[int | None]:
    """For each reference word, the position of the hypothesis word a
    minimum-edit alignment pairs it with (a match or a substitution), or None
    where it was deleted. A tie goes to the pairing, then to the deletion."""
    before = list(range(len(hypothesis) + 1))
    moves: list[bytearray] = []
    for i, wanted in enumerate(reference, start=1):
        now = [i]
        row = bytearray([_DELETE]) * (len(hypothesis) + 1)
        for j, heard in enumerate(hypothesis, start=1):
            paired = before[j - 1] + (wanted != heard)
            best = min(paired, before[j] + 1, now[j - 1] + 1)
            now.append(best)
            if best == paired:
                row[j] = _PAIR
            elif best != before[j] + 1:
                row[j] = _INSERT
        moves.append(row)
        before = now
    aligned: list[int | None] = [None] * len(reference)
    i, j = len(reference), len(hypothesis)
    while i > 0:
        move = moves[i - 1][j]
        if move == _PAIR:
            aligned[i - 1] = j - 1
        if move != _INSERT:
            i -= 1
        if move != _DELETE:
            j -= 1
    return aligned


def negations_heard(reference: str, hypothesis: str) -> Negations:
    """Each negation the reference says (NEGATIONS, over normalised words):
    how often, and how often the hypothesis has the same word at the aligned
    position -- every word of a contraction."""
    wanted, heard = words(reference), words(hypothesis)
    aligned = alignment(wanted, heard)
    counts: Negations = {}
    for at in range(len(wanted)):
        for negation, run in _NEGATION_WORDS.items():
            if tuple(wanted[at : at + len(run)]) != run:
                continue
            kept = all(
                (paired := aligned[at + k]) is not None and heard[paired] == word
                for k, word in enumerate(run)
            )
            found, said = counts.get(negation, (0, 0))
            counts[negation] = (found + kept, said + 1)
    return counts


def negation_recall(negations: Negations) -> float | None:
    """The share of the negations said that were heard; None with none said."""
    said = sum(count for _, count in negations.values())
    return sum(found for found, _ in negations.values()) / said if said else None


def negation_by_word(negations: Negations) -> str | None:
    """ "word found/said" for each negation said, in NEGATIONS order."""
    shown = [
        f"{n} {negations[n][0]}/{negations[n][1]}" for n in NEGATIONS if n in negations
    ]
    return "; ".join(shown) or None


def _pooled(rows: Sequence[Row]) -> Negations:
    """The rows' negation counts, added up word by word."""
    pooled: Negations = {}
    for row in rows:
        for negation, (found, said) in row.negations.items():
            before = pooled.get(negation, (0, 0))
            pooled[negation] = (before[0] + found, before[1] + said)
    return pooled


def wer(reference: str, hypothesis: str) -> float | None:
    """Word error rate over normalised words; None for an empty reference."""
    wanted = words(reference)
    return edit_distance(wanted, words(hypothesis)) / len(wanted) if wanted else None


def cer(reference: str, hypothesis: str) -> float | None:
    """Character error rate over normalised letters, spaces left out."""
    wanted = "".join(words(reference))
    heard = "".join(words(hypothesis))
    return edit_distance(wanted, heard) / len(wanted) if wanted else None


def speaker_accuracy(
    reference: Sequence[_Said], hypothesis: Sequence[Segment]
) -> float | None:
    """The share of the reference's speech time whose voice the engine told
    apart, each engine label read as the reference speaker it overlaps most."""
    overlap: dict[tuple[str, str], float] = defaultdict(float)
    for said in reference:
        for heard in hypothesis:
            shared = min(said.end_s, heard.end_s) - max(said.start_s, heard.start_s)
            overlap[(heard.speaker, said.speaker)] += max(shared, 0.0)
    best: dict[str, tuple[float, str]] = {}
    for (engine, marked), seconds in overlap.items():
        best[engine] = max(best.get(engine, (-1.0, "")), (seconds, marked))
    total = sum(said.end_s - said.start_s for said in reference)
    right = sum(s for (e, m), s in overlap.items() if best[e][1] == m)
    return right / total if total > 0 else None


def _arabic(text: str) -> bool:
    letters = script_letters(text)
    return letters["ar"] > letters["en"]


def _language_profile(reference: Sequence[_Said]) -> str:
    """The reference's language profile, each segment by its script."""
    return profile_of(
        tuple(
            Segment(
                start_s=said.start_s,
                end_s=max(said.end_s, said.start_s),
                speaker=said.speaker,
                text=said.text,
                language="ar" if _arabic(said.text) else "en",
                confidence=None,
            )
            for said in reference
        )
    ).value


def _load(manifest_path: Path) -> list[tuple[_Call, list[_Said]]]:
    """The manifest's calls, each file outside the repo, with its reference."""
    manifest_path = outside_repo(manifest_path)
    manifest = _Manifest.model_validate_json(manifest_path.read_bytes())
    loaded = []
    for call in manifest.calls:
        call.audio = outside_repo(manifest_path.parent / call.audio)
        call.reference = outside_repo(manifest_path.parent / call.reference)
        reference = _Reference.model_validate_json(call.reference.read_bytes())
        loaded.append((call, reference.segments))
    return loaded


async def evaluate(
    calls: list[tuple[_Call, list[_Said]]],
    engines: dict[str, tuple[SttProfile, Transcriber]],
    *,
    max_minutes: float | None,
    clock: Callable[[], float] = time.monotonic,
) -> list[Row]:
    """Every call on every profile, once each, until the cap."""
    rows: list[Row] = []
    spent = 0.0
    for call, reference in calls:
        minutes = call.duration_seconds / 60
        if max_minutes is not None and spent + minutes * len(engines) > max_minutes:
            print(f"stt_eval: --max-minutes reached before {call.id}", file=sys.stderr)
            break
        spent += minutes * len(engines)
        marked = " ".join(said.text for said in reference)
        language = _language_profile(reference)
        texts: dict[str, str] = {}
        call_rows: list[Row] = []
        for name, (profile, transcriber) in engines.items():
            row = Row(
                call.id, name, profile.provider, profile.model, language, minutes, 0
            )
            started = clock()
            try:
                transcript = await transcriber.transcribe(
                    call.audio,
                    language_hint=call.language_hint,
                    duration_seconds=call.duration_seconds,
                )
            except TranscriptionError as failed:
                row.error = failed.reason
            else:
                heard = " ".join(segment.text for segment in transcript.segments)
                texts[name] = heard
                row.wer = wer(marked, heard)
                row.counted(negations_heard(marked, heard))
                row.cer = cer(marked, heard) if _arabic(marked) else None
                row.speaker_accuracy = speaker_accuracy(reference, transcript.segments)
            row.seconds_taken = round(clock() - started, 3)
            call_rows.append(row)
        for row in call_rows:
            others = [
                wer(text, texts[row.profile])
                for name, text in texts.items()
                if name != row.profile and row.profile in texts
            ]
            agreed = [1 - rate for rate in others if rate is not None]
            row.agreement = sum(agreed) / len(agreed) if agreed else None
        rows += call_rows
    return rows + summarise(rows)


def _mean(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None


def summarise(rows: list[Row]) -> list[Row]:
    """One row per profile and language profile: means and totals."""
    groups: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for row in rows:
        groups[(row.profile, row.language_profile)].append(row)
    summary = []
    for (profile, language), group in sorted(groups.items()):
        row = Row(
            ALL,
            profile,
            group[0].provider,
            group[0].model,
            language,
            sum(row.minutes for row in group),
            sum(row.seconds_taken for row in group),
            _mean([row.wer for row in group]),
            _mean([row.cer for row in group]),
            _mean([row.speaker_accuracy for row in group]),
            _mean([row.agreement for row in group]),
            f"{sum(1 for row in group if row.error)} failed",
        )
        row.counted(_pooled(group))
        summary.append(row)
    return summary


def write_csv(path: Path, rows: list[Row]) -> None:
    with outside_repo(path).open("w", encoding="utf-8", newline="") as out:
        columns = [f.name for f in fields(Row) if f.metadata.get("csv", True)]
        writer = csv.DictWriter(out, columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    wanted = (
        args.profiles.split(",")
        if args.profiles
        else sorted(settings.call_stt_profiles)
    )
    unknown = sorted(set(wanted) - set(settings.call_stt_profiles))
    if unknown or not wanted:
        print(
            f"stt_eval: no such STT profile: {unknown or 'none set'}", file=sys.stderr
        )
        return 2
    calls = _load(args.manifest)
    outside_repo(args.out)
    async with httpx.AsyncClient() as http:
        engines = {
            name: (
                settings.call_stt_profiles[name],
                build_profile(name, settings.call_stt_profiles[name], settings, http),
            )
            for name in wanted
        }
        rows = await evaluate(calls, engines, max_minutes=args.max_minutes)
    write_csv(args.out, rows)
    print(f"stt_eval: {len(rows)} rows written")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("manifest", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--profiles", default="")
    parser.add_argument("--max-minutes", type=float, default=None)
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except RepoPathRefused:
        print("stt_eval: a path inside this repository was refused", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
