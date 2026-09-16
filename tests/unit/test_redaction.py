"""Register item 59: emails, phones and long ids leave the text a model reads;
dates, times and short numbers stay."""

from __future__ import annotations

import pytest

from dodeal_ai.core.redaction import Redaction, redact

ARABIC_INDIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
EXTENDED_ARABIC_INDIC = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


@pytest.mark.parametrize(
    "email",
    ["client@example.com", "first.last+crm@mail.example.co", "a_b-9@x-y.io"],
)
def test_an_email_is_redacted(email):
    """Every common email shape becomes [EMAIL] and is counted."""
    result = redact(f"Sent the brochure to {email} today.")
    assert result.text == "Sent the brochure to [EMAIL] today."
    assert (result.email, result.phone, result.id) == (1, 0, 0)


@pytest.mark.parametrize(
    "phone",
    [
        "+20 10 1234 5678",
        "010-1234-5678",
        "(02) 2345 6789",
        "010.123.4567",
        "+201012345678",
        "123-4567",
        "(012)3456789",
    ],
)
def test_a_formatted_run_of_seven_or_more_digits_is_a_phone(phone):
    """A separator or a leading + makes a 7+ digit run a phone."""
    result = redact(f"Call him on {phone} after six.")
    assert result.text == "Call him on [PHONE] after six."
    assert (result.phone, result.email, result.id) == (1, 0, 0)


@pytest.mark.parametrize("digits", ["01012345678", "29801011234567", "12345678"])
def test_an_unformatted_run_of_eight_or_more_digits_is_an_id(digits):
    """Bare 8+ digit runs are [ID]: national ids, references, a bare mobile."""
    result = redact(f"Reference {digits} on file.")
    assert result.text == "Reference [ID] on file."
    assert (result.id, result.phone) == (1, 0)


def test_digits_glued_to_letters_are_still_an_id():
    """A reference with a prefix loses its digits all the same."""
    assert redact("Invoice INV98765432 paid.").text == "Invoice INV[ID] paid."


@pytest.mark.parametrize(
    "text",
    [
        "Budget 1250000 for a 3BR on floor 12.",
        "Offer 1,250,000 EGP, 150 m2, 2 bathrooms.",
        "Unit 12-4, deposit 10%.",
        "Price 3.5M, handover 2027.",
        "Code 123456.",
        "Ext 12 345.",
    ],
)
def test_short_numbers_are_kept(text):
    """A price, an area, a floor or a count is what the rubric scores."""
    result = redact(text)
    assert result == Redaction(text)


@pytest.mark.parametrize(
    "text",
    [
        "Viewing on 2026-01-15 at 10:30.",
        "Viewing on 15/01/2026 at 10:30:45.",
        "Viewing on 01/15/2026.",
        "Signed 15.01.2026, keys 15-1-26.",
        "Call back 2026/1/5 at 9:05.",
    ],
)
def test_dates_and_times_are_kept(text):
    """A plausible date or time is never a phone, however many digits."""
    assert redact(text) == Redaction(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Code 12-34-5678.", "Code [PHONE]."),
        ("Code 2026-13-45.", "Code [PHONE]."),
        ("Code 45/45/2026.", "Code [PHONE]."),
        ("At 99:99 sharp.", "At 99:99 sharp."),
        ("Run 2026-01-159999.", "Run [PHONE]."),
    ],
)
def test_an_implausible_date_or_time_falls_back_to_the_digit_rules(text, expected):
    """Out-of-range parts are not a calendar: the run rules decide instead."""
    assert redact(text).text == expected


@pytest.mark.parametrize("table", [ARABIC_INDIC, EXTENDED_ARABIC_INDIC])
def test_arabic_indic_digits_follow_the_same_rules(table):
    """Phones, ids, dates and short numbers read the same in either digit set."""
    text = "اتصل 010-1234-5678 رقم 29801011234567 موعد 2026-01-15 سعر 1250000"
    result = redact(text.translate(table))
    assert result.text == (
        "اتصل [PHONE] رقم [ID] موعد 2026-01-15 سعر 1250000".translate(table)
    )
    assert (result.phone, result.id, result.email) == (1, 1, 0)


def test_every_kind_is_counted_separately():
    """Two phones, one email and one id in one note, each counted."""
    result = redact(
        "Mobile +20 100 000 0000, office (02) 3333-4444, "
        "mail sales@example.com, id 29801011234567."
    )
    assert result.text == ("Mobile [PHONE], office [PHONE], mail [EMAIL], id [ID].")
    assert result.fields() == {
        "redacted_phone": 2,
        "redacted_email": 1,
        "redacted_id": 1,
    }


def test_text_with_nothing_to_redact_is_unchanged():
    """A clean note comes back byte for byte with zero counts."""
    text = "Discussed the New Cairo 3BR; following up Tuesday."
    assert redact(text) == Redaction(text, 0, 0, 0)


def test_the_counts_never_carry_a_value():
    """The line fields are three integers and nothing from the text."""
    fields = redact("Call 010-1234-5678").fields()
    assert all(isinstance(value, int) for value in fields.values())
    assert "1234" not in str(fields)
