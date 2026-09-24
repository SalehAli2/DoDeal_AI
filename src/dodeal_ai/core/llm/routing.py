"""Model routing: a registry of providers, profiles that name one, and routes
that send each pass to a profile.

THE REGISTRY (DODEAL_LLM_PROVIDERS) names every provider besides the
DODEAL_LLM_* pair: its kind (openai_compatible only), base URL, the NAME of the
environment variable that holds its key, and its timeout. The key is read from
that variable in `_provider_key` and nowhere else; the JSON never holds one.

THE DEFAULT ROUTE is today's seam: the client build_llm_client makes from
DODEAL_LLM_*, fallback included. A pass whose own-name profile names a registry
provider goes to that provider; every other pass is sent exactly as before. With
no registry, no route and no fallback_profile there is nothing to route, and
build_router hands back that client itself -- the same object as before.

A NAMED ROUTE (DODEAL_MODEL_ROUTES) maps pass names to profile names; a pass it
leaves out is served as on the default route. A tenant names its route in its
config (unit_a / unit_b `model_route`); a route this process does not know
fails the call closed, never onto another provider.

FALLBACK, ONE HOP. A profile may name a fallback_profile, tried once when its
call got no response body at all (connect error, 429, 503, breaker open) --
core/llm/fallback.py's narrow rule. A received answer is never paid for twice.

ONE POOLED CLIENT PER PROVIDER, built at startup: the pair on the caller's
pool, each registry provider on a pool of its own that the router closes.
Every name is checked before a call: Settings refuses a profile naming an
unknown provider or a route naming an unknown profile, and the build refuses a
route naming an unknown pass or an empty key variable -- naming the route or
the provider, never a value.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from typing import Final

import httpx
from pydantic import SecretStr

from dodeal_ai.core.config import (
    ConfigError,
    LLMProvider,
    ModelProfile,
    ProviderSpec,
    Settings,
)
from dodeal_ai.core.llm.client import (
    LLMClient,
    LLMErrorReason,
    LLMProviderError,
    LLMResponse,
)
from dodeal_ai.core.llm.openai_compatible import (
    OpenAICompatibleClient,
    OpenAICompatibleError,
)
from dodeal_ai.core.llm.profiles import KNOWN_PROFILES
from dodeal_ai.core.prompting import AssembledPrompt

_logger = logging.getLogger("dodeal_ai.llm.routing")

# The route built from DODEAL_LLM_*, which every tenant is on by default.
DEFAULT_ROUTE: Final = "default"


class RoutedClient:
    """Structurally an LLMClient: one route's view of the providers."""

    def __init__(
        self,
        name: str,
        passes: Mapping[str, str],
        *,
        settings: Settings,
        default: LLMClient,
        providers: Mapping[str, OpenAICompatibleClient],
    ) -> None:
        self._name = name
        self._passes = dict(passes)
        self._settings = settings
        self._default = default
        self._providers = providers

    def profile_for(self, name: str) -> ModelProfile | None:
        """The profile pass `name` is sent under on this route; None when it
        resolves to the DODEAL_LLM_* pair."""
        return self._settings.llm_profiles.get(self._passes.get(name, name))

    def _client_for(self, profile: str) -> LLMClient:
        """A registry profile's provider client, else the pair's client."""
        found = self._settings.llm_profiles.get(profile)
        if found is None or isinstance(found.provider, LLMProvider):
            return self._default
        return self._providers[found.provider]

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        profile: str,
        max_output_tokens: int | None = None,
        response_schema: Mapping[str, object] | None = None,
    ) -> LLMResponse:
        name = self._passes.get(profile, profile)
        try:
            return await self._client_for(name).complete(
                prompt,
                profile=name,
                max_output_tokens=max_output_tokens,
                response_schema=response_schema,
            )
        except OpenAICompatibleError as exc:
            found = self._settings.llm_profiles.get(name)
            fallback = None if found is None else found.fallback_profile
            if fallback is None or not exc.fallback_eligible:
                raise
            # Names from config and the reason's fixed code: never a message.
            _logger.warning(
                "llm_fallback_used",
                extra={
                    "reason_code": "llm_fallback_used",
                    "primary_reason": exc.reason.value,
                    "route": self._name,
                    "profile": name,
                },
            )
        return await self._client_for(fallback).complete(
            prompt,
            profile=fallback,
            max_output_tokens=max_output_tokens,
            response_schema=response_schema,
        )


class _Unrouted:
    """A route this process does not know: every call fails closed, and nothing
    is sent anywhere -- a tenant moved off an API must never reach it again
    because a deploy dropped its route."""

    def __init__(self, name: str) -> None:
        self._name = name

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        profile: str,
        max_output_tokens: int | None = None,
        response_schema: Mapping[str, object] | None = None,
    ) -> LLMResponse:
        _logger.warning(
            "model_route_not_configured",
            extra={"reason_code": "model_route_not_configured", "route": self._name},
        )
        raise LLMProviderError(LLMErrorReason.ROUTE_NOT_CONFIGURED, transient=False)


class ModelRouter:
    """Structurally an LLMClient on the default route; `for_route` for a
    tenant's own. Owns the registry providers' pools."""

    def __init__(
        self,
        routes: Mapping[str, RoutedClient],
        providers: Mapping[str, OpenAICompatibleClient],
        pools: Sequence[httpx.AsyncClient],
    ) -> None:
        self._routes = dict(routes)
        self._providers = dict(providers)
        self._pools = tuple(pools)

    @property
    def providers(self) -> Mapping[str, OpenAICompatibleClient]:
        """Each registry provider's client, by name, for readiness and tests."""
        return self._providers

    def for_route(self, route: str) -> LLMClient:
        """The client `route` sends through; one that fails closed when this
        process has no such route."""
        return self._routes.get(route) or _Unrouted(route)

    def profile_for(self, name: str) -> ModelProfile | None:
        return self._routes[DEFAULT_ROUTE].profile_for(name)

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        profile: str,
        max_output_tokens: int | None = None,
        response_schema: Mapping[str, object] | None = None,
    ) -> LLMResponse:
        return await self._routes[DEFAULT_ROUTE].complete(
            prompt,
            profile=profile,
            max_output_tokens=max_output_tokens,
            response_schema=response_schema,
        )

    async def aclose(self) -> None:
        """Close every registry provider's pool; the pair's pool is the caller's."""
        for pool in self._pools:
            await pool.aclose()


def routed(client: LLMClient, route: str) -> LLMClient:
    """The client a tenant on `route` calls through. A client that is not a
    router has only the default route: it is returned as it is for "default",
    and fails closed for any other name."""
    if isinstance(client, ModelRouter):
        return client.for_route(route)
    return client if route == DEFAULT_ROUTE else _Unrouted(route)


def route_names(settings: Settings) -> frozenset[str]:
    """Every route a tenant may name: "default" and each configured one."""
    return frozenset({DEFAULT_ROUTE, *settings.model_routes})


def _needs_router(settings: Settings) -> bool:
    return bool(
        settings.llm_providers
        or settings.model_routes
        or any(p.fallback_profile for p in settings.llm_profiles.values())
    )


def _provider_key(name: str, spec: ProviderSpec) -> SecretStr:
    """THE one read of a registry provider's key: the variable its spec names.
    Empty or unset refuses startup, naming the provider and never the value."""
    value = os.environ.get(spec.api_key_env, "")
    if not value:
        raise ConfigError(f"llm_provider_key_missing:{name}")
    return SecretStr(value)


def _check_passes(settings: Settings) -> None:
    """Every route names only passes this service has. The other names are
    Settings' own check (config._routing_names_resolve)."""
    for route, passes in settings.model_routes.items():
        if not set(passes) <= set(KNOWN_PROFILES):
            raise ConfigError(f"model_route_unknown_pass:{route}")


def new_pool(limits: httpx.Limits | None) -> httpx.AsyncClient:
    """A registry provider's own connection pool; opens no socket."""
    return httpx.AsyncClient() if limits is None else httpx.AsyncClient(limits=limits)


def _registry_client(
    name: str, spec: ProviderSpec, settings: Settings, limits: httpx.Limits | None
) -> tuple[OpenAICompatibleClient, httpx.AsyncClient]:
    """One registry provider's client on a pool of its own. Opens no socket."""
    key = _provider_key(name, spec)
    if spec.base_url.startswith("http://"):
        # Not a refusal: the owner's server may have no certificate. ERROR,
        # because the key and every prompt then cross the wire in clear.
        logging.getLogger("dodeal_ai.startup").error(
            "llm_provider_insecure",
            extra={"event": "llm_provider_insecure", "provider": name},
        )
    pool = new_pool(limits)
    client = OpenAICompatibleClient(
        spec.base_url,
        "",
        key,
        pool,
        settings=settings,
        serves=name,
        timeout_seconds=spec.timeout_seconds,
    )
    return client, pool


async def build_router(
    settings: Settings,
    default: LLMClient,
    *,
    limits: httpx.Limits | None = None,
) -> LLMClient:
    """The client every pass is sent through: `default` itself when nothing is
    routed, else a ModelRouter over it and one client per registry provider.
    A ConfigError refuses startup, with every pool this built closed."""
    if not _needs_router(settings):
        return default
    _check_passes(settings)
    providers: dict[str, OpenAICompatibleClient] = {}
    pools: list[httpx.AsyncClient] = []
    try:
        for name, spec in settings.llm_providers.items():
            providers[name], pool = _registry_client(name, spec, settings, limits)
            pools.append(pool)
        # The registry's half of the startup sweep: temperature and provider.
        for name, profile in settings.llm_profiles.items():
            if not isinstance(profile.provider, LLMProvider):
                providers[profile.provider].validate_profile(name)
    except BaseException:
        for pool in pools:
            await pool.aclose()
        raise
    routes = {
        name: RoutedClient(
            name, passes, settings=settings, default=default, providers=providers
        )
        for name, passes in {DEFAULT_ROUTE: {}, **settings.model_routes}.items()
    }
    return ModelRouter(routes, providers, pools)
