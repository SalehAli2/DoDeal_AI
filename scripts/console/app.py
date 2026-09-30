"""The console's page (README, "The local console"): start the stack, send
recordings and notes, read what the service made of them.

Started by `python -m scripts.console`, which binds it to this machine; served
on any other address it stops at once. What it shows is also saved in the
console folder (stack.py), and nothing is logged.
"""

from __future__ import annotations

import atexit
import os
from typing import get_args

import httpx
import streamlit as st
from streamlit.runtime.uploaded_file_manager import UploadedFile

from dodeal_ai.units.call_intelligence.audio import AudioChannels
from dodeal_ai.units.call_intelligence.extras import WhatsAppDialect
from dodeal_ai.units.structured_intelligence.schemas import MAX_NOTE_TEXT_CHARS
from scripts.console.__main__ import ADDRESS
from scripts.console.stack import (
    AUDIO_SUFFIXES,
    CONSOLE_DIR_ENV,
    DEMO_AUTHOR_ID,
    DEMO_LEAD_ID,
    MAX_NOTES_PER_RUN,
    NOTE_COLUMNS,
    ConsoleError,
    NoteInput,
    Options,
    Stack,
    call_summary,
    console_dir,
    note_summary,
    notes_from_csv,
    past_reports,
)

LANGUAGES = ("mixed", "ar", "en")
DEFAULT = "(the tenant's default)"
CHANNELS = (DEFAULT, *get_args(AudioChannels.__value__))
DIALECTS = (DEFAULT, *get_args(WhatsAppDialect.__value__))
# How often the list of calls reads their jobs again, in seconds.
REFRESH_SECONDS = 3
# The newest answers shown in full under the notes table.
SHOWN_ANSWERS = 10


@st.cache_resource
def shared_stack() -> Stack:
    """The one stack every page session shares, stopped when the console
    exits."""
    made = Stack(console_dir(os.environ.get(CONSOLE_DIR_ENV)))
    atexit.register(made.stop)
    return made


def _or_none(choice: str) -> str | None:
    return None if choice == DEFAULT else choice


def _flash(message: str) -> None:
    """A message kept across the rerun that follows an action."""
    st.session_state.setdefault("flash", []).append(message)


def _show_flash() -> None:
    for message in st.session_state.pop("flash", []):
        st.error(message)


def vocabulary_of(
    upload: UploadedFile | None, kept: tuple[str, ...] | None
) -> tuple[str, ...] | None:
    """The terms of an uploaded vocabulary file, one per line, or those kept;
    UnicodeDecodeError for a file that is not UTF-8."""
    if upload is None:
        return kept
    text = upload.getvalue().decode("utf-8-sig")
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def sidebar(stack: Stack) -> None:
    bar = st.sidebar
    bar.title("Stack")
    if stack.running and stack.dead():
        bar.error(
            f"Stopped by itself: {', '.join(stack.dead())}. Read the logs in "
            f"{stack.logs}, then stop and start again."
        )
    elif stack.running:
        bar.success(f"Running for {stack.options.tenant}")
    else:
        bar.info("Stopped. Redis must be up: docker compose up -d redis")
    if stack.logs is not None:
        bar.caption(f"Logs: {stack.logs}")
    kept = stack.options
    with bar.form("options"):
        tenant = st.text_input("Tenant", kept.tenant, disabled=stack.running)
        stt_profile = st.text_input(
            "Speech-to-text profile",
            kept.stt_profile,
            help="Blank: the engine your .env names (DODEAL_CALL_STT_PROVIDER).",
        )
        channels = st.selectbox(
            "Channels", CHANNELS, index=CHANNELS.index(kept.channels or DEFAULT)
        )
        dialect = st.selectbox(
            "WhatsApp dialect", DIALECTS, index=DIALECTS.index(kept.dialect or DEFAULT)
        )
        words = st.file_uploader(
            "Keyword vocabulary (.txt, one term per line)", type=["txt"]
        )
        clear = st.checkbox(
            f"Clear the vocabulary ({len(kept.vocabulary or ())} terms)",
            disabled=kept.vocabulary is None,
        )
        stage2 = st.checkbox(
            "Run stage 2: score, coaching, extras",
            kept.stage2,
            disabled=stack.running,
        )
        label = "Apply settings" if stack.running else "Start the stack"
        submitted = st.form_submit_button(label, type="primary")
    if submitted:
        try:
            options = Options(
                tenant=tenant.strip() or "tenant-a",
                stt_profile=stt_profile.strip(),
                channels=_or_none(channels),
                dialect=_or_none(dialect),
                vocabulary=None if clear else vocabulary_of(words, kept.vocabulary),
                stage2=stage2,
            )
            if stack.running:
                stack.apply(options)
            else:
                with st.spinner("Starting the API and the workers (about 20 s)"):
                    stack.start(options)
        except UnicodeDecodeError:
            _flash("The vocabulary file is not UTF-8 text.")
        except (ConsoleError, httpx.HTTPError) as refused:
            _flash(str(refused) or type(refused).__name__)
        st.rerun()
    if stack.running and bar.button("Stop the stack"):
        stack.stop()
        st.rerun()


@st.fragment(run_every=REFRESH_SECONDS)
def live_calls(stack: Stack) -> None:
    """This session's calls, each read again until its report is written."""
    calls = list(reversed(stack.calls))
    if not calls:
        st.caption("No call sent yet.")
        return
    newly_reported = False
    for call in calls:
        if call.reported:
            continue
        try:
            stack.refresh(call)
        except httpx.HTTPError:
            continue
        newly_reported = newly_reported or call.reported
    st.dataframe([call_summary(call) for call in calls], hide_index=True)
    if newly_reported:
        st.rerun()


def report_viewer(stack: Stack) -> None:
    st.subheader("Reports")
    folders = past_reports(stack.root)
    if not folders:
        st.caption("A call's report appears here when its job has finished.")
        return
    chosen = st.selectbox("Report", folders, format_func=lambda path: path.name)
    page = (chosen / "report.html").read_text(encoding="utf-8")
    left, right = st.columns(2)
    left.download_button(
        "Download report.html",
        page,
        file_name=f"{chosen.name}_report.html",
        mime="text/html",
    )
    status = chosen / "status.json"
    if status.is_file():
        right.download_button(
            "Download status.json",
            status.read_bytes(),
            file_name=f"{chosen.name}_status.json",
            mime="application/json",
        )
    st.caption(
        f"Saved in {chosen}. The score is a local test, never for judging a person."
    )
    # The page escapes every value it shows (call_e2e.esc) and runs no script
    # of its own, so model output reaches the frame as text only.
    st.iframe(page, height="content")


def calls_tab(stack: Stack) -> None:
    st.subheader("Send recordings")
    round_ = st.session_state.setdefault("calls_round", 0)
    uploads = st.file_uploader(
        "Recordings",
        type=[suffix.lstrip(".") for suffix in AUDIO_SUFFIXES],
        accept_multiple_files=True,
        key=f"calls-{round_}",
    )
    language = st.radio("Language hint", LANGUAGES, horizontal=True)
    if not stack.running:
        st.caption("Start the stack in the sidebar to send.")
    if st.button("Send", type="primary", disabled=not (stack.running and uploads)):
        for upload in uploads or []:
            try:
                stack.push_call(upload.name, upload.getvalue(), language)
            except (ConsoleError, httpx.HTTPError) as refused:
                _flash(f"{upload.name}: {refused or type(refused).__name__}")
        st.session_state["calls_round"] = round_ + 1
        st.rerun()
    st.subheader("This session's calls")
    live_calls(stack)
    report_viewer(stack)


def _judge(stack: Stack, notes: list[NoteInput], resubmission: bool) -> str | None:
    """Judge each note in turn; why the run stopped, or None."""
    progress = st.progress(0.0)
    try:
        for n, note in enumerate(notes, start=1):
            stack.judge(note, resubmission=resubmission)
            progress.progress(n / len(notes))
    except (ConsoleError, httpx.HTTPError) as failed:
        return str(failed) or type(failed).__name__
    return None


def one_note(stack: Stack) -> None:
    with st.form("note"):
        text = st.text_area("Note", max_chars=MAX_NOTE_TEXT_CHARS, height=160)
        ids = st.columns(3)
        lead_id = ids[0].number_input("Lead id", min_value=1, value=DEMO_LEAD_ID)
        author_id = ids[1].number_input("Author id", min_value=1, value=DEMO_AUTHOR_ID)
        stage = ids[2].text_input("Stage the lead moves to (optional)")
        lead = st.columns(5)
        fields = {
            name: lead[n].text_input(name)
            for n, name in enumerate(
                ("leadType", "enquiryType", "project", "status", "deal_type")
            )
        }
        resubmission = st.checkbox("Resubmission: the note edited after a question")
        sent = st.form_submit_button(
            "Judge", type="primary", disabled=not stack.running
        )
    if sent and not text.strip():
        st.warning("Write a note first.")
    elif sent:
        note = NoteInput(
            note_text=text.strip(),
            lead_id=int(lead_id),
            author_id=int(author_id),
            stage_change_to=stage.strip() or None,
            **{name: value.strip() or None for name, value in fields.items()},
        )
        stopped = _judge(stack, [note], resubmission)
        if stopped is not None:
            st.error(stopped)


def many_notes(stack: Stack) -> None:
    st.caption(
        f"A CSV file with a header row. Columns: {', '.join(NOTE_COLUMNS)}; only "
        f"note_text is required. At most {MAX_NOTES_PER_RUN} notes per click: "
        "each is a paid judgement."
    )
    round_ = st.session_state.setdefault("notes_round", 0)
    upload = st.file_uploader("Notes file", type=["csv"], key=f"notes-{round_}")
    if upload is None:
        return
    try:
        notes = notes_from_csv(upload.getvalue().decode("utf-8-sig"))
    except UnicodeDecodeError:
        st.error("The file is not UTF-8 text.")
        return
    except ConsoleError as refused:
        st.error(str(refused))
        return
    too_many = len(notes) > MAX_NOTES_PER_RUN
    if too_many:
        st.warning(f"{len(notes)} notes: split the file, {MAX_NOTES_PER_RUN} at most.")
    resubmission = st.checkbox("Resubmissions", key="many-resubmission")
    label = f"Judge {len(notes)} notes"
    if st.button(label, type="primary", disabled=too_many or not stack.running):
        stopped = _judge(stack, notes, resubmission)
        if stopped is not None:
            _flash(stopped)
        # The file is cleared once sent, so a second click never pays twice.
        st.session_state["notes_round"] = round_ + 1
        st.rerun()


def notes_tab(stack: Stack) -> None:
    if not stack.running:
        st.caption("Start the stack in the sidebar to judge.")
    one, many = st.tabs(["One note", "A file of notes"])
    with one:
        one_note(stack)
    with many:
        many_notes(stack)
    st.subheader("This session's judgements")
    runs = list(reversed(stack.notes))
    if not runs:
        st.caption("No note judged yet.")
        return
    st.dataframe([note_summary(run) for run in runs], hide_index=True)
    for run in runs[:SHOWN_ANSWERS]:
        with st.expander(f"Note {run.note_id}: HTTP {run.code}"):
            st.caption(f"Saved in {run.saved}")
            st.json(run.answer)


def main() -> None:
    st.set_page_config(page_title="DODEAL AI console", layout="wide")
    if st.get_option("server.address") != ADDRESS:
        st.error(
            "Start the console with `python -m scripts.console`: this page shows "
            "real calls and notes, so it must listen on this machine only."
        )
        st.stop()
    stack = shared_stack()
    sidebar(stack)
    st.title("DODEAL AI console")
    _show_flash()
    calls, notes = st.tabs(["Calls", "Notes"])
    with calls:
        calls_tab(stack)
    with notes:
        notes_tab(stack)


main()
