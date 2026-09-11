"""decide — what the CRM is told, and whether we ask the salesperson anything.

Two independent questions, answered in that order, and keeping them independent
is the whole design:

  1. WHAT IS THE ADVICE? Purely the total against the tenant's two thresholds.
     A good note is accepted silently, a middling one is accepted with a flag,
     a poor one is advised for clarification. Nothing about counters, the store
     or the route touches this -- the same note scores the same advice on its
     tenth resubmission as on its first.

  2. IS A PROMPT ACTUALLY SENT? Four conditions, checked in a fixed order, and
     the FIRST that fails becomes `prompt_withheld`. A withheld prompt is a
     STATE with a reason, never a silently missing one: the CRM can tell "we
     had nothing to ask" from "this person has been asked enough today"
     without inferring it from an absent field.

WHY THE ORDER OF THE FOUR IS FIXED (resubmission, attempt_cap, rate_limited,
nothing_to_ask). More than one can be true at once, and the reported reason
must be stable, so it is the most SPECIFIC-to-least fact about why we are
quiet. A resubmission is a policy about this request; the attempt cap is about
this note; the rate limit is about this person; "nothing to ask" is about the
answer we got. Reporting the rate limit to someone whose note was a
resubmission would send them to the wrong explanation.

ENFORCEMENT IS THE CRM'S. `prompt_clarification` is advice; nothing here blocks
a save, and `enforcement_mode` does not branch (see config.py). What this
function controls is only whether WE send a question and whether the counters
that limit our questions move.

NO MODEL OUTPUT REACHES A DECISION. The inputs are a computed NoteScore, one
integer and one flag from db2, the tenant's config, and one boolean from the
route.
`analysis.clarification_prompt` is consulted for PRESENCE only -- whether there
is a question to ask -- never for its content.
"""

from __future__ import annotations

from dodeal_ai.units.structured_intelligence.config import TenantConfig
from dodeal_ai.units.structured_intelligence.schemas import (
    Decision,
    DecisionAction,
    NoteAnalysis,
    NoteScore,
    PromptWithheld,
)


def _action(total: int, config: TenantConfig) -> DecisionAction:
    """The advice, from the total alone.

    Thresholds come from TenantConfig and are never literals here: the same
    rule that keeps weights out of prompt text keeps them out of this file.
    """
    if total >= config.accept_threshold:
        return DecisionAction.ACCEPT_SILENT
    if total >= config.flag_threshold:
        return DecisionAction.ACCEPT_FLAG_PROMPT
    return DecisionAction.PROMPT_CLARIFICATION


def _withheld(
    analysis: NoteAnalysis,
    *,
    attempts: int,
    rate_allowed: bool,
    rate_count: int,
    config: TenantConfig,
    resubmission: bool,
) -> PromptWithheld | None:
    """The first failing condition, or None when all four hold and we ask.

    Written as four separate `if`s rather than an `all()` because the ORDER is
    the contract: a chained boolean would give the same answer to "may we ask?"
    and no answer at all to "why not?".
    """
    if resubmission:
        return PromptWithheld.RESUBMISSION
    if attempts >= config.clarification_cap:
        return PromptWithheld.ATTEMPT_CAP
    # TWO INPUTS, ONE CONDITION, because db2 answers this question in two ways
    # (state.py): the script REFUSED to take a slot when a prompt would be
    # sent, or the read came back at the cap when there was nothing to ask.
    if not rate_allowed or rate_count >= config.rate_limit_per_hour:
        return PromptWithheld.RATE_LIMITED
    if analysis.clarification_prompt is None:
        return PromptWithheld.NOTHING_TO_ASK
    return None


def decide(
    score: NoteScore,
    analysis: NoteAnalysis,
    *,
    attempts: int,
    rate_allowed: bool,
    rate_count: int,
    config: TenantConfig,
    resubmission: bool,
) -> Decision:
    """The judgement's decision half. Pure: no I/O, no clock, no model.

    `attempts` and `rate_count` are the counts BEFORE this request, as read
    from db2. `attempt` on the result is the count AFTER it -- one more when we
    are about to send a prompt, unchanged when we are not -- so the number the
    CRM shows a salesperson matches the number of questions they have actually
    been asked. The increments themselves are the pipeline's, and they run only
    when `prompt_sent` is true; this function decides, it does not write.

    `rate_allowed` is db2's answer from the round trip that claimed the slot:
    this function is told what the store said and decides nothing about it.

    A store outage reads 0 through the fail-open policy in state.py, so an
    unreachable db2 makes us MORE willing to ask, never less. That is the
    intended direction: the counters are a politeness guard, and the failure
    mode of a politeness guard should be a question too many, not a judgement
    refused.

    ACCEPT_SILENT NEVER CARRIES A WITHHELD REASON. There was no prompt to
    withhold -- the note was good, and nothing was computed to ask. Filling in
    `nothing_to_ask` there would tell the CRM we wanted to ask something and
    could not, which is the opposite of what happened.
    """
    action = _action(score.total, config)

    if action is DecisionAction.ACCEPT_SILENT:
        withheld: PromptWithheld | None = None
        prompt_sent = False
    else:
        withheld = _withheld(
            analysis,
            attempts=attempts,
            rate_allowed=rate_allowed,
            rate_count=rate_count,
            config=config,
            resubmission=resubmission,
        )
        prompt_sent = withheld is None

    attempt = attempts + 1 if prompt_sent else attempts
    return Decision(
        action=action,
        prompt_sent=prompt_sent,
        prompt_withheld=withheld,
        attempt=attempt,
        attempts_remaining=max(config.clarification_cap - attempt, 0),
    )
