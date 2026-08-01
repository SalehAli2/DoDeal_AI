"""Cost controls

Per-tenant and per-user LLM spend/token counters, enforced
inside the auth dependency chain. Over limit -> 429. Backed by Redis.
"""
