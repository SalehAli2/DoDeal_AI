"""One adapter for every provider that speaks OpenAI's /chat/completions.

Groq and OpenAI differ by BASE URL and nothing else that matters here: the
request body, the auth header, the response shape and the finish-reason
vocabulary are identical. So there is one class parameterised by base URL rather
than one class per vendor (report R16) -- a second vendor class would have been
a copy whose two halves drift, and the conformance tests run the same
assertions against both URLs precisely to keep that true.

Gemini is NOT this shape; it would need a class of its own, and has none.

WHAT THIS MODULE PROMISES, and what each promise is worth:

  - JSON mode is MANDATORY, not a hint. `response_format={"type":"json_object"}`
    is on every request because Unit A's only use of a reply is `parse_output`,
    and a fenced or prose-wrapped answer fails validation and spends a reprompt
    -- a second paid call -- to ask for what the first call could have been told
    to produce. A profile may ask for json_schema instead, which sends the
    caller's schema when it gave one. It is still not TRUST: the reply is
    untrusted text until parse_output validates it.
  - THREE OPTIONAL PROFILE FIELDS (register item 147), each sent only when set,
    so a profile that sets none sends exactly the body it always did:
    reasoning_effort (temperature is then left out, and the ceiling goes as
    max_completion_tokens, which counts the reasoning), json_schema, and seed.
    Reasoning text a provider returns beside the answer is never read.
  - REASONING TOKENS OUTSIDE completion_tokens ARE STILL PAID (D-62). Some
    providers report hidden reasoning only as total_tokens above prompt plus
    completion; that gap is priced as output, and is the reasoning count when
    no reasoning_tokens detail is reported. A reported detail is already
    inside completion_tokens and is never added on top (_usage_counts).
  - NO RETRY, ever. A model call is paid and may have completed on the
    provider's side even when we saw a failure. One attempt per complete(); the
    watchdog wraps this with retry=False and a test asserts the transport is
    entered exactly once on every failure path.
  - ONE CIRCUIT BREAKER PER CLIENT (register item 20), on the Redis breaker's
    settings. Only a connect error, a timeout, a 5xx or a 429 counts; an open
    breaker refuses with reason breaker_open before any socket is opened.
  - Exceptions carry the HTTP STATUS and the PROVIDER NAME and nothing else.
    Never the body, never a header, never the provider's message, never the base
    URL -- which can carry a credential when it is a proxy override.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any, Final

import httpx
from pydantic import SecretStr

from dodeal_ai.core.breaker import CircuitBreaker
from dodeal_ai.core.config import ConfigError, LLMProvider, Settings
from dodeal_ai.core.llm.client import (
    FinishReason,
    LLMErrorReason,
    LLMProviderError,
    LLMResponse,
)
from dodeal_ai.core.llm.profiles import ResolvedProfile, resolve_profile
from dodeal_ai.core.prompting import AssembledPrompt

_logger = logging.getLogger("dodeal_ai.llm.openai_compatible")

# Provider base URLs live HERE, not in Settings: they are facts about a vendor's
# API, not a per-deployment choice. Settings.llm_base_url overrides them for a
# proxy, which is the only legitimate reason to point somewhere else.
GROQ_BASE_URL: Final = "https://api.groq.com/openai/v1"
OPENAI_BASE_URL: Final = "https://api.openai.com/v1"

BASE_URLS: Final[dict[LLMProvider, str]] = {
    LLMProvider.GROQ: GROQ_BASE_URL,
    LLMProvider.OPENAI: OPENAI_BASE_URL,
}

# The label that reaches an exception and a log line. Derived from the base URL
# so the URL ITSELF never has to be carried; an override we do not recognise is
# named generically rather than echoed back.
_PROVIDER_NAMES: Final[dict[str, str]] = {
    GROQ_BASE_URL: LLMProvider.GROQ.value,
    OPENAI_BASE_URL: LLMProvider.OPENAI.value,
}
UNKNOWN_PROVIDER_NAME: Final = "openai_compatible"

_COMPLETIONS_PATH: Final = "/chat/completions"

# THE HTTP BUDGET, as a share of the watchdog deadline that wraps this call.
# Strictly less than 1 so httpx's own timer fires FIRST and the failure arrives
# as OpenAICompatibleError with a provider name; at parity the watchdog always
# won -- its clock starts before _resolve(), httpx's read clock only after
# connect and send -- so the branch below was dead and every timeout lost its
# attribution. Too low wastes budget; at 1.0 the attribution is lost again.
_HTTP_TIMEOUT_SHARE: Final = 0.9

# THE TEMPERATURE BOUND for this API family. OpenAI and Groq both accept 0-2,
# where Anthropic accepts 0-1 -- so the bound belongs to the adapter and not to
# the shared ModelProfile field. Enforced at resolution, before any paid call.
TEMPERATURE_MIN: Final = 0.0
TEMPERATURE_MAX: Final = 2.0

# The provider's finish_reason vocabulary, mapped onto the seam's three values.
# Anything absent from this table is OTHER by design: an unknown reason is not an
# error, and FinishReason.OTHER is what the seam says to do with it.
_FINISH_REASONS: Final[dict[str, FinishReason]] = {
    "stop": FinishReason.STOP,
    "length": FinishReason.MAX_TOKENS,
}

# Both statuses mean the same thing to us -- the key is not good for this call --
# and the difference between them is the provider's to explain, not ours to act
# on. 403 is also what a key with the wrong scope returns.
_AUTH_STATUSES: Final = frozenset({401, 403})

# Register item 26: the provider's request id, from its response header first.
# Only a short token of safe characters is kept, so it can go on a log line.
_REQUEST_ID_HEADERS: Final = ("x-request-id", "request-id")
_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")


class OpenAICompatibleError(LLMProviderError):
    """A provider failure that also remembers the two facts that are safe to
    keep: the HTTP status and which provider produced it.

    str() is still the seam's fixed `llm_provider_error:<reason>` -- the whole
    point of the base class -- so this can reach a log line unchanged. The body,
    the headers and the provider's own message are never captured at all, rather
    than captured and carefully not printed.
    """

    def __init__(
        self,
        reason: LLMErrorReason,
        *,
        transient: bool,
        status: int | None,
        provider: str,
        trips_breaker: bool = False,
        fallback_eligible: bool = False,
    ) -> None:
        super().__init__(reason, transient=transient)
        self.status = status
        self.provider = provider
        # True for the four failures the breaker counts (register item 20).
        self.trips_breaker = trips_breaker
        # True when no response body came back: connect error, 429, 503 or an
        # open breaker. The only failures a fallback may retry (register item 21).
        self.fallback_eligible = fallback_eligible


def _trips_breaker(exc: BaseException) -> bool:
    return isinstance(exc, OpenAICompatibleError) and exc.trips_breaker


def _never_reached_provider(exc: BaseException) -> bool:
    return (
        isinstance(exc, OpenAICompatibleError)
        and exc.reason is LLMErrorReason.PROVIDER_POOL_EXHAUSTED
    )


def provider_name_for(base_url: str) -> str:
    """The safe label for `base_url`. An unrecognised override -- a proxy, which
    may carry a credential in its host or path -- is named generically."""
    return _PROVIDER_NAMES.get(base_url.rstrip("/"), UNKNOWN_PROVIDER_NAME)


class OpenAICompatibleClient:
    """Structurally satisfies LLMClient; a test asserts it at runtime and for
    mypy. One HTTP call per complete(), no retry, no state between calls.

    `http` is INJECTED rather than built here: the pooled AsyncClient is owned by
    lifespan (item 84), and a per-call client would open a fresh TLS connection
    for every judgement. `settings` is injected for the same reason the profile
    table is resolved here and not at the call site -- it is what resolution and
    the per-call timeout both read, and reading it from ambient state would make
    this class untestable without an environment.

    `model` is the pinned default the factory read from Settings. The profile
    table decides the model per task and is authoritative (R17); on the fallback
    path resolve_profile returns this same value, so the two agree.

    A REGISTRY PROVIDER (core/llm/routing.py) passes `serves`, its registry
    name, which is then also its label and the provider a profile must name,
    and its own `timeout_seconds`. Left out, the client serves the
    DODEAL_LLM_* pair exactly as before.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: SecretStr,
        http: httpx.AsyncClient,
        *,
        settings: Settings,
        serves: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._http = http
        self._settings = settings
        self._serves: LLMProvider | str | None = (
            settings.llm_provider if serves is None else serves
        )
        self._timeout_seconds = (
            settings.llm_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        self._provider = provider_name_for(base_url) if serves is None else serves
        self._breaker = CircuitBreaker(
            f"llm.{self._provider}",
            failure_threshold=settings.breaker_failure_threshold,
            open_seconds=settings.breaker_open_seconds,
            counts=_trips_breaker,
            uncounted=_never_reached_provider,
            refusal=self._refused,
        )

    @property
    def breaker(self) -> CircuitBreaker:
        """This client's breaker, for readiness and metrics to read."""
        return self._breaker

    @property
    def provider(self) -> str:
        """The safe label every error, log line and response carries."""
        return self._provider

    # --- the one method on the seam ----------------------------------------

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        profile: str,
        max_output_tokens: int | None = None,
        response_schema: Mapping[str, object] | None = None,
    ) -> LLMResponse:
        resolved = self._resolve(profile)
        task_ceiling = (
            self._settings.llm_max_output_tokens
            if max_output_tokens is None
            else max_output_tokens
        )
        payload = self._body(prompt, resolved, task_ceiling)
        payload["response_format"] = _response_format(
            profile, resolved, response_schema
        )
        if resolved.seed is not None:
            payload["seed"] = resolved.seed
        response = await self._breaker.call(lambda: self._post(payload))
        return self._parse(response)

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        """The one HTTP call, its failures translated. A malformed 200 is parsed
        outside, so an answering provider never counts against its breaker."""
        try:
            response = await self._http.post(
                f"{self._base_url}{_COMPLETIONS_PATH}",
                json=payload,
                headers=self._headers(),
                timeout=self._timeout(),
            )
        except httpx.PoolTimeout:
            # Register items 112 and 16: no free pooled connection. Nothing was
            # sent, so it is named for the pool and never read as a timeout.
            raise self._error(
                LLMErrorReason.PROVIDER_POOL_EXHAUSTED, transient=True, status=None
            ) from None
        except httpx.TimeoutException:
            # Catch-list 55: a timeout leaves here as the seam's transient
            # exception, NEVER as a builtin TimeoutError -- which asyncio.wait_for
            # also raises, and which the watchdog could then not tell apart from
            # its own deadline.
            raise self._error(
                LLMErrorReason.UNAVAILABLE,
                transient=True,
                status=None,
                trips_breaker=True,
            ) from None
        except httpx.ConnectError:
            raise self._error(
                LLMErrorReason.UNAVAILABLE,
                transient=True,
                status=None,
                trips_breaker=True,
                fallback_eligible=True,
            ) from None
        except httpx.HTTPError:
            # Connect/read/protocol failures. `from None` on purpose: the
            # original carries the request URL, which is a credential when the
            # base URL is a proxy override.
            raise self._error(
                LLMErrorReason.UNAVAILABLE, transient=True, status=None
            ) from None

        if response.status_code != httpx.codes.OK:
            raise self._status_error(response.status_code)
        return response

    def _refused(self, name: str) -> OpenAICompatibleError:
        """What an open breaker raises: transient, no status, nothing sent."""
        return self._error(
            LLMErrorReason.BREAKER_OPEN,
            transient=True,
            status=None,
            fallback_eligible=True,
        )

    def _timeout(self) -> httpx.Timeout:
        """The HTTP budget on connect, write and read; the pool wait is the
        acquire setting, and never longer than that budget."""
        budget = self._http_timeout_seconds()
        pool = min(self._settings.llm_pool_acquire_timeout_seconds, budget)
        return httpx.Timeout(budget, pool=pool)

    def _http_timeout_seconds(self) -> float:
        """The HTTP budget: inside the watchdog deadline, never equal to it.

        `llm_timeout_seconds` stays the true outer bound -- the watchdog still
        enforces it -- and the HTTP call is given a strict fraction of it so it
        gives up first and we learn WHICH provider was slow. A registry
        provider's own timeout is never above it (routing.py).
        """
        return self._timeout_seconds * _HTTP_TIMEOUT_SHARE

    # --- resolution ---------------------------------------------------------

    def validate_profile(self, profile: str) -> None:
        """Run resolution's guards for `profile` and throw the answer away.

        The startup sweep (main.py, item 84) so a bad temperature or a
        cross-vendor profile refuses to START, rather than 503ing the first
        judgement that happens to name it -- which could be days later and on
        one task only. Same code path as a real call, so the sweep cannot
        disagree with what a call would do.
        """
        self._resolve(profile)

    def _resolve(self, profile: str) -> ResolvedProfile:
        """The profile table's answer, checked against what THIS API accepts.

        Both checks are here rather than on ModelProfile because both are
        per-provider facts, and both run before the request is built, so a bad
        profile costs nothing.
        """
        resolved = resolve_profile(self._settings, profile)
        if resolved.provider != self._serves:
            # A profile naming another vendor -- anthropic, or even groq on an
            # openai client (register item 77) -- would post that vendor's model
            # id to this vendor's URL. Routing per profile is the gateway's job
            # (item 84), not this class's; the startup sweep inherits this.
            raise ConfigError(f"llm_profile_provider_mismatch:{self._provider}")
        if not TEMPERATURE_MIN <= resolved.temperature <= TEMPERATURE_MAX:
            # The value is deliberately not interpolated: the rule is to name
            # what was wrong, never to echo a rejected value into a log.
            raise ConfigError(f"llm_temperature_out_of_range:{self._provider}")
        return resolved

    # --- request ------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """THE ONLY place the API key is read. get_secret_value() appears once in
        the service, here, so grepping it finds every use."""
        return {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}

    def _body(
        self, prompt: AssembledPrompt, resolved: ResolvedProfile, task_ceiling: int
    ) -> dict[str, Any]:
        """One user message carrying `prompt.text` -- stable, then variable, then
        tail, in that order and unchanged. The prompt is NOT re-split into a
        system/user pair: the injection boundary is tested in core/prompting.py,
        and moving it here would move it out of the module that guards it.
        """
        body: dict[str, Any] = {
            "model": resolved.model,
            "messages": [{"role": "user", "content": prompt.text}],
        }
        # The ceiling rule: a profile may lower the task's number, never
        # raise it (ResolvedProfile.effective_max_output_tokens).
        ceiling = resolved.effective_max_output_tokens(task_ceiling)
        if resolved.reasoning_effort is None:
            body["temperature"] = resolved.temperature
            body["max_tokens"] = ceiling
        else:
            # A reasoning model refuses a temperature, and counts its hidden
            # reasoning inside max_completion_tokens (register item 147).
            body["reasoning_effort"] = resolved.reasoning_effort
            body["max_completion_tokens"] = ceiling
        return body

    # --- response -----------------------------------------------------------

    def _parse(self, response: httpx.Response) -> LLMResponse:
        """Map a 200 onto LLMResponse, or fail transiently.

        A 200 whose body we cannot read is transient rather than invalid_request:
        the request was accepted, so what came back is a provider or gateway
        problem, and "try again later" is the only thing a caller can do about it
        either way.
        """
        try:
            body = response.json()
            choice = body["choices"][0]
            text = choice["message"]["content"]
            usage = body["usage"]
            if not isinstance(text, str):
                # A null content is what a tool call or a refusal looks like on
                # this API. Nothing to validate, so it is not a reply.
                raise TypeError("content_not_text")
            prompt, output, reasoning = _usage_counts(usage)
            return LLMResponse(
                text=text,
                input_tokens=prompt,
                output_tokens=output,
                # What the provider says RAN, not what we asked for: it becomes
                # model_version on the stored judgement.
                model=str(body["model"]),
                finish_reason=_FINISH_REASONS.get(
                    choice["finish_reason"], FinishReason.OTHER
                ),
                provider_request_id=_request_id(response, body),
                cached_input_tokens=_detail(
                    usage, "prompt_tokens_details", "cached_tokens"
                ),
                reasoning_tokens=reasoning,
                provider=self._provider,
            )
        except (KeyError, IndexError, TypeError, ValueError):
            # ValueError covers json.JSONDecodeError (non-JSON) and a token count
            # that is not a number. `from None` keeps the decoder's message --
            # which quotes the body -- out of any traceback.
            raise self._error(
                LLMErrorReason.UNKNOWN,
                transient=True,
                status=response.status_code,
            ) from None

    # --- failures -----------------------------------------------------------

    def _status_error(self, status: int) -> OpenAICompatibleError:
        if status in _AUTH_STATUSES:
            return self._error(LLMErrorReason.AUTH, transient=False, status=status)
        if status == httpx.codes.TOO_MANY_REQUESTS:
            return self._error(
                LLMErrorReason.RATE_LIMITED,
                transient=True,
                status=status,
                trips_breaker=True,
                fallback_eligible=True,
            )
        if status >= httpx.codes.INTERNAL_SERVER_ERROR:
            return self._error(
                LLMErrorReason.UNAVAILABLE,
                transient=True,
                status=status,
                trips_breaker=True,
                fallback_eligible=status == httpx.codes.SERVICE_UNAVAILABLE,
            )
        if status == httpx.codes.BAD_REQUEST:
            return self._error(
                LLMErrorReason.INVALID_REQUEST, transient=False, status=status
            )
        # Any other 4xx (404 on a wrong base URL, 413, 422). Not transient:
        # repeating a request the provider rejected spends money to be told the
        # same thing.
        return self._error(LLMErrorReason.UNKNOWN, transient=False, status=status)

    def _error(
        self,
        reason: LLMErrorReason,
        *,
        transient: bool,
        status: int | None,
        trips_breaker: bool = False,
        fallback_eligible: bool = False,
    ) -> OpenAICompatibleError:
        """Build the exception AND log the failure in one place, so the two can
        never disagree about what a call did. Four content-free fields."""
        _logger.warning(
            "llm_provider_call_failed",
            extra={
                "reason_code": reason.value,
                "provider": self._provider,
                "status": status,
                "transient": transient,
            },
        )
        return OpenAICompatibleError(
            reason,
            transient=transient,
            status=status,
            provider=self._provider,
            trips_breaker=trips_breaker,
            fallback_eligible=fallback_eligible,
        )


# A json_schema answer's name, as the API allows it: the profile's name with
# anything outside [A-Za-z0-9_-] replaced, at most 64 characters.
_SCHEMA_NAME_REFUSED: Final = re.compile(r"[^A-Za-z0-9_-]")
_SCHEMA_NAME_CHARS: Final = 64


def _response_format(
    profile: str,
    resolved: ResolvedProfile,
    schema: Mapping[str, object] | None,
) -> dict[str, object]:
    """JSON object mode, mandatory -- or the caller's schema, when the profile
    asks for json_schema and the caller gave one. Not strict: the schema
    guides the model, and parse_output is still what decides."""
    if resolved.response_format != "json_schema" or schema is None:
        return {"type": "json_object"}
    name = _SCHEMA_NAME_REFUSED.sub("_", profile)[:_SCHEMA_NAME_CHARS]
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "schema": dict(schema), "strict": False},
    }


def _count(value: object) -> int | None:
    """A whole non-negative token count, or None for anything else."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _reported(usage: dict[str, Any], section: str, name: str) -> int | None:
    """A count from usage's optional details, or None when it is missing,
    null, or not a whole non-negative number."""
    details = usage.get(section)
    return _count(details.get(name) if isinstance(details, dict) else None)


def _detail(usage: dict[str, Any], section: str, name: str) -> int:
    """A count from usage's optional details, or 0: an optional detail never
    turns an answered call into a failure."""
    return _reported(usage, section, name) or 0


def _usage_counts(usage: dict[str, Any]) -> tuple[int, int, int]:
    """(input, priced output, reasoning) from a usage block. The gap is
    total_tokens above prompt plus completion, never below 0, and 0 when total
    is missing or not a count. Output is completion plus the gap only;
    reasoning is the reported detail when there is one, else the gap."""
    prompt = int(usage["prompt_tokens"])
    completion = int(usage["completion_tokens"])
    total = _count(usage.get("total_tokens"))
    gap = 0 if total is None else max(0, total - prompt - completion)
    reported = _reported(usage, "completion_tokens_details", "reasoning_tokens")
    return prompt, completion + gap, gap if reported is None else reported


def _request_id(response: httpx.Response, body: dict[str, Any]) -> str | None:
    """The provider's request id: x-request-id, then request-id, then the body's
    id, the first that is 1-128 of [A-Za-z0-9_-]. Anything else is None rather
    than a failure: it is a reconciliation handle, not part of the answer."""
    candidates = [response.headers.get(name) for name in _REQUEST_ID_HEADERS]
    for value in (*candidates, body.get("id")):
        if isinstance(value, str) and _REQUEST_ID.fullmatch(value):
            return value
    return None
