"""The call demo (Unit B): CALL_DEMO_ALLOW_LOCAL_AUDIO admits http and
loopback -- and, off, refuses them -- with an ERROR at startup, and
scripts/call_demo.py serves audio, verifies the service's own signatures and
prints only event, headers and status."""

from __future__ import annotations

import io
import json
import logging
import time
import wave
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from dodeal_ai.core.audio_download import AudioDownloadError, downloaded_audio
from dodeal_ai.core.callbacks import CALL_STAGE1, Delivery, post_event
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.logging_config import warn_if_demo_audio
from dodeal_ai.units.call_intelligence.config import parse_unit_b_section
from dodeal_ai.units.call_intelligence.schemas import CallJobRequest
from scripts import call_demo
from scripts import mint_demo_token as minter

SECRET = "demo-callback-secret"
NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)


@pytest.fixture
def demo_on(monkeypatch) -> None:
    monkeypatch.setenv("DODEAL_CALL_DEMO_ALLOW_LOCAL_AUDIO", "true")
    monkeypatch.setenv("DODEAL_CALL_CALLBACK_SECRETS", json.dumps({"tenant-a": SECRET}))
    get_settings.cache_clear()


async def _loopback(host: str, port: int) -> list[str]:
    return ["127.0.0.1"]


def _app_client(emitted: list[str]) -> httpx.AsyncClient:
    app = call_demo.build_app(SECRET, call_demo.silent_wav(), emitted.append)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app))


# --- the guard: off refuses loopback, on admits it ---------------------------


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://127.0.0.1:8765/demo-call.wav", "audio_scheme_refused"),
        ("https://127.0.0.1/demo-call.wav", "audio_address_refused"),
    ],
)
async def test_with_the_flag_off_loopback_and_http_are_refused(
    url: str, reason: str
) -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(AudioDownloadError) as refused:
            async with downloaded_audio(
                url,
                expires_at=NOW + timedelta(hours=1),
                now=NOW,
                allowed_hosts=frozenset({"127.0.0.1"}),
                max_bytes=1 << 20,
                timeout_seconds=5,
                http=http,
                resolve=_loopback,
            ):
                pass
    assert refused.value.reason == reason


async def test_with_the_flag_on_the_demo_recording_is_fetched() -> None:
    emitted: list[str] = []
    async with (
        _app_client(emitted) as http,
        downloaded_audio(
            "http://127.0.0.1:8765/demo-call.wav",
            expires_at=NOW + timedelta(hours=1),
            now=NOW,
            allowed_hosts=frozenset({"127.0.0.1"}),
            max_bytes=1 << 20,
            timeout_seconds=5,
            http=http,
            resolve=_loopback,
            allow_local=True,
        ) as audio,
    ):
        assert audio.content_type == "audio/wav"
        assert audio.path.read_bytes() == call_demo.silent_wav()


async def test_a_callback_to_loopback_is_refused_off_and_verified_on(
    demo_on,
) -> None:
    """End to end: the service's signer, the demo's check, and nothing but
    event, headers and status printed."""
    emitted: list[str] = []
    body = b'{"transcript":"invented words that must not be printed"}'
    async with _app_client(emitted) as http:
        kwargs = {
            "tenant": "tenant-a",
            "event": CALL_STAGE1,
            "event_id": "e-1",
            "body": body,
            "timestamp": str(int(time.time())),
            "http": http,
            "resolve": _loopback,
        }
        url = "http://127.0.0.1:8765/callback"
        assert await post_event(url, **kwargs) is Delivery.REFUSED
        assert emitted == []
        assert await post_event(url, allow_local=True, **kwargs) is Delivery.DELIVERED

    assert emitted[0] == f"callback {CALL_STAGE1}"
    assert "  signature: verified  status: 204" in emitted
    assert all("invented words" not in line for line in emitted)


def test_the_push_and_the_callback_url_need_the_flag_for_http(monkeypatch) -> None:
    push = call_demo.push_body(8765, now=NOW)
    config = {"callback_url": "http://127.0.0.1:8765/callback"}
    with pytest.raises(ValidationError):
        CallJobRequest.model_validate(push)
    with pytest.raises(ValidationError):
        parse_unit_b_section(config)

    monkeypatch.setenv("DODEAL_CALL_DEMO_ALLOW_LOCAL_AUDIO", "true")
    get_settings.cache_clear()
    assert CallJobRequest.model_validate(push).audio_url.startswith("http://127.0.0.1")
    assert parse_unit_b_section(config).callback_url == config["callback_url"]


def test_the_flag_is_one_error_at_startup(monkeypatch, caplog) -> None:
    caplog.set_level(logging.ERROR, logger="dodeal_ai.startup")
    warn_if_demo_audio(get_settings())
    assert not caplog.records
    monkeypatch.setenv("DODEAL_CALL_DEMO_ALLOW_LOCAL_AUDIO", "true")
    get_settings.cache_clear()
    warn_if_demo_audio(get_settings())
    (line,) = caplog.records
    assert line.__dict__["event"] == "call_demo_audio_insecure"


# --- the receiver ------------------------------------------------------------------


def test_the_receiver_refuses_a_bad_a_stale_or_a_garbled_signature() -> None:
    now = str(int(time.time()))
    good = call_demo.hmac.new(
        SECRET.encode(), now.encode() + b".{}", call_demo.hashlib.sha256
    ).hexdigest()
    assert call_demo.verified(SECRET, now, b"{}", good)
    assert not call_demo.verified(SECRET, now, b"{}", "0" * 64)
    assert not call_demo.verified(SECRET, now, b"{ }", good)
    stale = str(int(time.time()) - 301)
    assert not call_demo.verified(SECRET, stale, b"{}", good)
    assert not call_demo.verified(SECRET, "yesterday", b"{}", good)


async def test_an_unsigned_callback_is_401_and_says_refused() -> None:
    emitted: list[str] = []
    async with _app_client(emitted) as client:
        response = await client.post("http://demo/callback", content=b"{}")
    assert response.status_code == 401
    assert emitted[0] == "callback (no event)"
    assert emitted[-1] == "  signature: REFUSED  status: 401"


def test_the_demo_recording_is_a_real_wav() -> None:
    with wave.open(io.BytesIO(call_demo.silent_wav()), "rb") as audio:
        assert (audio.getnchannels(), audio.getframerate()) == (1, 8000)


def test_the_instructions_name_the_flag_the_secret_and_both_curls() -> None:
    lines = call_demo.instructions(
        port=8765,
        tenant="tenant-a",
        secret=SECRET,
        base_url="http://localhost:8000",
        now=NOW,
    )
    text = "\n".join(lines)
    assert "DODEAL_CALL_DEMO_ALLOW_LOCAL_AUDIO=true" in text
    assert f'DODEAL_CALL_CALLBACK_SECRETS={{"tenant-a": "{SECRET}"}}' in text
    assert "mint_demo_token.py --route calls" in text
    assert "/api/v1/admin/tenant-config/unit_b" in text
    assert "http://127.0.0.1:8765/demo-call.wav" in text
    assert "Host: tenant-a.dodealcrm.com" in text


def test_the_minters_calls_body_is_a_valid_push(monkeypatch) -> None:
    monkeypatch.setenv("DODEAL_CALL_DEMO_ALLOW_LOCAL_AUDIO", "true")
    get_settings.cache_clear()
    args = minter.argparse.Namespace(
        call_id=9001, lead_id=1004, audio_url=minter.DEMO_AUDIO_URL
    )
    body = minter.calls_body(args)
    assert CallJobRequest.model_validate(body).call_id == 9001
    assert "calls" in minter.SERVICE_ROUTES
