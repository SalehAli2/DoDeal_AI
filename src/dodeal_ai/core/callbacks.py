"""Signed callbacks to the CRM (Unit B): the one place an event leaves.

WHERE: the tenant's configured `unit_b.callback_url` and nowhere else -- never a
URL from a push body, which cannot carry one (extra="forbid"). The URL is held
to the download's egress rules: https on 443, every resolved address public,
the request pinned to the address that was checked, and no redirect followed.

WHAT: a JSON body and four headers --

  X-DODEAL-Event       call.stage1 | call.stage2 | call.failed
  X-DODEAL-Timestamp   unix seconds, as a decimal string
  X-DODEAL-Event-Id    one per (job, event), the same on every retry
  X-DODEAL-Signature   hex HMAC-SHA256 of "<timestamp>.<body>" under the
                       tenant's secret in CALL_CALLBACK_SECRETS

The receiver recomputes the signature over the exact bytes it received and
refuses a stale timestamp; the event id makes a retried event safe to see twice.

NEVER UNSIGNED: a tenant with no secret gets no callback at all. The secret is
read in exactly one place, `_signature`. Nothing here logs the URL, the body or
a header value.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from enum import StrEnum

import httpx

from dodeal_ai.core.audio_download import (
    AudioDownloadError,
    Resolver,
    link_parts,
    pinned_request,
    public_address,
)
from dodeal_ai.core.config import get_settings

CALL_STAGE1 = "call.stage1"
# Wave 2's result, sent by the stage-2 task once stage 2 is done.
CALL_STAGE2 = "call.stage2"
CALL_FAILED = "call.failed"
# A done call's translation, asked for by the CRM: sent once, read by GET.
CALL_TRANSLATION = "call.translation"
EVENTS = (CALL_STAGE1, CALL_STAGE2, CALL_FAILED, CALL_TRANSLATION)

# The waits before each retry of a failed delivery, in seconds: a minute, five,
# thirty, two hours. After the last, the delivery has failed.
DELIVERY_DELAYS_SECONDS = (60, 300, 1800, 7200)

EVENT_HEADER = "X-DODEAL-Event"
TIMESTAMP_HEADER = "X-DODEAL-Timestamp"
EVENT_ID_HEADER = "X-DODEAL-Event-Id"
SIGNATURE_HEADER = "X-DODEAL-Signature"

# Fixes every event id to this service's namespace.
_EVENT_NAMESPACE = uuid.UUID("5b1d3c3e-8e5f-4f0e-9a44-6c1f0d2b7a91")


class Delivery(StrEnum):
    """What one attempt came to: sent, worth another try, or never sendable."""

    DELIVERED = "delivered"
    RETRY = "retry"
    REFUSED = "refused"


def event_id(tenant: str, job_id: str, event: str) -> str:
    """The same id for every attempt at one event of one job."""
    return str(uuid.uuid5(_EVENT_NAMESPACE, f"{tenant}/{job_id}/{event}"))


def _signature(tenant: str, timestamp: str, body: bytes) -> str | None:
    """THE one read of a CALL_CALLBACK_SECRETS value: the hex HMAC-SHA256 of
    "<timestamp>.<body>", or None when the tenant has no secret."""
    secret = get_settings().call_callback_secrets.get(tenant)
    if secret is None:
        return None
    message = timestamp.encode("ascii") + b"." + body
    key = secret.get_secret_value().encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


async def post_event(
    url: str,
    *,
    tenant: str,
    event: str,
    event_id: str,
    body: bytes,
    timestamp: str,
    http: httpx.AsyncClient,
    resolve: Resolver,
    allow_local: bool = False,
) -> Delivery:
    """Sign and POST one event to `url`, the tenant's callback URL. REFUSED
    when it can never be sent -- no secret, a bad URL, a private address --
    RETRY for anything the CRM or the network might answer differently later.
    `allow_local` is the demo's flag: http, any port and loopback."""
    signature = _signature(tenant, timestamp, body)
    if signature is None:
        return Delivery.REFUSED
    try:
        scheme, host, port = link_parts(url, allow_local=allow_local)
        if not host:
            return Delivery.REFUSED
        address = await public_address(host, port, resolve, allow_loopback=allow_local)
    except AudioDownloadError as refused:
        return Delivery.RETRY if refused.retryable else Delivery.REFUSED
    pinned, pinned_headers, extensions = pinned_request(
        url, scheme, host, port, address
    )
    headers = {
        **pinned_headers,
        "Content-Type": "application/json",
        EVENT_HEADER: event,
        TIMESTAMP_HEADER: timestamp,
        EVENT_ID_HEADER: event_id,
        SIGNATURE_HEADER: signature,
    }
    try:
        response = await http.post(
            pinned,
            content=body,
            headers=headers,
            extensions=extensions,
            follow_redirects=False,
            timeout=get_settings().external_call_timeout_seconds,
        )
    except httpx.HTTPError:
        return Delivery.RETRY
    return Delivery.DELIVERED if response.is_success else Delivery.RETRY
