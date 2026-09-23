"""The call-recording download (Unit B): every refusal happens before a byte is
kept, the request goes to the address that was checked, and the file is gone
when the block ends however it ends."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from dodeal_ai.core import audio_download
from dodeal_ai.core.audio_download import (
    AudioDownloadError,
    downloaded_audio,
    resolve_host,
)

NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
HOST = "audio.tenant-a.example"
URL = f"https://{HOST}/calls/7.wav?sig=SIGNED"
PUBLIC = "93.184.216.34"
AUDIO = b"RIFF" + b"\x00" * 60


def _resolver(*addresses: str) -> Callable:
    async def resolve(host: str, port: int) -> list[str]:
        assert (host, port) == (HOST, 443)
        return list(addresses)

    return resolve


class _Source:
    """A MockTransport that records every request it is sent."""

    def __init__(self, handler: Callable[[httpx.Request], object]) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self._handler(request)
        if asyncio.iscoroutine(answer):
            answer = await answer
        assert isinstance(answer, httpx.Response)
        return answer


def _audio(body: bytes = AUDIO, **headers: str) -> httpx.Response:
    return httpx.Response(
        200, content=body, headers={"content-type": "audio/wav", **headers}
    )


@pytest.fixture
def temp_files(monkeypatch) -> list[str]:
    """Every temporary file the module makes."""
    made: list[str] = []
    real = tempfile.mkstemp

    def recording(*args, **kwargs):
        fd, name = real(*args, **kwargs)
        made.append(name)
        return fd, name

    monkeypatch.setattr(audio_download.tempfile, "mkstemp", recording)
    return made


async def _fetch(
    source: _Source,
    *,
    url: str = URL,
    resolve: Callable | None = None,
    expires_at: datetime = NOW + timedelta(hours=1),
    max_bytes: int = 1024,
    timeout_seconds: float = 5.0,
) -> tuple[bytes, int, str]:
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(source)) as http,
        downloaded_audio(
            url,
            expires_at=expires_at,
            now=NOW,
            allowed_hosts=frozenset({HOST}),
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
            http=http,
            resolve=resolve or _resolver(PUBLIC),
        ) as audio,
    ):
        return audio.path.read_bytes(), audio.size_bytes, audio.content_type


async def _refusal(source: _Source, **kwargs) -> AudioDownloadError:
    with pytest.raises(AudioDownloadError) as caught:
        await _fetch(source, **kwargs)
    assert caught.value.__cause__ is None
    assert "SIGNED" not in str(caught.value) and HOST not in str(caught.value)
    return caught.value


# --- the happy path ------------------------------------------------------------


async def test_a_listed_public_host_is_fetched_from_the_checked_address(
    temp_files: list[str],
) -> None:
    source = _Source(lambda _: _audio(**{"content-type": "audio/wav; codecs=1"}))
    body, size, media = await _fetch(source)

    assert (body, size, media) == (AUDIO, len(AUDIO), "audio/wav")
    (request,) = source.requests
    assert request.url.host == PUBLIC
    assert request.headers["host"] == HOST
    assert request.extensions["sni_hostname"] == HOST
    assert [Path(name).exists() for name in temp_files] == [False]


async def test_the_file_is_deleted_when_the_callers_block_raises(
    temp_files: list[str],
) -> None:
    source = _Source(lambda _: _audio())
    async with httpx.AsyncClient(transport=httpx.MockTransport(source)) as http:
        with pytest.raises(RuntimeError):
            async with downloaded_audio(
                URL,
                expires_at=NOW + timedelta(hours=1),
                now=NOW,
                allowed_hosts=frozenset({HOST}),
                max_bytes=1024,
                timeout_seconds=5.0,
                http=http,
                resolve=_resolver(PUBLIC),
            ):
                assert Path(temp_files[0]).exists()
                raise RuntimeError("the transcriber failed")
    assert not Path(temp_files[0]).exists()


# --- the guard: refused before a byte is kept ---------------------------------


async def test_a_redirect_to_loopback_is_refused_before_a_byte_is_kept(
    temp_files: list[str],
) -> None:
    source = _Source(
        lambda _: httpx.Response(302, headers={"location": "http://127.0.0.1/x"})
    )
    refusal = await _refusal(source)
    assert (refusal.reason, refusal.retryable) == ("audio_redirect_refused", False)
    assert len(source.requests) == 1
    assert temp_files == []


async def test_an_off_list_host_is_refused_before_any_request(
    temp_files: list[str],
) -> None:
    source = _Source(lambda _: _audio())
    refusal = await _refusal(source, url="https://other.example/7.wav")
    assert (refusal.reason, refusal.retryable) == ("audio_host_not_allowed", False)
    assert source.requests == [] and temp_files == []


async def test_an_oversize_declared_length_is_refused_before_a_byte_is_kept(
    temp_files: list[str],
) -> None:
    source = _Source(lambda _: _audio(b"x" * 2048))
    refusal = await _refusal(source, max_bytes=1024)
    assert refusal.reason == "audio_too_large"
    assert temp_files == []


async def test_an_oversize_stream_is_refused_and_its_bytes_deleted(
    temp_files: list[str],
) -> None:
    async def chunks():
        for _ in range(4):
            yield b"x" * 512

    source = _Source(
        lambda _: httpx.Response(
            200, content=chunks(), headers={"content-type": "audio/mpeg"}
        )
    )
    refusal = await _refusal(source, max_bytes=1024)
    assert refusal.reason == "audio_too_large"
    assert [Path(name).exists() for name in temp_files] == [False]


async def test_http_is_refused_before_any_request(temp_files: list[str]) -> None:
    source = _Source(lambda _: _audio())
    refusal = await _refusal(source, url=f"http://{HOST}/7.wav")
    assert (refusal.reason, refusal.retryable) == ("audio_scheme_refused", False)
    assert source.requests == [] and temp_files == []


# --- the other rules ---------------------------------------------------------------


async def test_an_expired_link_fails_and_is_never_retried() -> None:
    source = _Source(lambda _: _audio())
    refusal = await _refusal(source, expires_at=NOW)
    assert (refusal.reason, refusal.retryable) == ("audio_link_expired", False)
    assert source.requests == []


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.9",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fd00:ec2::254",
        "fe80::1",
        "::ffff:127.0.0.1",
    ],
)
async def test_a_host_resolving_to_a_non_public_address_is_refused(
    address: str,
) -> None:
    source = _Source(lambda _: _audio())
    refusal = await _refusal(source, resolve=_resolver(address))
    assert (refusal.reason, refusal.retryable) == ("audio_address_refused", False)
    assert source.requests == []


async def test_one_private_answer_among_public_ones_is_refused() -> None:
    source = _Source(lambda _: _audio())
    refusal = await _refusal(source, resolve=_resolver(PUBLIC, "10.0.0.5"))
    assert refusal.reason == "audio_address_refused"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        (f"https://{HOST}:8443/7.wav", "audio_port_refused"),
        (f"https://{HOST}:notaport/7.wav", "audio_url_invalid"),
    ],
)
async def test_a_port_other_than_443_is_refused(url: str, reason: str) -> None:
    refusal = await _refusal(_Source(lambda _: _audio()), url=url)
    assert refusal.reason == reason


async def test_a_host_that_does_not_resolve_is_retryable() -> None:
    async def failing(host: str, port: int) -> list[str]:
        raise OSError("dns down")

    for resolve in (failing, _resolver()):
        refusal = await _refusal(_Source(lambda _: _audio()), resolve=resolve)
        assert (refusal.reason, refusal.retryable) == ("audio_host_unresolved", True)


@pytest.mark.parametrize(
    ("response", "reason", "retryable"),
    [
        (httpx.Response(503), "audio_source_unavailable", True),
        (httpx.Response(429), "audio_source_unavailable", True),
        (httpx.Response(404), "audio_source_refused", False),
        (httpx.Response(403), "audio_source_refused", False),
        (
            httpx.Response(
                200, content=b"<html>", headers={"content-type": "text/html"}
            ),
            "audio_type_refused",
            False,
        ),
        (httpx.Response(200, content=b"x"), "audio_type_refused", False),
    ],
)
async def test_the_source_answer_decides_retry(
    response: httpx.Response, reason: str, retryable: bool, temp_files: list[str]
) -> None:
    refusal = await _refusal(_Source(lambda _: response))
    assert (refusal.reason, refusal.retryable) == (reason, retryable)
    assert temp_files == []


async def test_a_garbled_length_is_refused() -> None:
    def garbled(_: httpx.Request) -> httpx.Response:
        response = _audio()
        response.headers["content-length"] = "lots"
        return response

    refusal = await _refusal(_Source(garbled))
    assert refusal.reason == "audio_too_large"


async def test_a_stalled_source_times_out_and_is_retryable() -> None:
    async def stall(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return _audio()

    refusal = await _refusal(_Source(stall), timeout_seconds=0.05)
    assert (refusal.reason, refusal.retryable) == ("audio_download_timeout", True)


async def test_a_transport_failure_is_retryable_and_carries_no_url() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    refusal = await _refusal(_Source(down))
    assert (refusal.reason, refusal.retryable) == ("audio_source_unavailable", True)


async def test_the_real_resolver_answers_for_localhost() -> None:
    """The loop's own resolver, on a name every machine resolves locally."""
    addresses = await resolve_host("localhost", 443)
    assert addresses and all(isinstance(address, str) for address in addresses)
