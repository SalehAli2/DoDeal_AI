"""Prometheus metrics (register item 22), on one registry of this service's own.

Every label is a fixed vocabulary: an outcome or reason code, a judgement route,
a pass name, a bypass event, a backend failure kind, a breaker name. Never a
tenant, a subject, a note or a lead -- a label per tenant is a cardinality and
a disclosure problem, and a test fails the build if one appears.

`/metrics` (main.py) serves `render()` only when DODEAL_METRICS_ENABLED is set,
outside the gates and the in-flight cap.
"""

from __future__ import annotations

import weakref
from collections.abc import Iterator
from typing import Protocol

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

REGISTRY = CollectorRegistry(auto_describe=True)
CONTENT_TYPE = CONTENT_TYPE_LATEST

JUDGEMENTS = Counter(
    "judgements",
    "Judgements by outcome (completed, suppressed, replayed, or the reason code) "
    "and route (fetch, direct, history).",
    ["outcome", "route"],
    registry=REGISTRY,
)
MODEL_CALLS = Counter(
    "model_calls",
    "Paid model calls by pass (classify, vague, score) and outcome.",
    ["pass", "outcome"],
    registry=REGISTRY,
)
BYPASSES = Counter(
    "bypass",
    "Fail-open bypasses by event code.",
    ["event"],
    registry=REGISTRY,
)
LOAD_SHED = Counter(
    "load_shed",
    "Requests refused at the in-flight cap.",
    registry=REGISTRY,
)
# Register item 157: judgements the short-note tables let past the length
# floor. No label at all -- the question a floor change is decided on is "how
# often", and a tenant label would be a cardinality and a disclosure problem.
RECOGNISED_SHORT = Counter(
    "recognised_short",
    "Judgements of a note below the length floor that a tenant table recognised.",
    registry=REGISTRY,
)
BACKEND_ERRORS = Counter(
    "backend_errors",
    "CRM read failures that stopped a judgement, by kind.",
    ["kind"],
    registry=REGISTRY,
)
# Up to the 25 s default deadline and past it: a histogram that stops below the
# deadline cannot show the requests that ran into it. By route (register item
# 72), so a backfill's latency never reads as the live notes'.
JUDGEMENT_SECONDS = Histogram(
    "judgement_seconds",
    "Wall time of one judgement, from the entry point to its answer, by route.",
    ["route"],
    buckets=(0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0),
    registry=REGISTRY,
)

# The breaker states as numbers: 0 closed, 1 half open, 2 open.
_BREAKER_VALUES = {"closed": 0, "half_open": 1, "open": 2}


class _State(Protocol):
    @property
    def value(self) -> str: ...


class _Breaker(Protocol):
    """What the gauge reads off a breaker; core/breaker.py satisfies it."""

    @property
    def name(self) -> str: ...

    @property
    def state(self) -> _State: ...


# Name -> the most recently built breaker of that name, held weakly.
_BREAKERS: dict[str, weakref.ref[_Breaker]] = {}


def track_breaker(breaker: _Breaker) -> None:
    """Report this breaker's state on every scrape for as long as it lives; a
    newer breaker of the same name replaces it."""
    _BREAKERS[breaker.name] = weakref.ref(breaker)


class _BreakerStates(Collector):
    """breaker_state{breaker}, read at scrape time so no transition has to
    remember to update it. One series per name: the newest live breaker."""

    def collect(self) -> Iterator[GaugeMetricFamily]:
        gauge = GaugeMetricFamily(
            "breaker_state",
            "Circuit breaker state: 0 closed, 1 half open, 2 open.",
            labels=["breaker"],
        )
        for name, ref in sorted(_BREAKERS.items()):
            breaker = ref()
            if breaker is not None:
                gauge.add_metric([name], _BREAKER_VALUES[breaker.state.value])
        yield gauge


REGISTRY.register(_BreakerStates())


def pass_name(label: str) -> str:
    """The pass a model-call label names, e.g. classify."""
    return label.rsplit(".", 1)[-1]


def render() -> bytes:
    """The whole registry in the Prometheus text format."""
    return generate_latest(REGISTRY)
