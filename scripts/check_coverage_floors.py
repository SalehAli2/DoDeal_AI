"""Per-file coverage floors for the modules a tenancy breach would run through.

The repo-wide gate in pyproject.toml (fail_under) is an AVERAGE: a security
module can rot to 60% while the total stays above 92% because well-covered
code elsewhere carries it. These floors are per FILE, so no file below can be
paid for by another.

The list is deliberately short -- the code on a deny path, plus the code that
decides what a salesperson is told about their own work:

    core/auth/**      Gate 1: who the caller is. Wrong -> everyone is anyone.
    core/tenancy.py   Gate 2: whose data. Wrong -> a cross-tenant read.
    core/cost/**      Gate 4: the spend cap. Wrong -> an unbounded bill.
    core/breaker.py   whether a Redis store is asked at all. Wrong -> a
                      reservation refused for a whole open window.
    core/errors.py    what a denied caller is told (and is NOT told).
    core/validation.py  the untrusted-output boundary.
    core/log_safety.py  what may reach a log line.
    units/structured_intelligence/**  Unit A's judgement pipeline.
      ... /config.py   the ONLY source of a weight, threshold, cap or TTL.
                       Wrong -> a silently wrong score, not a crash.
      ... /state.py    three state concerns with three DIFFERENT failure
                       policies. Wrong -> duplicate paid work, or a user
                       pestered past their cap.
      ... /decide.py   whether we interrupt a salesperson to ask a question,
                       and what the CRM is advised to do. Wrong -> nagging, or
                       silence where a note needed a question.
    core/llm/openai_compatible.py  the paid call. Wrong -> a retry, or a
                       provider's message in an exception.
    middleware/body_limit.py  the byte check before every gate. Wrong -> an
                       unbounded body read on behalf of anyone.
    core/logging_config.py  how every log line is written. Wrong -> a raw
                       exception message on the stream, or no line at all.
    middleware/inflight.py  load shedding. Wrong -> an unbounded queue, or
                       a slot that is never given back.
    tools/errors.py   typed CRM failures. Wrong -> a 404 reported as a 503.
    core/llm/fallback.py  when a second provider is called. Wrong -> a paid
                       call sent twice.
    core/inflight.py  the counter load shedding reads. Wrong -> a count that
                       drifts, shedding traffic the service could serve.
    api/routes/admin.py  a tenant's rules changed at runtime. Wrong -> a rule
                       stored or refused on a path nobody ran.
    tools/crm_reads.py  the CRM reads behind the brief. Wrong -> a page lost.
    units/structured_intelligence/measures.py  the per-rep measures a manager
                       is shown. Wrong -> a figure nobody tested.
    units/structured_intelligence/language.py  the note's script. Wrong -> a
                       question asked in the wrong language.
    core/audio_download.py  a call's recording fetched. Wrong -> SSRF.
    core/callbacks.py  a signed callback. Wrong -> an unsigned transcript.
    core/jobs.py      a call job's state. Wrong -> a job paid twice or lost.
    units/call_intelligence/worker.py  process_call. Wrong -> a paid retry
                       nobody asked for, or a job stranded.
    units/call_intelligence/ numbers, alarms, evidence, paid, analysis, sweep,
                       queues and the wave 2 passes. Wrong -> a number a
                       model reads, an invented quote believed, a received
                       answer paid for twice, a stuck job never swept.
    units/call_intelligence/stage2.py  analyse_stage2. Wrong -> a stage 2
                       left pending, or its event never sent.

A file matched by both a `**` pattern and its own exact pattern is checked
against both and printed twice; the stricter floor governs. That is deliberate
for config.py.

100 where the file is small enough that every line is reachable in a unit
test; 95 where a defensive branch is not worth contorting a test to reach.

Run AFTER pytest, which writes coverage.json (pyproject addopts). Stdlib only:
CI runs it as a plain `uv run python scripts/check_coverage_floors.py` step and
it must not need anything pytest did not already install.
"""

from __future__ import annotations

import json
import sys
from fnmatch import fnmatch
from pathlib import Path

_COVERAGE_JSON = Path("coverage.json")

# glob pattern (forward slashes, repo-relative) -> minimum percent covered
_FLOORS: dict[str, float] = {
    "src/dodeal_ai/core/auth/**": 95,
    # The CRM's service token (register item D1): every refusal is a reason code
    # a test can reach, and a gap is a service call admitted on a path nobody ran.
    "src/dodeal_ai/core/auth/service.py": 100,
    "src/dodeal_ai/core/tenancy.py": 100,
    "src/dodeal_ai/core/cost/**": 95,
    # The two counters, the two scripts, and the only place a judgement is
    # refused for money. Every branch here is a spend decision or a fail-open
    # path, and both are cheap to reach against a faked store.
    "src/dodeal_ai/core/cost/limiter.py": 100,
    # The Redis circuit breaker: what decides, per call, whether a store is asked
    # at all. A small state machine on an injected clock, so every transition is
    # reachable -- and a wrong one is a fail-closed reservation refused for 30s.
    "src/dodeal_ai/core/breaker.py": 100,
    "src/dodeal_ai/core/errors.py": 95,
    "src/dodeal_ai/core/validation.py": 100,
    "src/dodeal_ai/core/log_safety.py": 100,
    # What a model reads of a note (register item 59). Pure functions over a
    # string, so every rule and every kept shape is reachable in a unit test.
    "src/dodeal_ai/core/redaction.py": 100,
    # Unit A. The unit decides what a salesperson is told about their own work,
    # and its config is the only source of a weight or a threshold -- a gap
    # there is a silently wrong score, not a crash.
    "src/dodeal_ai/units/structured_intelligence/**": 95,
    "src/dodeal_ai/units/structured_intelligence/config.py": 100,
    "src/dodeal_ai/units/structured_intelligence/state.py": 95,
    # The arithmetic a salesperson is shown and asked to accept. A gap here is a
    # silently wrong total, not a crash -- and it is pure functions over marks
    # and config, so there is no excuse for an uncovered line.
    "src/dodeal_ai/units/structured_intelligence/scoring.py": 100,
    # The untrusted-parse boundary: the ONE place model output becomes a typed
    # object, and the one place a malformed answer decides its own fate. Every
    # branch here is a security branch -- truncation, decode failure, schema
    # failure, rule failure, the single reprompt, and the 503 after it.
    "src/dodeal_ai/units/structured_intelligence/llm_call.py": 100,
    # The order the whole unit runs in, and the three stop-points that cost
    # money if they move. 95 rather than 100: the release-on-failure branch and
    # the version-mismatch line are defensive, and contorting a test to reach
    # the last statement of either is worse than the statement being uncovered.
    "src/dodeal_ai/units/structured_intelligence/pipeline.py": 95,
    # What a salesperson is actually told, and whether we interrupt them to ask
    # a question. Pure functions over a total, two counters and the config --
    # no I/O, no clock, no model -- so every branch is reachable and a gap here
    # is a wrong decision shipped silently.
    "src/dodeal_ai/units/structured_intelligence/decide.py": 100,
    # Register item 113, four deny-path files. The adapter every paid call goes
    # through: no retry, and an exception carries a status and a provider name
    # only. Its failure paths run on a fake transport, so a gap is an untested one.
    "src/dodeal_ai/core/llm/openai_compatible.py": 100,
    # The byte check before every gate (item 87). Plain ASGI, so the header path
    # and the streamed path both run without a server; a gap is a body read in
    # full for a caller we were always going to refuse.
    "src/dodeal_ai/middleware/body_limit.py": 100,
    # How every log line is written: extra= fields lifted, an exception reduced
    # to its type and frames. A gap is a line that ships a raw message, or none.
    "src/dodeal_ai/core/logging_config.py": 100,
    # The typed backend failures (items 89 and F1): which 4xx becomes which code,
    # and the Retry-After parse. Pure classes and one function; a gap is a
    # status the pipeline maps without a test behind it.
    "src/dodeal_ai/tools/errors.py": 100,
    # The fallback provider (item 21): the one rule for when a second paid call
    # is allowed. A gap is a retry of a call that may already have been billed.
    "src/dodeal_ai/core/llm/fallback.py": 100,
    # The in-flight counter itself (item 13): three methods whose every line is a
    # slot taken, refused or given back. A gap is a count that can drift unseen.
    "src/dodeal_ai/core/inflight.py": 100,
    # Load shedding (item 73): admit, refuse, and give the slot back in a finally.
    # 95 as item 113 sets it; the file measured 100 when the floor was added, so
    # the margin is headroom and not a known uncovered line.
    "src/dodeal_ai/middleware/inflight.py": 95,
    # Register item 113, four more, asked for at 100; each measured 100 when
    # added. The admin routes: a gap is a rule change stored or refused untested.
    "src/dodeal_ai/api/routes/admin.py": 100,
    # The keyset CRM reads (item 143): a gap is a page or a refusal of the
    # backend's answer that no test has read.
    "src/dodeal_ai/tools/crm_reads.py": 100,
    # The per-rep measures (item 144): pure arithmetic a manager acts on, so a
    # gap is a figure or a suppression shipped without a test behind it.
    "src/dodeal_ai/units/structured_intelligence/measures.py": 100,
    # The note's script (item 34): pure functions over a string; a gap is a
    # note answered in the wrong language.
    "src/dodeal_ai/units/structured_intelligence/language.py": 100,
    # Register item 113, Unit B. The call budgets are core/cost/limiter.py, at
    # 100 above. The recording's download: every refusal is an SSRF or a disk
    # guard, reachable on a fake transport, so a gap is an untested refusal.
    "src/dodeal_ai/core/audio_download.py": 100,
    # Callback signing: a gap is a callback path sent without a test behind it.
    "src/dodeal_ai/core/callbacks.py": 100,
    # The job store's scripts run for real on fakeredis[lua]; a gap is a state
    # move -- a claim, a pause, a delivery -- that no test has made.
    "src/dodeal_ai/core/jobs.py": 100,
    # process_call: 95, as item 113 sets it, for a defensive branch or two.
    "src/dodeal_ai/units/call_intelligence/worker.py": 95,
    # Unit B batch 3. What a model may read of a call (the mask), what code
    # finds without one (alarms), and the quote check every pass rests on: a
    # gap is a number shown or an invented quote believed.
    "src/dodeal_ai/units/call_intelligence/numbers.py": 100,
    "src/dodeal_ai/units/call_intelligence/alarms.py": 100,
    "src/dodeal_ai/units/call_intelligence/evidence.py": 100,
    # The retry-once rule and the work kept: a gap is a received answer paid for
    # twice. Wave 1 is the order its passes run in.
    "src/dodeal_ai/units/call_intelligence/paid.py": 100,
    "src/dodeal_ai/units/call_intelligence/analysis.py": 100,
    # The sweep and the queues it asks: a gap is a stuck job or a lost stage 2
    # never found, or a second copy of one that was only waiting.
    "src/dodeal_ai/units/call_intelligence/sweep.py": 100,
    "src/dodeal_ai/units/call_intelligence/queues.py": 100,
    # Wave 2 and its five passes: each rule a schema cannot hold, and every
    # mark, total and band computed in code. A gap is a rule nobody ran.
    "src/dodeal_ai/units/call_intelligence/wave2.py": 100,
    "src/dodeal_ai/units/call_intelligence/objections.py": 100,
    "src/dodeal_ai/units/call_intelligence/score.py": 100,
    "src/dodeal_ai/units/call_intelligence/escalations.py": 100,
    "src/dodeal_ai/units/call_intelligence/coaching.py": 100,
    "src/dodeal_ai/units/call_intelligence/extras.py": 100,
    # analyse_stage2: 95, as for process_call, for a defensive branch or two.
    "src/dodeal_ai/units/call_intelligence/stage2.py": 95,
}


def _matches(pattern: str, path: str) -> bool:
    """`**` means "this directory and everything under it"; a plain path is an
    exact match. fnmatch alone would let `*` cross a directory separator, so
    the directory form is handled explicitly."""
    if pattern.endswith("/**"):
        return path.startswith(pattern[: -len("**")])
    return fnmatch(path, pattern)


def main() -> int:
    if not _COVERAGE_JSON.exists():
        print(
            f"{_COVERAGE_JSON} not found. Run `uv run pytest` first -- it writes "
            "the JSON report these floors read (pyproject addopts).",
            file=sys.stderr,
        )
        return 2

    report = json.loads(_COVERAGE_JSON.read_text(encoding="utf-8"))
    # coverage records the OS-native path; normalise so the patterns above are
    # written once and work on Windows and Linux alike.
    measured = {
        path.replace("\\", "/"): data["summary"]["percent_covered"]
        for path, data in report["files"].items()
    }

    failures = 0
    unmatched = []
    print(f"{'file':<48}{'covered':>9}{'floor':>7}  result")
    print("-" * 74)
    for pattern, floor in _FLOORS.items():
        hits = sorted(p for p in measured if _matches(pattern, p))
        if not hits:
            unmatched.append(pattern)
            continue
        for path in hits:
            percent = measured[path]
            ok = percent >= floor
            failures += not ok
            print(
                f"{path:<48}{percent:>8.2f}%{floor:>6.0f}%  {'PASS' if ok else 'FAIL'}"
            )

    if unmatched:
        # Fail closed: a renamed or deleted file must not silently drop its
        # floor. Update _FLOORS deliberately instead.
        print()
        for pattern in unmatched:
            print(f"NO FILE MATCHED: {pattern}", file=sys.stderr)
        print(
            "A floor that matches nothing is not enforcing anything. Update "
            "_FLOORS in this script to match the current layout.",
            file=sys.stderr,
        )
        return 1

    print()
    if failures:
        print(f"{failures} file(s) below their coverage floor.", file=sys.stderr)
        return 1
    print(f"All {len(_FLOORS)} coverage floors met.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
