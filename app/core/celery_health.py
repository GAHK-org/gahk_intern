"""The worker heartbeat as a Celery bootstep — see core.health.

The worker's own timer, in the main process: it keeps beating while the pool's single slot runs an
hour-long encode, and stops if the worker itself hangs, which is what the healthcheck is for.
"""

from typing import Any

from celery import bootsteps


class HeartbeatStep(bootsteps.StartStopStep):
    requires = {"celery.worker.components:Timer"}

    def __init__(self, worker: Any, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(worker, **kwargs)
        self._tref: Any = None

    def start(self, worker: Any) -> None:  # noqa: ANN401
        from .health import WORKER_HEARTBEAT_SECONDS, worker_heartbeat

        worker_heartbeat(worker.hostname)
        self._tref = worker.timer.call_repeatedly(
            WORKER_HEARTBEAT_SECONDS, worker_heartbeat, (worker.hostname,), priority=10
        )

    def stop(self, worker: Any) -> None:  # noqa: ANN401
        if self._tref is not None:
            self._tref.cancel()
