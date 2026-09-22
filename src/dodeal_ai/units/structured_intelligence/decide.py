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

WHY THE ORDER IS FIXED (history, resubmission, attempt_cap, rate_limited,
nothing_to_ask). More than one can be true at once, and the reported reason
must be stable, so it is the most SPECIFIC-to-least fact about why we are
quiet. A history judgement (register item 127) is an old note nobody is waiting
on, so it never asks and says so first; a resubmission is a policy about this
request; the attempt cap is about
this note; the rate limit is about this person; "nothing to ask" is about the
answer we got. Reporting the rate limit to someone whose note was a
resubmission would send them to the wrong explanation.

THE ENFORCEMENT BLOCK IS THE THIRD QUESTION, and `enforcement()` below answers
it for EVERY judgement -- scored or suppressed. It is separate from decide()
because a suppressed note has no Decision to hang a verdict on and still needs
one: the CRM must never infer what to do from an action that is not there.
Nothing here blocks a save today; `strict` is built and recorded, never
enabled, and the mode comes from the tenant's config and nowhere else.

NO MODEL OUTPUT REACHES A DECISION. The inputs are a computed NoteScore, one
integer and one flag from db2, the tenant's config, and two booleans from the
route.
`analysis.clarification_prompt` is consulted for PRESENCE only -- whether there
is a question to ask -- never for its content.
"""

from __future__ import annotations

from dodeal_ai.units.structured_intelligence.config import TenantConfig
from dodeal_ai.units.structured_intelligence.schemas import (
    Decision,
    DecisionAction,
    Enforcement,
    EnforcementMode,
    EnforcementTarget,
    EnforcementVerdict,
    NoteAnalysis,
    NoteScore,
    PromptWithheld,
    SuppressedDetail,
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


# The two actions that mean we have something to say about the note: one
# accepts it with a flag, the other wants to ask about it. accept_silent is
# absent deliberately -- a note we would not ask about is not flagged.
_FLAGGED_ACTIONS: frozenset[DecisionAction] = frozenset(
    {DecisionAction.ACCEPT_FLAG_PROMPT, DecisionAction.PROMPT_CLARIFICATION}
)

# The one suppression that is still a complaint ABOUT THE NOTE: it is below the
# length floor, so the salesperson wrote too little. Every other suppression is
# a statement about what we can judge -- a machine timeline entry, a note the
# classifier could not place, a note too long to be one interaction -- and
# flagging a salesperson for one would be flagging them for our own limits.
_FLAGGED_SUPPRESSION: SuppressedDetail = SuppressedDetail.NOTE_TOO_SHORT


def enforcement(
    config: TenantConfig,
    *,
    action: DecisionAction | None = None,
    detail: SuppressedDetail | None = None,
) -> Enforcement:
    """The enforcement block, for every judgement this service returns.

    Pure, and derived from TWO things only: the tenant's mode and what we
    already decided about the note. No model output reaches it -- there is no
    field on any output schema it could read -- and neither does the CRM's
    payload. A caller cannot hand us a verdict any more than it can hand us a
    band.

    Exactly one of `action` and `detail` is given: `action` on a scored
    judgement, `detail` on a suppressed one. Both defaulting to None is not an
    invitation to pass neither -- a judgement with neither is not a shape the
    pipeline can produce, and if it were, "nothing was decided about this note"
    is honestly an allow.

    OFF short-circuits before anything else is looked at: a tenant that has
    turned enforcement off gets `allow` whatever the action was. The judgement
    is still made and still reported in full -- off is about what the CRM does
    with it, not about whether we do the work.
    """
    if config.enforcement_mode is EnforcementMode.OFF:
        return Enforcement(
            mode=EnforcementMode.OFF,
            verdict=EnforcementVerdict.ALLOW,
            applies_to=None,
        )

    # STRICT DOES NOT BLOCK, and must not until the blocking work lands.
    # Blocking is scoped to a STAGE CHANGE, and the request carries no field
    # saying the lead is moving stage -- that field is an ask to the backend
    # and a register item of its own. Until it exists there is no moment to
    # block at, so strict is advisory plus a recorded intent: a tenant may set
    # it, and the mode on the judgement says so, but the verdict is the same.
    flagged = action in _FLAGGED_ACTIONS or detail is _FLAGGED_SUPPRESSION
    return Enforcement(
        mode=config.enforcement_mode,
        verdict=EnforcementVerdict.FLAG if flagged else EnforcementVerdict.ALLOW,
        # Null exactly when nothing is flagged: `allow` has nothing to be about.
        applies_to=EnforcementTarget.NOTE if flagged else None,
    )


def withheld_reason(
    analysis: NoteAnalysis,
    *,
    attempts: int,
    rate_allowed: bool,
    rate_count: int,
    config: TenantConfig,
    resubmission: bool,
    history: bool = False,
) -> PromptWithheld | None:
    """The first failing condition, or None when all five hold and we ask.

    Written as separate `if`s rather than an `all()` because the ORDER is
    the contract: a chained boolean would give the same answer to "may we ask?"
    and no answer at all to "why not?".

    Public (not `_`-prefixed): `decide()` below is the caller for a SCORED
    judgement, and the length gate's fixed question (register item 64) is the
    other -- it has no NoteScore to build a Decision from, only the four
    conditions this function alone decides.
    """
    if history:
        return PromptWithheld.HISTORY
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
    history: bool = False,
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

    `history` (register item 127) withholds every prompt as `history`; the
    action, and so the enforcement verdict, is computed exactly as for a live
    note.

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
        withheld = withheld_reason(
            analysis,
            attempts=attempts,
            rate_allowed=rate_allowed,
            rate_count=rate_count,
            config=config,
            resubmission=resubmission,
            history=history,
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
