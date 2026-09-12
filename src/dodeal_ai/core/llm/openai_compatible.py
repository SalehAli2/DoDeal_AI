"""One adapter for every provider that speaks OpenAI's /chat/completions.

Groq and OpenAI differ by BASE URL and nothing else that matters here: the
request body, the auth header, the response shape and the finish-reason
vocabulary are identical. So there is one class parameterised by base URL rather
than one class per vendor (report R16) -- a second vendor class would have been
a copy whose two halves drift, and the conformance tests run the same
assertions against both URLs precisely to keep that true.

Gemini is NOT this shape and gets its own class (item 76.3).

WHAT THIS MODULE PROMISES, and what each promise is worth:

  - JSON mode is MANDATORY, not a hint. `response_format={"type":"json_object"}`
    is on every request because Unit A's only use of a reply is `parse_output`,
    and a fenced or prose-wrapped answer fails validation and spends a reprompt
    -- a second paid call -- to ask for what the first call could have been told
    to produce. It is still not TRUST: the reply is untrusted text until
    parse_output validates it.
  - NO RETRY, ever. A model call is paid and may have completed on the
    provider's side even when we saw a failure. One attempt per complete(); the
    watchdog wraps this with retry=False and a test asserts the transport is
    entered exactly once on every failure path.
  - Exceptions carry the HTTP STATUS and the PROVIDER NAME and nothing else.
    Never the body, never a header, never the provider's message, never the base
    URL -- which can carry a credential when it is a proxy override.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import httpx
from pydantic import SecretStr

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
    ) -> None:
        super().__init__(reason, transient=transient)
        self.status = status
        self.provider = provider


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
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: SecretStr,
        http: httpx.AsyncClient,
        *,
        settings: Settings,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._http = http
        self._settings = settings
        self._provider = provider_name_for(base_url)

    # --- the one method on the seam ----------------------------------------

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        profile: str,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        resolved = self._resolve(profile)
        task_ceiling = (
            self._settings.llm_max_output_tokens
            if max_output_tokens is None
            else max_output_tokens
        )
        payload = self._body(prompt, resolved, task_ceiling)

        try:
            response = await self._http.post(
                f"{self._base_url}{_COMPLETIONS_PATH}",
                json=payload,
                headers=self._headers(),
                timeout=httpx.Timeout(self._settings.llm_timeout_seconds),
            )
        except httpx.TimeoutException:
            # Catch-list 55: a timeout leaves here as the seam's transient
            # exception, NEVER as a builtin TimeoutError -- which asyncio.wait_for
            # also raises, and which the watchdog could then not tell apart from
            # its own deadline.
            raise self._error(
                LLMErrorReason.UNAVAILABLE, transient=True, status=None
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
        return self._parse(response)

    # --- resolution ---------------------------------------------------------

    def _resolve(self, profile: str) -> ResolvedProfile:
        """The profile table's answer, checked against what THIS API accepts.

        Both checks are here rather than on ModelProfile because both are
        per-provider facts, and both run before the request is built, so a bad
        profile costs nothing.
        """
        resolved = resolve_profile(self._settings, profile)
        if resolved.provider not in BASE_URLS:
            # A profile naming anthropic/gemini would otherwise post that
            # vendor's model id to this vendor's URL. Routing per profile is the
            # gateway's job (item 84), not this class's.
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
        return {
            "model": resolved.model,
            "messages": [{"role": "user", "content": prompt.text}],
            "temperature": resolved.temperature,
            # The ceiling rule: a profile may lower the task's number, never
            # raise it (ResolvedProfile.effective_max_output_tokens).
            "max_tokens": resolved.effective_max_output_tokens(task_ceiling),
            # Mandatory, never conditional -- see the module docstring.
            "response_format": {"type": "json_object"},
        }

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
            return LLMResponse(
                text=text,
                input_tokens=int(usage["prompt_tokens"]),
                output_tokens=int(usage["completion_tokens"]),
                # What the provider says RAN, not what we asked for: it becomes
                # model_version on the stored judgement.
                model=str(body["model"]),
                finish_reason=_FINISH_REASONS.get(
                    choice["finish_reason"], FinishReason.OTHER
                ),
                provider_request_id=_optional_str(body.get("id")),
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
                LLMErrorReason.RATE_LIMITED, transient=True, status=status
            )
        if status >= httpx.codes.INTERNAL_SERVER_ERROR:
            return self._error(
                LLMErrorReason.UNAVAILABLE, transient=True, status=status
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
        self, reason: LLMErrorReason, *, transient: bool, status: int | None
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
            reason, transient=transient, status=status, provider=self._provider
        )


def _optional_str(value: object) -> str | None:
    """The provider's request id when it sent a usable one. Absent, null or a
    non-string is None rather than a failure: it is a reconciliation handle, not
    part of the answer."""
    return value if isinstance(value, str) else None
