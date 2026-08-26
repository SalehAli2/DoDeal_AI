"""Property tests for the single tenant-label rule (TENANT_LABEL_RE), checked
at both boundaries it guards: the Host header (tenant_from_host) and the JWT
claim (extract_identity). One rule, two call sites, proven with the same
properties rather than a fixed example list."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from dodeal_ai.core.auth.claims import (
    TENANT_LABEL_RE,
    extract_identity,
    normalise_tenant_label,
)
from dodeal_ai.core.config import Settings
from dodeal_ai.core.tenancy import tenant_from_host

_BASE_DOMAIN = "dodealcrm.com"
_LABEL_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789-"

# Interior labels only: RFC 1035 forbids a leading/trailing hyphen, so the
# first and last characters are drawn from the no-hyphen alphabet and only the
# (possibly empty) middle may contain one.
_valid_labels = st.builds(
    lambda first, middle, last: first + middle + last,
    first=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789"),
    middle=st.text(alphabet=_LABEL_ALPHABET, max_size=61),
    last=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789"),
).filter(lambda s: TENANT_LABEL_RE.match(s) is not None)


def _decorate(label: str, case: str, trailing_dot: bool, port: str) -> str:
    host = label
    if case == "upper":
        host = host.upper()
    elif case == "mixed":
        host = "".join(c.upper() if i % 2 == 0 else c for i, c in enumerate(host))
    host = f"{host}.{_BASE_DOMAIN}"
    if trailing_dot:
        host += "."
    if port:
        host += port
    return host


@given(
    label=_valid_labels,
    case=st.sampled_from(["lower", "upper", "mixed"]),
    trailing_dot=st.booleans(),
    port=st.sampled_from(["", ":443", ":8000"]),
)
@settings(max_examples=200)
def test_tenant_from_host_normalises_any_valid_label_under_any_decoration(
    label, case, trailing_dot, port
):
    host = _decorate(label, case, trailing_dot, port)
    assert tenant_from_host(host, _BASE_DOMAIN) == label.lower()


@given(text=st.text(max_size=80))
@settings(max_examples=200)
def test_tenant_from_host_never_returns_anything_but_none_or_a_valid_label(text):
    # Also exercise it as a whole Host value and as the label portion of one,
    # so both parsing paths inside tenant_from_host see arbitrary input.
    for host in (text, f"{text}.{_BASE_DOMAIN}"):
        result = tenant_from_host(host, _BASE_DOMAIN)
        assert result is None or TENANT_LABEL_RE.match(result) is not None


@given(text=st.text(max_size=80))
@settings(max_examples=200)
def test_normalise_tenant_label_never_returns_anything_but_none_or_a_valid_label(text):
    result = normalise_tenant_label(text)
    assert result is None or TENANT_LABEL_RE.match(result) is not None


@given(label=_valid_labels)
@settings(max_examples=200)
def test_extract_identity_lowercases_any_valid_uppercased_label(label):
    settings_obj = Settings(_env_file=None, jwt_signing_key="test-key")
    identity = extract_identity(
        {"sub": 1, "subdomain": label.upper(), "database": "d"}, settings_obj
    )
    assert identity.tenant == label.lower()
