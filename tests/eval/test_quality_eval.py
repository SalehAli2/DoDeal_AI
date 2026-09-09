"""Quality eval — a placeholder, and deliberately empty of model code.

The structural eval next door proves the pipeline survives the corpus. It says
nothing about whether a judgement is RIGHT: every note there is classified
`discovery` and marked identically, because `FakeLLM` returns whatever it was
handed. Asking whether the classifier is accurate, whether the vague call agrees
with a human, and whether the marks track the rubric needs a real model and a
labelled set, and this repo has neither yet.

TRIGGER: STEP 18 (the provider adapter). Until `get_llm_client()` returns
something that talks to a provider, there is nothing here to run and nothing
honest to assert. When step 18 lands, this file gets:

  - a labelled subset of the corpus, agreed with the business, with the label
    being a human's answer and not a previous model's;
  - per-pass agreement rates against those labels, reported rather than
    asserted, until there is an agreed floor to assert against;
  - a cost and latency record per pass, since a quality number nobody can
    afford is not a result.

WHY IT IS A FILE AND NOT A TICKET. A skipped test in the suite is visible on
every run and impossible to lose; a ticket is not. The skip is opt-in through
`DODEAL_EVAL_REAL=1` so that turning it on is a deliberate act by someone who
knows they are about to spend money -- and CI never sets it.

There is no provider code in this file, and there must not be until step 18.
Anything that calls a real model from here before then would spend money from a
test run and would bypass the seam the whole campaign has been building.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.eval

_ENABLED = os.environ.get("DODEAL_EVAL_REAL") == "1"


@pytest.mark.skipif(
    not _ENABLED,
    reason="real-model quality eval: set DODEAL_EVAL_REAL=1 to enable (step 18)",
)
def test_quality_against_a_real_model():
    # Reached only when someone has deliberately opted in, and still skips:
    # there is no provider to call yet. This is the trigger point for step 18 --
    # see the module docstring for what belongs here when it lands.
    pytest.skip("step 18: real-model quality eval")
