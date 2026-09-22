"""Valid scoring-pass payloads for the tests that script a model answer.

Register item 131: the model answers twelve yes/no checks, and only the ones
the note type and rubric ask about. The default answer is 55 of a denominator of
80 -- 69, `fair`, one mark below the accept threshold: what_happened and
client_said and clarity fully evidenced, no next step named. A no_contact note
asks about neither what_happened nor client_said, so it takes the narrower
answer below instead.
"""

from __future__ import annotations

# The default rubric asks nine checks of every type but no_contact
# (deal_specifics is off for all, Q13). 25 + 20 + 0 + 10 = 55 of 80.
FAIR_CHECKS: dict[str, bool] = {
    "wh_outcome": True,
    "wh_action": True,
    "cs_present": True,
    "cs_own_terms": True,
    "ns_action": False,
    "ns_date": False,
    "ns_closure": False,
    "cl_readable": True,
    "cl_substance": True,
}

# no_contact suppresses what_happened and client_said as well (register item
# 137): five checks. 13 + 10 = 23 of 35 -> 66, `fair`.
NO_CONTACT_CHECKS: dict[str, bool] = {
    "ns_action": True,
    "ns_date": False,
    "ns_closure": False,
    "cl_readable": True,
    "cl_substance": True,
}


def score_payload(
    checks: dict[str, bool] | None = None, **overrides: bool
) -> dict[str, object]:
    """The scoring pass's answer body: `checks` (default FAIR_CHECKS) with any
    keyword overrides applied, and the reasoning the schema requires."""
    return {
        "checks": {**(FAIR_CHECKS if checks is None else checks), **overrides},
        "reasoning": "Scripted.",
    }
