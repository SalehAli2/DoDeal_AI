"""The one place a call recording is fetched (Unit B, register item 50).

A signed link from the CRM is a URL we are told to GET from inside our network,
which makes this the service's server-side request forgery surface. So every
rule is here, in order, and every refusal happens BEFORE a byte is kept:

  1. the link has not expired        else audio_link_expired, never retried
  2. https, on the default port      else audio_scheme_refused / audio_port_refused
  3. the host is one of the tenant's audio_hosts, exactly   else audio_host_not_allowed
  4. EVERY address the host resolves to is public -- not private, loopback,
     link-local (the cloud metadata address is one), shared, reserved,
     multicast or unspecified      else audio_address_refused
  5. the request goes to the address just checked, with the name only as the
     Host and the TLS server name, so a second lookup cannot answer
     differently (DNS rebinding)
  6. no redirect is followed         else audio_redirect_refused
  7. the answer is audio/*           else audio_type_refused
  8. at most max_audio_bytes, by the header and then by the count, inside
     CALL_DOWNLOAD_TIMEOUT_SECONDS  else audio_too_large / audio_download_timeout

The bytes go to a temporary file that is deleted in a `finally` however the
caller leaves the block. Nothing here logs; a refusal is an AudioDownloadError
carrying a fixed reason code and whether trying again could help -- never the
URL, its host, an address or a header.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

# A resolver: the host and port in, every address it answers with out.
type Resolver = Callable[[str, int], Awaitable[list[str]]]

_HTTPS_PORT = 443


class AudioDownloadError(Exception):
    """A refused or failed download. str() is the reason code alone; `retryable`
    says whether the same link could succeed later (a timeout, a 5xx)."""

    def __init__(self, reason: str, *, retryable: bool) -> None:
        self.reason = reason
        self.retryable = retryable
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class DownloadedAudio:
    """A recording on local disk for the life of the `with` block only."""

    path: Path
    size_bytes: int
    content_type: str


async def resolve_host(host: str, port: int) -> list[str]:
    """Every address `host` resolves to, through the loop's resolver."""
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )
    return [str(info[4][0]) for info in infos]


def _is_public(address: str) -> bool:
    """Globally routable and not multicast. An IPv4-mapped IPv6 address is
    judged as the IPv4 address it carries."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _refused(reason: str) -> AudioDownloadError:
    return AudioDownloadError(reason, retryable=False)


def _checked_host(url: str, allowed_hosts: frozenset[str]) -> tuple[str, int]:
    """Rules 2 and 3: the host and port of an https link to a listed host."""
    try:
        parts = urlsplit(url)
        port = parts.port or _HTTPS_PORT
    except ValueError:
        raise _refused("audio_url_invalid") from None
    if parts.scheme != "https":
        raise _refused("audio_scheme_refused")
    if port != _HTTPS_PORT:
        raise _refused("audio_port_refused")
    host = (parts.hostname or "").lower()
    if host not in allowed_hosts:
        raise _refused("audio_host_not_allowed")
    return host, port


async def _public_address(host: str, port: int, resolve: Resolver) -> str:
    """Rule 4: the first address, once every address has been checked."""
    try:
        addresses = await resolve(host, port)
    except OSError:
        raise AudioDownloadError("audio_host_unresolved", retryable=True) from None
    if not addresses:
        raise AudioDownloadError("audio_host_unresolved", retryable=True)
    if not all(_is_public(address) for address in addresses):
        raise _refused("audio_address_refused")
    return addresses[0]


def _media_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").split(";")[0].strip().lower()


def _check_response(response: httpx.Response, max_bytes: int) -> None:
    """Rules 6 to 8 on the status line and headers, before the body is read."""
    status = response.status_code
    if response.is_redirect or 300 <= status < 400:
        raise _refused("audio_redirect_refused")
    if status == httpx.codes.TOO_MANY_REQUESTS or status >= 500:
        raise AudioDownloadError("audio_source_unavailable", retryable=True)
    if status != httpx.codes.OK:
        raise _refused("audio_source_refused")
    if not _media_type(response).startswith("audio/"):
        raise _refused("audio_type_refused")
    declared = response.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > max_bytes):
        raise _refused("audio_too_large")


async def _stream_to(response: httpx.Response, path: Path, max_bytes: int) -> int:
    """Write the body to `path`, refusing at the first byte past `max_bytes`.
    Disk writes run off the event loop."""
    written = 0
    handle = await asyncio.to_thread(path.open, "wb")
    try:
        async for chunk in response.aiter_bytes():
            written += len(chunk)
            if written > max_bytes:
                raise _refused("audio_too_large")
            await asyncio.to_thread(handle.write, chunk)
    finally:
        await asyncio.to_thread(handle.close)
    return written


@asynccontextmanager
async def downloaded_audio(
    url: str,
    *,
    expires_at: datetime,
    now: datetime,
    allowed_hosts: frozenset[str],
    max_bytes: int,
    timeout_seconds: float,
    http: httpx.AsyncClient,
    resolve: Resolver = resolve_host,
) -> AsyncIterator[DownloadedAudio]:
    """Fetch `url` under every rule above and yield it on disk; the file is
    deleted when the block exits, however it exits."""
    if expires_at <= now:
        raise _refused("audio_link_expired")
    host, port = _checked_host(url, allowed_hosts)
    address = await _public_address(host, port, resolve)
    pinned = httpx.URL(url).copy_with(host=address)

    # Made only once the headers have passed, so a refusal leaves no file.
    path: Path | None = None
    try:
        try:
            async with asyncio.timeout(timeout_seconds):
                async with http.stream(
                    "GET",
                    pinned,
                    headers={"Host": host},
                    extensions={"sni_hostname": host},
                    follow_redirects=False,
                ) as response:
                    _check_response(response, max_bytes)
                    path = await _temporary_file()
                    size = await _stream_to(response, path, max_bytes)
                    media = _media_type(response)
        except TimeoutError:
            raise AudioDownloadError("audio_download_timeout", retryable=True) from None
        except httpx.HTTPError:
            # from None: an httpx error carries the request URL, a signed link.
            raise AudioDownloadError(
                "audio_source_unavailable", retryable=True
            ) from None
        yield DownloadedAudio(path=path, size_bytes=size, content_type=media)
    finally:
        if path is not None:
            await asyncio.to_thread(path.unlink, missing_ok=True)


async def _temporary_file() -> Path:
    """An empty private file (mode 0600) in the system temporary directory."""
    fd, name = await asyncio.to_thread(tempfile.mkstemp, suffix=".audio")
    os.close(fd)
    return Path(name)
