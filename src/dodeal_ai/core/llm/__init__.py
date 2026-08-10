"""Provider abstraction for LLM calls -- empty until the first real call exists.

Grows into the model gateway at that point: provider routing/fallback, pinned
model versions, prompt caching, timeouts/circuit breakers, per-tenant quotas.
See FUTURE_PATTERNS.md item 3. Keep the watchdog (core/resilience.py) wrapping
the model call when it's added.
"""
