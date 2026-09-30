"""The local console's stack (scripts/console/stack.py) and its file-and-
callback server (files.py): what a call and a note send, what the console
refuses, and that nothing is served or kept inside the repository. The
service is a mock transport here; the page itself is test_console_page.py."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from dodeal_ai.core.config import ConfigError
from scripts import call_e2e as e2e
from scripts.console import __main__ as launcher
from scripts.console import files, stack
from scripts.console.stack import (
    MAX_NOTES_PER_RUN,
    ConsoleError,
    NoteInput,
    NoteRun,
    Options,
    Stack,
    call_summary,
    console_dir,
    note_summary,
    notes_from_csv,
)

# --- the console folder and the uploads ----------------------------------------------


def test_the_console_folder_is_refused_inside_the_repository() -> None:
    with pytest.raises(ConsoleError, match="outside the repository"):
        console_dir(str(e2e.REPO_ROOT / "tmp-console"))
    assert not (e2e.REPO_ROOT / "tmp-console").exists()


def test_the_console_folder_is_made_where_asked(tmp_path: Path) -> None:
    made = console_dir(str(tmp_path / "console"))
    assert made == (tmp_path / "console").resolve()
    assert made.is_dir()


@pytest.mark.parametrize(
    ("name", "stem"),
    [
        ("Invented Call #1.mp3", "Invented_Call_1"),
        ("../../etc/passwd.wav", "passwd"),
        ("مكالمة.mp3", "call"),
        ("x" * 80 + ".mp3", "x" * 40),
    ],
)
def test_an_upload_name_becomes_a_safe_folder_name(name: str, stem: str) -> None:
    assert stack.safe_stem(name) == stem


def test_only_recording_types_are_taken() -> None:
    assert stack.audio_suffix("a.MP3") == ".mp3"
    with pytest.raises(ConsoleError, match="not a recording type"):
        stack.audio_suffix("a.html")


# --- notes ---------------------------------------------------------------------------


def test_a_notes_file_is_read_with_its_lead_fields_and_default_ids() -> None:
    text = (
        "﻿note_text,lead_id,leadType,project,deal_type\n"
        "An invented note about a flat.,,Apartment,Invented Tower,\n"
        "Another invented note.,42,,,rent\n"
    )
    first, second = notes_from_csv(text)
    assert first == NoteInput(
        note_text="An invented note about a flat.",
        leadType="Apartment",
        project="Invented Tower",
    )
    assert (second.lead_id, second.author_id, second.deal_type) == (
        42,
        stack.DEMO_AUTHOR_ID,
        "rent",
    )


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("lead_id\n1\n", "needs a note_text column"),
        ("note_text,phone\nhello,1\n", "unknown columns: phone"),
        ("note_text,lead_id\nhello,0\n", "row 2: lead_id must be a whole number"),
        ("note_text,author_id\nhello,x7\n", "row 2: author_id must be"),
        ("note_text\n\n  \n", "no rows"),
        ("note_text,project\n ,Invented Tower\n", "row 2: note_text is empty"),
        ("note_text,lead_id\n\n,\nhello,0\n", "row 4: lead_id must be"),
        ("note_text\nhello,more\n", "row 2: more cells than the header"),
    ],
)
def test_a_bad_notes_file_is_refused_by_row_and_column(text: str, why: str) -> None:
    with pytest.raises(ConsoleError, match=why) as refused:
        notes_from_csv(text)
    assert "hello" not in str(refused.value)


def test_a_notes_body_carries_the_lead_fields_given_and_nothing_else() -> None:
    note = NoteInput(note_text="An invented note.", leadType="Villa", status="New")
    assert note.body(5) == {
        "lead_id": stack.DEMO_LEAD_ID,
        "note_id": 5,
        "author_id": stack.DEMO_AUTHOR_ID,
        "note_text": "An invented note.",
        "lead": {"leadType": "Villa", "status": "New"},
    }
    moved = NoteInput(note_text="x", stage_change_to="Booked").body(6)
    assert moved["stage_change_to"] == "Booked"


# --- the stack, against a mock service -----------------------------------------------


class _Alive:
    """Children that never exit, for a stack marked running."""

    def dead(self) -> list[str]:
        return []


def _running(root: Path, handler: Any, *, stage2: bool = True) -> Stack:
    """A stack that is running as far as its methods can tell, its service a
    mock transport."""
    made = Stack(console_dir(str(root)))
    (made.root / "audio").mkdir()
    made._children = _Alive()  # type: ignore[assignment]
    made._key = "invented-signing-key-for-tests-only-0000"
    made._issuer, made._audience = "dodeal-crm", "dodeal-ai"
    made._tenant, made._host = "tenant-a", "tenant-a.dodealcrm.com"
    made._files_port = 8765
    made.options = Options(stage2=stage2)
    transport = httpx.MockTransport(handler)
    made._client = lambda: httpx.Client(  # type: ignore[method-assign]
        base_url="http://api.invalid", transport=transport
    )
    return made


def test_a_call_is_saved_outside_the_repo_and_pushed_as_the_crm_would(
    tmp_path: Path,
) -> None:
    sent: list[httpx.Request] = []

    def service(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(202, json={"job_id": "a" * 32, "status": "queued"})

    made = _running(tmp_path / "c", service)
    call = made.push_call("Invented.mp3", b"ID3", "ar", seconds_of=lambda _: 61.4)

    (request,) = sent
    body = json.loads(request.content)
    assert request.url.path == "/api/v1/calls/jobs"
    assert request.headers["Host"] == "tenant-a.dodealcrm.com"
    assert (body["duration_seconds"], body["language_hint"]) == (61, "ar")
    assert (body["lead_id"], body["author_id"]) == (1004, 7)
    name = body["audio_url"].rsplit("/", 1)[1]
    assert body["audio_url"] == f"http://127.0.0.1:8765/audio/{name}"
    assert files._SERVED_NAME.match(name)
    assert (made.root / "audio" / name).read_bytes() == b"ID3"
    assert call.job_id == "a" * 32 and made.calls == [call]
    assert call.folder.parent == made.root / "calls"


def test_a_refused_push_says_the_services_reason_and_lists_nothing(
    tmp_path: Path,
) -> None:
    def service(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"reason": "calls_not_enabled"})

    made = _running(tmp_path / "c", service)
    with pytest.raises(ConsoleError, match="403 calls_not_enabled"):
        made.push_call("Invented.mp3", b"ID3", "ar", seconds_of=lambda _: 60.0)
    assert made.calls == []


def test_nothing_is_sent_while_the_stack_is_stopped(tmp_path: Path) -> None:
    stopped = Stack(console_dir(str(tmp_path / "c")))
    with pytest.raises(ConsoleError, match="start the stack first"):
        stopped.push_call("Invented.mp3", b"ID3", "ar", seconds_of=lambda _: 60.0)
    with pytest.raises(ConsoleError, match="start the stack first"):
        stopped.judge(NoteInput(note_text="An invented note."))


def test_a_finished_call_is_reported_once_in_its_folder(tmp_path: Path) -> None:
    reads = []

    def service(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "b" * 32, "status": "queued"})
        reads.append(request)
        return httpx.Response(
            200,
            json={"job_id": "b" * 32, "status": "failed", "reason": "stt_refused"},
        )

    made = _running(tmp_path / "c", service)
    call = made.push_call("Invented.mp3", b"ID3", "en", seconds_of=lambda _: 60.0)
    made.refresh(call)
    made.refresh(call)

    assert len(reads) == 1
    assert call.reported
    assert sorted(path.name for path in call.folder.iterdir()) == [
        "outcomes.json",
        "report.html",
        "report.txt",
        "status.json",
    ]
    assert str(call_summary(call)["status / delivery / stage 2"]).startswith("failed")
    assert stack.past_reports(made.root) == [call.folder]


def test_a_call_still_running_is_not_reported(tmp_path: Path) -> None:
    def service(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "c" * 32, "status": "queued"})
        return httpx.Response(200, json={"job_id": "c" * 32, "status": "analysing"})

    made = _running(tmp_path / "c", service)
    call = made.push_call("Invented.mp3", b"ID3", "en", seconds_of=lambda _: 60.0)
    made.refresh(call)
    assert not call.reported and call.state.startswith("analysing")
    assert not (call.folder / "report.html").exists()


def test_a_note_goes_to_the_direct_route_and_its_answer_is_kept(
    tmp_path: Path,
) -> None:
    sent: list[httpx.Request] = []
    judgement = {
        "analysis": {"note_type": "follow_up", "is_vague": False},
        "score": {"band": "good", "total": 72},
        "decision": {"action": "none"},
    }

    def service(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=judgement)

    made = _running(tmp_path / "c", service)
    run = made.judge(NoteInput(note_text="An invented note.", leadType="Villa"))
    again = made.judge(NoteInput(note_text="Edited."), resubmission=True)

    assert [r.url.path for r in sent] == [
        "/api/v1/notes/judgements/direct",
        "/api/v1/notes/judgements/direct/resubmission",
    ]
    assert json.loads(sent[0].content)["lead"] == {"leadType": "Villa"}
    assert again.note_id > run.note_id
    kept = json.loads(run.saved.read_text(encoding="utf-8"))
    assert (kept["code"], kept["answer"]) == (200, judgement)
    assert run.saved.parent == made.root / "notes"
    row = note_summary(run)
    assert (row["band"], row["total"], row["type"], row["refused"]) == (
        "good",
        72,
        "follow_up",
        None,
    )


def test_a_refused_note_is_kept_as_its_reason_only(tmp_path: Path) -> None:
    def service(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"reason": "model_unavailable", "detail": "x"})

    made = _running(tmp_path / "c", service)
    run = made.judge(NoteInput(note_text="An invented note."))
    assert (run.code, run.answer) == (503, {"refused": "503 model_unavailable"})


def test_more_notes_than_the_cap_are_refused_before_any_is_sent(
    tmp_path: Path,
) -> None:
    sent: list[httpx.Request] = []

    def service(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    made = _running(tmp_path / "c", service)
    notes = [
        NoteInput(note_text=f"Invented {n}.") for n in range(MAX_NOTES_PER_RUN + 1)
    ]
    with pytest.raises(ConsoleError, match=f"at most {MAX_NOTES_PER_RUN}"):
        made.judge_many(notes)
    assert sent == []
    assert len(made.judge_many(notes[:MAX_NOTES_PER_RUN])) == MAX_NOTES_PER_RUN


def test_the_tenant_and_stage2_change_only_on_a_restart(tmp_path: Path) -> None:
    made = _running(tmp_path / "c", lambda request: httpx.Response(200, json={}))
    with pytest.raises(ConsoleError, match="only on a restart"):
        made.apply(Options(tenant="tenant-b"))
    with pytest.raises(ConsoleError, match="only on a restart"):
        made.apply(Options(stage2=False))


def test_new_settings_are_put_as_call_e2e_puts_them(tmp_path: Path) -> None:
    sent: list[httpx.Request] = []

    def service(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    made = _running(tmp_path / "c", service)
    made.apply(
        Options(channels="auto", dialect="egyptian_ar", vocabulary=("Invented",))
    )
    body = json.loads(sent[0].content)
    assert sent[0].url.path == "/api/v1/admin/tenant-config/unit_b"
    assert body["callback_url"] == "http://127.0.0.1:8765/callback"
    assert (body["audio_channels"], body["whatsapp_default_dialect"]) == (
        "auto",
        "egyptian_ar",
    )
    assert body["keyword_vocabulary"] == ["Invented"]
    assert made.options.dialect == "egyptian_ar"


def test_a_start_refusal_is_a_console_error_and_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = Stack(console_dir(str(tmp_path / "c")))
    monkeypatch.setenv("DODEAL_ENVIRONMENT", "production")
    with pytest.raises(ConsoleError, match="production"):
        made.start(Options())
    assert not made.running

    def broken() -> None:
        raise ConfigError("Missing or invalid required configuration")

    monkeypatch.setattr(e2e, "get_settings", broken)
    with pytest.raises(ConsoleError, match="settings did not load"):
        made.start(Options())
    assert not made.running


def test_ids_never_repeat(tmp_path: Path) -> None:
    made = Stack(console_dir(str(tmp_path / "c")))
    ids = [made._next_id() for _ in range(50)]
    assert len(set(ids)) == 50 and all(1 <= n < 1_000_000_000 for n in ids)


def test_a_refused_judgement_row_has_no_band() -> None:
    run = NoteRun(3, 409, {"refused": "409 duplicate_request"}, Path("x.json"))
    row = note_summary(run)
    assert (row["band"], row["refused"]) == (None, "409 duplicate_request")


# --- the file-and-callback server ----------------------------------------------------

SECRET = "invented-callback-secret"


def _signed(body: bytes, *, secret: str = SECRET) -> dict[str, str]:
    stamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), stamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return {
        "X-DODEAL-Event": "call.stage1",
        "X-DODEAL-Event-Id": "invented-id",
        "X-DODEAL-Timestamp": stamp,
        "X-DODEAL-Signature": signature,
    }


def test_only_a_saved_recording_is_served(tmp_path: Path) -> None:
    name = "0123456789abcdef0123456789abcdef.mp3"
    (tmp_path / name).write_bytes(b"ID3")
    (tmp_path / "notes.txt").write_text("private", encoding="utf-8")
    client = TestClient(files.build_app(SECRET, tmp_path, emit=lambda line: None))

    got = client.get(f"/audio/{name}")
    assert (got.status_code, got.content) == (200, b"ID3")
    assert got.headers["content-type"].startswith("audio/")
    for other in ("notes.txt", "..%2Fnotes.txt", "0123456789abcdef.mp3", "x" * 32):
        assert client.get(f"/audio/{other}").status_code == 404


def test_a_recording_type_the_download_would_refuse_goes_untyped() -> None:
    assert files.media_type("a.mp3").startswith("audio/")
    assert files.media_type("a.mp4") == "application/octet-stream"
    assert files.media_type("a.unknownsuffix") == "application/octet-stream"


def test_a_callback_is_verified_and_printed_without_its_body(tmp_path: Path) -> None:
    lines: list[str] = []
    client = TestClient(files.build_app(SECRET, tmp_path, emit=lines.append))
    body = b'{"transcript": "invented words"}'

    assert (
        client.post("/callback", content=body, headers=_signed(body)).status_code == 204
    )
    forged = _signed(body, secret="another")
    assert client.post("/callback", content=body, headers=forged).status_code == 401
    assert lines[0] == "callback call.stage1"
    assert "  signature: verified  status: 204" in lines
    assert "  signature: REFUSED  status: 401" in lines
    assert not any("invented words" in line for line in lines)


# --- the launcher --------------------------------------------------------------------


def test_the_page_listens_on_this_machine_only_and_reports_nothing_home() -> None:
    argv = launcher.streamlit_argv(8501)
    options = dict(zip(argv[3::2], argv[4::2], strict=True))
    assert options["--server.address"] == "127.0.0.1"
    assert options["--browser.gatherUsageStats"] == "false"
    assert options["--client.toolbarMode"] == "minimal"
    assert argv[:3] == ["streamlit", "run", str(launcher.APP)]


def test_the_launcher_refuses_a_folder_inside_the_repository(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert launcher.main(["--dir", str(e2e.REPO_ROOT / "tmp-console")]) == 2
    assert "outside the repository" in capsys.readouterr().out
