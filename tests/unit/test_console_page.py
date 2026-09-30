"""The local console's page (scripts/console/app.py), run headless with
Streamlit's AppTest over a stand-in stack. Streamlit is in the `console`
dependency group only, so this file is skipped where it is not installed (CI,
a plain `uv sync`); run it with `uv run --group console pytest`."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("streamlit")

import streamlit as st
from streamlit import config
from streamlit.testing.v1 import AppTest

from scripts.console import stack
from scripts.console.stack import (
    ConsoleError,
    NoteInput,
    NoteRun,
    Options,
)

APP = str(Path(stack.__file__).with_name("app.py"))


class FakeStack:
    """What the page reads and calls on a stack, recorded."""

    refuse: str | None = None

    def __init__(self, root: Path) -> None:
        self.root = root
        self.options = Options()
        self.calls: list[Any] = []
        self.notes: list[NoteRun] = []
        self.logs: Path | None = None
        self.running = False
        self.started: list[Options] = []
        self.judged: list[tuple[NoteInput, bool]] = []

    def dead(self) -> list[str]:
        return []

    def start(self, options: Options) -> None:
        if FakeStack.refuse is not None:
            raise ConsoleError(FakeStack.refuse)
        self.started.append(options)
        self.options, self.running = options, True

    def stop(self) -> None:
        self.running = False

    def apply(self, options: Options) -> None:
        self.options = options

    def judge(self, note: NoteInput, *, resubmission: bool = False) -> NoteRun:
        self.judged.append((note, resubmission))
        run = NoteRun(len(self.judged), 200, {"score": {"band": "good"}}, Path("x"))
        self.notes.append(run)
        return run


@pytest.fixture
def made(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[FakeStack]]:
    """Every FakeStack the page builds, the page served on loopback."""
    built: list[FakeStack] = []

    def build(root: Path) -> FakeStack:
        built.append(FakeStack(root))
        return built[-1]

    monkeypatch.setenv(stack.CONSOLE_DIR_ENV, str(tmp_path / "console"))
    monkeypatch.setattr(stack, "Stack", build)
    FakeStack.refuse = None
    before = config.get_option("server.address")
    config.set_option("server.address", "127.0.0.1")
    st.cache_resource.clear()
    yield built
    config.set_option("server.address", before)
    st.cache_resource.clear()


def _page() -> AppTest:
    return AppTest.from_file(APP, default_timeout=30).run()


def test_the_page_stops_when_it_is_served_beyond_this_machine(
    made: list[FakeStack],
) -> None:
    config.set_option("server.address", "0.0.0.0")
    page = _page()
    assert "this machine only" in page.error[0].value
    assert made == []


def test_a_stopped_stack_says_so_and_sends_nothing(made: list[FakeStack]) -> None:
    page = _page()
    assert page.title[0].value == "DODEAL AI console"
    assert page.sidebar.info[0].value.startswith("Stopped")
    send = next(button for button in page.button if button.label == "Send")
    assert send.disabled


def test_start_sends_the_sidebar_settings(made: list[FakeStack]) -> None:
    page = _page()
    page.sidebar.selectbox[0].select("auto")
    page.sidebar.selectbox[1].select("egyptian_ar")
    next(b for b in page.button if b.label == "Start the stack").click()
    page.run()
    (fake,) = made
    assert fake.started == [
        Options(tenant="tenant-a", channels="auto", dialect="egyptian_ar")
    ]
    assert page.sidebar.success[0].value == "Running for tenant-a"


def test_a_refused_start_is_shown_after_the_rerun(made: list[FakeStack]) -> None:
    FakeStack.refuse = "Redis did not answer. Run: docker compose up -d redis"
    page = _page()
    next(b for b in page.button if b.label == "Start the stack").click()
    page.run()
    assert any("Redis did not answer" in error.value for error in page.error)
    assert not made[0].running


def test_a_note_is_judged_with_its_lead_fields(made: list[FakeStack]) -> None:
    page = _page()
    next(b for b in page.button if b.label == "Start the stack").click()
    page.run()
    page.text_area[0].input("An invented note about a villa.")
    lead_type = next(box for box in page.text_input if box.label == "leadType")
    lead_type.input("Villa")
    next(b for b in page.button if b.label == "Judge").click()
    page.run()
    ((note, resubmission),) = made[0].judged
    assert note == NoteInput(
        note_text="An invented note about a villa.", leadType="Villa"
    )
    assert resubmission is False
    assert page.dataframe[0].value["band"].tolist() == ["good"]


def test_an_empty_note_is_not_sent(made: list[FakeStack]) -> None:
    page = _page()
    next(b for b in page.button if b.label == "Start the stack").click()
    page.run()
    next(b for b in page.button if b.label == "Judge").click()
    page.run()
    assert made[0].judged == []
    assert page.warning[0].value == "Write a note first."
