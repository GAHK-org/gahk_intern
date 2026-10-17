import logging
import threading
import time
from datetime import timedelta

from django.db import connection
from django.db.models import QuerySet
from django.utils import timezone

logger = logging.getLogger(__name__)

# How often a running job writes down how far it has got. Well inside models.JOB_STALLED_AFTER, so
# a job still working is always seen to be, and rare enough that a big import is not one UPDATE per
# megabyte.
PROGRESS_WRITE_INTERVAL = timedelta(seconds=30)


class JobProgress:
    """Records how far a background job has got, on its row, where `fail_stalled_*` can see it.

    Call it as often as is convenient — per file, per chunk read — with the step and the counts; it
    writes at most once per PROGRESS_WRITE_INTERVAL. It only writes when called, never from a timer:
    a worker wedged somewhere stops reporting just like a dead one, and is treated like one.

    The write goes over a connection of its own when the job is inside a transaction — an import
    stores the whole archive in one. An UPDATE made inside it would stay invisible to everyone else
    until it commits, and for an import that dies half-way that is never.
    """

    def __init__(self, rows: QuerySet) -> None:
        self._rows = rows
        self._written_at = time.monotonic()

    def __call__(self, step: str, done: int, total: int) -> None:
        now = time.monotonic()
        if now - self._written_at < PROGRESS_WRITE_INTERVAL.total_seconds():
            return
        self._written_at = now
        fields = {"progress": {"step": step, "done": done, "total": total}, "progressed_at": timezone.now()}
        if not connection.in_atomic_block:
            self._write(fields)
            return
        thread = threading.Thread(target=self._write_on_own_connection, args=(fields,), daemon=True)
        thread.start()
        thread.join()

    def _write(self, fields: dict[str, object]) -> None:
        # Never the reason a job fails: a missed write costs at most an early reap, a raise here
        # would cost the import itself.
        try:
            self._rows.update(**fields)
        except Exception:
            logger.warning("Could not record the job's progress.", exc_info=True)

    def _write_on_own_connection(self, fields: dict[str, object]) -> None:
        try:
            self._write(fields)
        finally:
            connection.close()  # this thread's connection, not the job's
