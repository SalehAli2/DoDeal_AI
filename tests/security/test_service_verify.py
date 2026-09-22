"""The CRM's service token (register item D1): what it must carry, and every
distinct refusal. Keys are generated in-process; none is real."""

from __future__ import annotations

import time

import jwt
import pytest

from dodeal_ai.core.auth.claims import Identity
from dodeal_ai.core.auth.service import (
    SERVICE_SUBJECT,
    ServiceTokenVerifier,
    service_key_text,
)
from dodeal_ai.core.auth.verify import AuthError
from dodeal_ai.core.config import ConfigError, Settings, _build_settings
from tests.helpers import tokens


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "jwt_signing_key": tokens.TEST_SECRET,
        "service_jwt_algorithm": "HS256",
        "service_jwt_signing_key": tokens.SERVICE_TEST_SECRET,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def _reason(token: str, settings: Settings | None = None) -> str:
    with pytest.raises(AuthError) as raised:
        ServiceTokenVerifier(settings or _settings()).verify(token)
    return raised.value.reason_code


def test_a_valid_service_token_yields_the_tenant_and_the_service_subject() -> None:
    """A token carrying iss, aud, subdomain, iat and exp verifies."""
    identity = ServiceTokenVerifier(_settings()).verify(tokens.mint_service_token())
    assert identity == Identity(tenant="tenant-a", subject=SERVICE_SUBJECT, database="")


def test_the_tenant_claim_is_normalised_like_a_user_tokens() -> None:
    """The subdomain is lowercased and validated as one DNS label."""
    token = tokens.mint_service_token(subdomain="Tenant-B")
    assert ServiceTokenVerifier(_settings()).verify(token).tenant == "tenant-b"


def test_a_database_claim_is_carried_when_it_is_a_string() -> None:
    """The optional database claim rides along; a non-string one is dropped."""
    verifier = ServiceTokenVerifier(_settings())
    assert verifier.verify(tokens.mint_service_token(database="crm_a")).database == (
        "crm_a"
    )
    assert verifier.verify(tokens.mint_service_token(database=7)).database == ""


def test_an_integer_sub_on_a_service_token_does_not_refuse_it() -> None:
    """No subject is read, so PyJWT's string-only sub rule stays off here too."""
    token = tokens.mint_service_token(sub=9)
    assert ServiceTokenVerifier(_settings()).verify(token).subject == SERVICE_SUBJECT


# --- the five refusals the guard names, each with its own code --------------


def test_the_five_named_refusals_have_distinct_reason_codes() -> None:
    """Wrong aud, wrong iss, lifetime 301, alg none and a user token all differ."""
    codes = [
        _reason(tokens.mint_service_token(aud="someone-else")),
        _reason(tokens.mint_service_token(iss="someone-else")),
        _reason(tokens.mint_service_token(ttl_seconds=301)),
        _reason(tokens.mint_alg_none_token()),
        _reason(tokens.mint_token()),
    ]
    assert codes == [
        "invalid_audience",
        "invalid_issuer",
        "lifetime_exceeded",
        "alg_none",
        "user_token",
    ]


def test_a_lifetime_of_exactly_the_maximum_is_accepted() -> None:
    """300 seconds is inside the default; only above it is refused."""
    token = tokens.mint_service_token(ttl_seconds=300)
    assert ServiceTokenVerifier(_settings()).verify(token).tenant == "tenant-a"


def test_the_lifetime_is_measured_from_iat_not_from_now() -> None:
    """An old iat with a far exp is refused even while exp is in the future."""
    now = int(time.time())
    token = tokens.mint_service_token(iat=now - 10, exp=now + 295)
    assert _reason(token) == "lifetime_exceeded"


def test_a_user_token_signed_with_the_service_secret_is_still_refused() -> None:
    """The subject-without-issuer shape is refused before any key is tried."""
    token = tokens.mint_token(secret=tokens.SERVICE_TEST_SECRET)
    assert _reason(token) == "user_token"


# --- rotation ---------------------------------------------------------------


def test_the_previous_key_is_accepted_during_a_rotation() -> None:
    """A token signed with the outgoing key verifies while it is still set."""
    settings = _settings(
        service_jwt_signing_key="a-brand-new-service-secret-0123456789abcdef",
        service_jwt_previous_signing_key=tokens.SERVICE_TEST_SECRET,
    )
    assert ServiceTokenVerifier(settings).verify(tokens.mint_service_token()).tenant
    fresh = tokens.mint_service_token(
        secret="a-brand-new-service-secret-0123456789abcdef"
    )
    assert ServiceTokenVerifier(settings).verify(fresh).tenant == "tenant-a"


def test_a_token_neither_key_signed_is_an_invalid_signature() -> None:
    """With and without a previous key, a stranger's signature is refused."""
    stranger = tokens.mint_service_token(secret="x" * 64)
    assert _reason(stranger) == "invalid_signature"
    rotating = _settings(service_jwt_previous_signing_key="y" * 64)
    assert _reason(stranger, rotating) == "invalid_signature"


def test_the_previous_key_is_not_tried_for_a_failure_that_is_not_the_signature() -> (
    None
):
    """An expired token stays expired whichever key would have verified it."""
    settings = _settings(service_jwt_previous_signing_key="y" * 64)
    assert _reason(tokens.mint_service_token(ttl_seconds=-120), settings) == (
        "token_expired"
    )


# --- the settings guard -----------------------------------------------------


@pytest.mark.parametrize(
    "field", ["service_jwt_signing_key", "service_jwt_previous_signing_key"]
)
def test_an_hs256_service_key_equal_to_the_user_key_refuses_settings(
    field: str,
) -> None:
    """Either service key equal to jwt_signing_key under HS256 is a ConfigError."""
    overrides: dict[str, object] = {
        "jwt_signing_key": "one-shared-secret-for-both",
        "service_jwt_algorithm": "HS256",
        "service_jwt_signing_key": "a-different-service-secret",
        field: "one-shared-secret-for-both",
    }
    with pytest.raises(ConfigError):
        _build_settings(_env_file=None, **overrides)


def test_different_hs256_keys_build_settings() -> None:
    """The refusal is for equality only."""
    settings = _build_settings(
        _env_file=None,
        jwt_signing_key="user-secret",
        service_jwt_algorithm="HS256",
        service_jwt_signing_key="service-secret",
    )
    assert settings.service_jwt_signing_key is not None


def test_the_defaults_are_rs256_the_agreed_names_and_five_minutes() -> None:
    """RS256, dodeal-crm, dodeal-ai, 300 s and no key by default."""
    settings = Settings(_env_file=None, jwt_signing_key="k")
    assert settings.service_jwt_algorithm == "RS256"
    assert settings.service_jwt_issuer == "dodeal-crm"
    assert settings.service_jwt_audience == "dodeal-ai"
    assert settings.service_jwt_max_lifetime_seconds == 300
    assert settings.service_jwt_signing_key is None
    assert settings.service_jwt_previous_signing_key is None


# --- RS256 and the one-line PEM ---------------------------------------------


def test_an_rs256_token_verifies_with_a_one_line_pem() -> None:
    """A public PEM pasted with literal \\n escapes is normalised and verifies."""
    private_pem, public_pem = tokens.rsa_test_keypair()
    settings = _settings(
        service_jwt_algorithm="RS256",
        service_jwt_signing_key=public_pem.strip().replace("\n", "\\n"),
    )
    token = jwt.encode(tokens.service_claims(), private_pem, algorithm="RS256")
    assert ServiceTokenVerifier(settings).verify(token).tenant == "tenant-a"


def test_an_rs256_verifier_refuses_an_hs256_token_as_bad_algorithm() -> None:
    """The configured algorithm is the only one accepted."""
    _private_pem, public_pem = tokens.rsa_test_keypair()
    settings = _settings(
        service_jwt_algorithm="RS256", service_jwt_signing_key=public_pem
    )
    assert _reason(tokens.mint_service_token(), settings) == "bad_algorithm"


def test_an_unparseable_rs256_key_is_its_own_reason() -> None:
    """A key that cannot be read for the algorithm refuses as a config fault."""
    private_pem, _public_pem = tokens.rsa_test_keypair()
    settings = _settings(
        service_jwt_algorithm="RS256", service_jwt_signing_key="not-a-pem-at-all"
    )
    token = jwt.encode(tokens.service_claims(), private_pem, algorithm="RS256")
    assert _reason(token, settings) == "service_key_unusable"


def test_a_shared_secret_is_never_rewritten() -> None:
    """Only a PEM has its escapes turned into newlines."""
    from pydantic import SecretStr

    assert service_key_text(SecretStr("a\\nb")) == "a\\nb"
    assert service_key_text(SecretStr("-----BEGIN X\\nY")) == "-----BEGIN X\nY"


# --- the rest of the vocabulary ---------------------------------------------


def test_no_service_key_refuses_every_token() -> None:
    """Unconfigured is a refusal with its own code, never an allow."""
    settings = Settings(_env_file=None, jwt_signing_key=tokens.TEST_SECRET)
    verifier = ServiceTokenVerifier(settings)
    assert verifier.configured is False
    assert _reason(tokens.mint_service_token(), settings) == (
        "service_token_not_configured"
    )


def test_a_string_that_is_not_a_jwt_is_invalid_token() -> None:
    """Garbage never reaches a key."""
    assert _reason("not.a.jwt") == "invalid_token"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"ttl_seconds": -120}, "token_expired"),
        (
            {"iat": int(time.time()) + 3600, "exp": int(time.time()) + 3700},
            ("token_not_yet_valid"),
        ),
        ({"iat": "yesterday"}, "invalid_iat"),
        ({"exp": "later"}, "invalid_token"),
        ({"subdomain": None}, "missing_required_claim"),
        ({"subdomain": ""}, "missing_subdomain"),
        ({"subdomain": 7}, "missing_subdomain"),
        ({"subdomain": "tenant.evil.com"}, "invalid_tenant_claim"),
    ],
)
def test_each_remaining_failure_has_its_reason(
    overrides: dict[str, object], code: str
) -> None:
    """Expiry, skew, malformed claims and the tenant claim each audit distinctly."""
    claims = tokens.service_claims(**overrides)
    if claims["subdomain"] is None:
        del claims["subdomain"]
    token = jwt.encode(claims, tokens.SERVICE_TEST_SECRET, algorithm="HS256")
    assert _reason(token) == code


def test_the_verifier_reads_settings_when_given_none(monkeypatch) -> None:
    """Built with no argument, it verifies against the process Settings."""
    tokens.service_settings_env(monkeypatch)
    assert ServiceTokenVerifier().settings.service_jwt_algorithm == "HS256"
