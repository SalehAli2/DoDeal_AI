"""Redis connection layer.
this module owns the client connections.
Two isolated instances:
  - queue_client : Celery broker (async task queue)
  - cost_client  : per-tenant/user quota counters (§7)
Session-state instance (Unit C1) is added in Phase 2, not now.
"""
