"""Marks every worker job as one source of audit events, its task id as their correlation id.

Eager tasks (tests, and anything called with .apply) get the same context as a real worker's.
"""

from contextlib import ExitStack
from typing import Any

from celery.signals import task_postrun, task_prerun

from . import trail

_stacks: dict[str, ExitStack] = {}


@task_prerun.connect(weak=False, dispatch_uid="audit:task_prerun")
def _enter(task_id: str | None = None, **_: Any) -> None:  # noqa: ANN401
    stack = ExitStack()
    stack.enter_context(trail.context("worker", correlation_id=task_id or ""))
    _stacks[task_id or ""] = stack


@task_postrun.connect(weak=False, dispatch_uid="audit:task_postrun")
def _leave(task_id: str | None = None, **_: Any) -> None:  # noqa: ANN401
    stack = _stacks.pop(task_id or "", None)
    if stack is not None:
        stack.close()
