"""arq worker entrypoint — the skeleton, deliberately empty.

Decision 2 (docs/decisions/0001-principal-model-and-execution-model.md) settled
one execution model: workers run the *same* async code as the request path,
under arq, rather than a forking Celery process bridging into it. This module
is where that worker is configured; it holds no tasks yet.

Run it (once there is something to run) with:

    arq dodeal_ai.workers.runner.WorkerSettings

Redis comes from the same `redis_queue_url` the queue connection always used,
parsed into arq's own settings type. That read is deferred to attribute access
by a descriptor, so importing this module reads no settings and opens no
connection. Both matter: the wheel import check imports every module in the
package inside a venv with no Redis running, and pytest imports this module at
COLLECTION -- before any fixture can supply DODEAL_JWT_SIGNING_KEY, which CI
has neither in the environment nor in a .env file. An eager read here failed
the whole test file with ConfigError at collection time.

TRIGGER -- step 14 (the sweep job API) fills this in: the two lanes
(worker-sweep, worker-bulk) become arq queues, and the real tasks are appended
to `functions`. Job state, priority lanes, dead-lettering and idempotency are
designed there, not here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from arq.connections import RedisSettings

from dodeal_ai.core.config import get_settings


def redis_settings() -> RedisSettings:
    """Derive arq's Redis settings from `redis_queue_url`. Parses the URL into
    host/port/database; it does not connect."""
    return RedisSettings.from_dsn(get_settings().redis_queue_url)


class _LazyRedisSettings:
    """Resolves `redis_settings()` when the attribute is READ, not when the
    class is created. arq reads `WorkerSettings.redis_settings` as it starts
    the worker, which is late enough for the environment to be in place; doing
    it in the class body made merely importing this module load Settings.

    Reading current settings on every access is also what the tests want: a
    value captured once would go stale the moment a test changed the URL."""

    def __get__(
        self,
        obj: WorkerSettings | None,
        objtype: type[WorkerSettings] | None = None,
    ) -> RedisSettings:
        return redis_settings()


class WorkerSettings:
    """What `arq` reads to build the worker. Empty on purpose: no task exists
    until step 14, and a placeholder task would pre-decide a layout that the
    sweep job API has not designed yet."""

    redis_settings = _LazyRedisSettings()
    functions: ClassVar[list[Callable[..., Any]]] = []
