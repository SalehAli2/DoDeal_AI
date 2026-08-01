"""Watchdog timeout + retry policy.

One shared timeout/retry-once-then-fail-closed wrapper for every external
call (LLM + backend tools). Imported by core/llm and tools/ so the policy
is defined once and cannot drift.
"""
