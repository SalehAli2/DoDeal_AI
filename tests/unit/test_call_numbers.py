"""Number detection (units/call_intelligence/numbers.py): written and spoken
digits in both languages, the phone rule, the two hashes, the escalation, and
a prompt copy that masks phones, emails and long digit runs and leaves prices
alone."""

from __future__ import annotations

import hashlib

import pytest

from dodeal_ai.units.call_intelligence.numbers import (
    NumberFindings,
    detect_numbers,
    numbers_if_enabled,
    phone_digits,
    phone_spans,
    prompt_copy,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment

# Invented numbers only.
LEAD_DIGITS = "971501234567"
COMPANY_DIGITS = "971561112222"
LEAD_HASH = hashlib.sha256(LEAD_DIGITS.encode()).hexdigest()
COMPANY_HASH = hashlib.sha256(COMPANY_DIGITS.encode()).hexdigest()


def _say(speaker: str, text: str, start: float = 12.5) -> Segment:
    return Segment(
        start_s=start,
        end_s=start + 5,
        speaker=speaker,
        text=text,
        language="en",
        confidence=0.9,
    )


def _detect(*segments: Segment) -> NumberFindings:
    return detect_numbers(
        segments,
        lead_phone_hash=LEAD_HASH,
        agent_phone_hash=COMPANY_HASH,
        country_code="971",
    )


def _digits(text: str) -> list[str]:
    return [span.digits for span in phone_spans(text)]


# --- the guard -------------------------------------------------------------------


def test_a_client_giving_the_leads_number_matches_lead() -> None:
    findings = _detect(_say("lead", "you can reach me on 050 123 4567 anytime"))
    assert findings.finds == [
        {
            "speaker": "client",
            "start_s": 12.5,
            "segment": "s1",
            "last4": "4567",
            "match": "lead",
        }
    ]
    assert findings.escalations == []


def test_an_agent_giving_another_number_escalates() -> None:
    findings = _detect(
        _say("agent", "Good morning.", start=0),
        _say("agent", "text me on my own mobile, 055 765 4321", start=9),
    )
    (find,) = findings.finds
    assert (find["match"], find["segment"], find["last4"]) == (
        "agent_personal",
        "s2",
        "4321",
    )
    assert findings.escalations == [
        {
            "type": "off_channel_contact",
            "source": "number",
            "speaker": "agent",
            "start_s": 9,
            "segment": "s2",
        }
    ]


def test_a_price_is_not_masked() -> None:
    text = "the asking price is 1,200,000 AED, or 1.2 million"
    assert prompt_copy(text) == text
    assert phone_spans(text) == []


# --- what is matched -------------------------------------------------------------


def test_the_agents_company_number_is_not_personal() -> None:
    findings = _detect(_say("agent", "the office line is 056 111 2222"))
    assert [f["match"] for f in findings.finds] == ["agent_company"]
    assert findings.escalations == []


def test_a_client_giving_another_number_is_a_new_client_number() -> None:
    findings = _detect(_say("lead", "use my wife's number, 0529876543"))
    assert [f["match"] for f in findings.finds] == ["new_client_number"]
    assert findings.escalations == []


@pytest.mark.parametrize("lead", [None, LEAD_HASH])
def test_with_no_company_hash_an_agents_number_is_unverified_and_never_escalates(
    lead: str | None,
) -> None:
    findings = detect_numbers(
        (_say("agent", "text me on my own mobile, 055 765 4321"),),
        lead_phone_hash=lead,
        agent_phone_hash=None,
        country_code="971",
    )
    assert [f["match"] for f in findings.finds] == ["agent_unverified"]
    assert findings.escalations == []


def test_with_no_company_hash_the_leads_number_still_matches() -> None:
    findings = detect_numbers(
        (_say("agent", "I have you on 050 123 4567"),),
        lead_phone_hash=LEAD_HASH,
        agent_phone_hash=None,
        country_code="971",
    )
    assert [f["match"] for f in findings.finds] == ["lead"]


@pytest.mark.parametrize("label", ["speaker_1", "unknown"])
def test_with_no_roles_applied_a_number_not_the_leads_is_put_to_review(
    label: str,
) -> None:
    """An engine's label or no speaker at all: the speaker is unknown, and
    only the lead's own number raises nothing."""
    findings = _detect(
        _say(label, "reach me on 050 123 4567", start=1.0),
        _say(label, "or text me on 055 765 4321", start=6.0),
        _say(label, "the office is 056 111 2222", start=11.0),
    )
    assert [(f["speaker"], f["match"]) for f in findings.finds] == [
        ("unknown", "lead"),
        ("unknown", "unattributed_number"),
        ("unknown", "agent_company"),
    ]
    assert findings.escalations == [
        {
            "type": "off_channel_contact_review",
            "source": "number",
            "speaker": "unknown",
            "start_s": start,
            "segment": segment,
        }
        for start, segment in ((6.0, "s2"), (11.0, "s3"))
    ]
    with_no_company = detect_numbers(
        (_say(label, "055 765 4321"),),
        lead_phone_hash=None,
        agent_phone_hash=None,
        country_code="971",
    )
    assert [f["match"] for f in with_no_company.finds] == ["unattributed_number"]
    assert len(with_no_company.escalations) == 1


def test_a_hash_is_compared_whatever_its_case() -> None:
    findings = detect_numbers(
        (_say("lead", "050 123 4567"),),
        lead_phone_hash=LEAD_HASH.upper(),
        agent_phone_hash=None,
        country_code="971",
    )
    assert [f["match"] for f in findings.finds] == ["lead"]


# --- the phone rule ---------------------------------------------------------------


@pytest.mark.parametrize(
    "said",
    [
        "+971 50 123 4567",
        "00971-50-123-4567",
        "050.123.4567",
        "(050) 1234567",
        "٠٥٠١٢٣٤٥٦٧",
        "۰۵۰ ۱۲۳ ۴۵۶۷",
        "zero five zero, one two three, four five six seven",
        "oh five oh one two three four five six seven",
        "plus nine seven one five zero one two three four five six seven",
        "zero five zero 123 4567",
        "zero five zero one two three four five six seven",
        "صفر خمسة صفر واحد اتنين تلاتة اربعة خمسة ستة سبعة",
        "صفر خمسه صفر وحده ثنين ثلاث أربع خمس ست سبع",
        "زائد تسعة سبعة واحد خمسة صفر واحد اثنين ثلاثة أربعة خمسة ستة سبعة",
    ],
)
def test_every_form_of_one_number_normalises_to_the_same_digits(said: str) -> None:
    assert _digits(said) == [LEAD_DIGITS]


def test_double_and_triple_repeat_the_next_digit() -> None:
    said = "zero five five double seven one triple two three"
    assert _digits(said) == ["971557712223"]


def test_arabic_dialect_digits_with_a_leading_and() -> None:
    said = "صفر خمسة خمسة وسبعة سبعة تمانية تمانية تسعة تسعة صفر"
    assert _digits(said) == ["971557788990"]


@pytest.mark.parametrize(
    ("digits", "plus", "expected"),
    [
        ("0501234567", False, "971501234567"),
        ("00971501234567", False, "971501234567"),
        ("971501234567", True, "971501234567"),
        ("971501234567", False, "971501234567"),
        ("041234567", False, "97141234567"),
        ("97141234567", False, "97141234567"),
        ("9714123456", False, None),
        ("00201001234567", False, "201001234567"),
        ("97150123", False, None),
        ("0501234", False, None),
        ("1200000", False, None),
        ("2026", False, None),
        ("0012345", False, None),
        ("123456789012345678", True, None),
    ],
)
def test_the_phone_rule(digits: str, plus: bool, expected: str | None) -> None:
    assert phone_digits(digits, plus=plus) == expected


@pytest.mark.parametrize(
    "said", ["04 123 4567", "+971 4 123 4567", "00971 4 1234567", "971 4 123 4567"]
)
def test_a_landline_hashes_the_same_in_every_form(said: str) -> None:
    """A leading single 0 takes the country code for landlines too."""
    assert _digits(said) == ["97141234567"]


def test_the_tenants_country_code_replaces_the_leading_zero() -> None:
    assert phone_digits("0501234567", plus=False, country_code="966") == (
        "966501234567"
    )
    assert phone_spans("966 50 123 4567", country_code="966")[0].digits == (
        "966501234567"
    )
    assert prompt_copy("call 050 123 4567", country_code="966") == "call [PHONE]"


def test_a_saudi_tenants_client_giving_the_leads_number_matches_lead() -> None:
    saudi_lead = hashlib.sha256(b"966501234567").hexdigest()
    findings = detect_numbers(
        (_say("lead", "my number is 050 123 4567"),),
        lead_phone_hash=saudi_lead,
        agent_phone_hash=None,
        country_code="966",
    )
    assert [f["match"] for f in findings.finds] == ["lead"]


def test_two_numbers_in_one_run_are_two_finds() -> None:
    assert _digits("050 123 4567, 055 765 4321") == ["971501234567", "971557654321"]


def test_a_number_after_a_unit_number_is_still_found() -> None:
    assert _digits("unit 12 0501234567") == ["971501234567"]


def test_a_plus_mid_run_starts_a_new_number() -> None:
    assert _digits("0501234567 +44 20 7946 0000") == ["971501234567", "442079460000"]


@pytest.mark.parametrize(
    "text",
    [
        "we meet at 10:30 on 15/01/2026",
        "the villa is 4500 square feet on plot 7",
        "double",
        "five bedrooms, three bathrooms",
        "plus the service charge",
    ],
)
def test_ordinary_numbers_are_not_phone_numbers(text: str) -> None:
    assert phone_spans(text) == []


# --- the prompt copy --------------------------------------------------------------


def test_the_prompt_copy_masks_phones_and_emails_only() -> None:
    text = "mail sara.k@example.com or call 050 123 4567 about the 2,500,000 unit"
    assert prompt_copy(text) == (
        "mail [EMAIL] or call [PHONE] about the 2,500,000 unit"
    )


def test_a_long_digit_run_is_masked_but_a_price_is_not() -> None:
    """The guard: another country's number said without its + is masked in
    the prompt copy; a price grouped in thousands is kept."""
    assert prompt_copy("call 966 50 123 4567 today") == "call [PHONE] today"
    assert prompt_copy("it is 1,200,000 AED") == "it is 1,200,000 AED"


@pytest.mark.parametrize(
    ("said", "shown"),
    [
        ("ref 1234 5678", "ref 1234 5678"),
        ("ref 123 456 789", "ref [PHONE]"),
        ("ref 123.456.789", "ref [PHONE]"),
        ("ref 123-456-789", "ref [PHONE]"),
        ("id 123456789012345", "id [PHONE]"),
        ("card 4111 1111 1111 1111", "card [PHONE]"),
        ("id 1234567890123456789", "id [PHONE]"),
        ("id 12345678901234567890", "id 12345678901234567890"),
        ("٩٦٦ ٥٠ ١٢٣ ٤٥٦٧", "[PHONE]"),
        ("1,250,500 123 456", "1,250,500 123 456"),
        ("1,250,000,000.50 in all", "1,250,000,000.50 in all"),
        ("1٬200٬000 درهم", "1٬200٬000 درهم"),
        ("1,200,000 and 966 50 123 4567", "1,200,000 and [PHONE]"),
        ("50,123,456,78", "50,123,456,78"),
    ],
    ids=[
        "eight-kept",
        "nine-spaces",
        "nine-dots",
        "nine-hyphens",
        "fifteen",
        "sixteen-card",
        "nineteen",
        "twenty-kept",
        "arabic-indic",
        "price-beside-digits",
        "price-decimals",
        "arabic-separator",
        "price-then-run",
        "commas-break-runs",
    ],
)
def test_the_prompt_copy_masks_every_run_of_nine_to_nineteen_digits(
    said: str, shown: str
) -> None:
    assert prompt_copy(said) == shown


def test_a_masked_run_is_not_a_find() -> None:
    """The finds stay the phone rule's."""
    assert _detect(_say("lead", "my Saudi number is 966 50 123 4567")).finds == []


def test_digits_inside_an_email_are_not_a_phone_number() -> None:
    findings = _detect(_say("agent", "write to agent0501234567@example.com"))
    assert findings.finds == []
    assert prompt_copy("agent0501234567@example.com") == "[EMAIL]"


def test_a_spoken_number_is_masked_whole() -> None:
    assert (
        prompt_copy(
            "it is plus nine seven one five zero one two three four "
            "five six seven, thanks"
        )
        == "it is [PHONE], thanks"
    )


# --- the switch -------------------------------------------------------------------


def test_nothing_is_looked_for_while_the_switch_is_off() -> None:
    segments = (_say("agent", "055 765 4321"),)
    kwargs = {
        "lead_phone_hash": LEAD_HASH,
        "agent_phone_hash": COMPANY_HASH,
        "country_code": "971",
    }
    assert numbers_if_enabled(segments, enabled=False, **kwargs) is None
    found = numbers_if_enabled(segments, enabled=True, **kwargs)
    assert found is not None and len(found.escalations) == 1
