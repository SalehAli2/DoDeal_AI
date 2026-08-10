"""Unit A -- structured intelligence (fast/sync note scoring).

Empty until the exit demo passes (see ASSUMPTIONS.md). The first write this
service ever makes back to the CRM (note write-back) starts here -- apply an
idempotency key, or pass retry=False, before wrapping it in the watchdog. See
FUTURE_PATTERNS.md item 1.
"""
