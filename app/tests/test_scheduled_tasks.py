import importlib
from collections.abc import Callable
from typing import Any

import pytest


@pytest.mark.parametrize(
    ("module_name", "task_name", "command_name"),
    [
        ("admissions.tasks", "purge_expired_applications", "purge_applications"),
        ("ak.tasks", "apply_monthly_assessment", "ak_monthly_assessment"),
        ("opslagstavle.tasks", "purge_orphaned_images", "purge_notices"),
        ("reparationer.tasks", "archive_finished_repairs", "archive_finished_repairs"),
        ("events.tasks", "purge_expired_events", "purge_events"),
        ("events.tasks", "remind_rsvp_deadlines", "remind_rsvp_deadlines"),
        ("photo_album.tasks", "purge_expired_media", "purge_photo_album"),
        ("koekken.tasks", "generate_koekkenvagter", "generate_koekkenvagter"),
        ("koekken.tasks", "post_koekken_obligation", "post_koekken_obligation"),
        ("koekken.tasks", "roll_forward_koekkenvagter", "roll_forward_koekkenvagter"),
        ("koekken.tasks", "reconcile_koekkenvagter", "reconcile_koekkenvagter"),
    ],
)
def test_scheduled_task_runs_its_management_command(
    module_name: str,
    task_name: str,
    command_name: str,
    monkeypatch: pytest.MonkeyPatch,
    settings: object,
) -> None:
    # koekken's two tasks are gated closed by default (KOEKKEN_JOBS_ENABLED, see
    # config/settings.py and koekken/tasks.py) — irrelevant to every other task here, but this test
    # is specifically checking "the task calls its management command", so open the gate for it.
    # The gate's closed-by-default behaviour has its own tests below.
    settings.KOEKKEN_JOBS_ENABLED = True  # type: ignore[attr-defined]

    module = importlib.import_module(module_name)
    commands: list[str] = []
    monkeypatch.setattr(module, "call_command", commands.append)

    task: Callable[[], Any] = getattr(module, task_name).run
    task()

    assert commands == [command_name]


@pytest.mark.parametrize(
    ("task_name", "command_name"),
    [
        ("generate_koekkenvagter", "generate_koekkenvagter"),
        ("post_koekken_obligation", "post_koekken_obligation"),
        ("roll_forward_koekkenvagter", "roll_forward_koekkenvagter"),
        ("reconcile_koekkenvagter", "reconcile_koekkenvagter"),
    ],
)
def test_koekken_scheduled_tasks_noop_when_gate_closed(
    task_name: str, command_name: str, monkeypatch: pytest.MonkeyPatch, settings: object
) -> None:
    """FIX 2: deploying this branch must not silently start real monthly debt accrual. All four
    koekken jobs must no-op — never call their management command — while KOEKKEN_JOBS_ENABLED is
    at its default (False). (roll_forward_koekkenvagter and reconcile_koekkenvagter never themselves
    post debt -- they only write VagtTildeling rows -- but Amendment 1/2/3 gate them the same way
    anyway, for consistency with the other two while the feature is staged closed.)"""
    import koekken.tasks as module

    settings.KOEKKEN_JOBS_ENABLED = False  # type: ignore[attr-defined]
    commands: list[str] = []
    monkeypatch.setattr(module, "call_command", commands.append)

    task: Callable[[], Any] = getattr(module, task_name).run
    task()

    assert commands == []


@pytest.mark.parametrize(
    ("task_name", "command_name"),
    [
        ("generate_koekkenvagter", "generate_koekkenvagter"),
        ("post_koekken_obligation", "post_koekken_obligation"),
        ("roll_forward_koekkenvagter", "roll_forward_koekkenvagter"),
        ("reconcile_koekkenvagter", "reconcile_koekkenvagter"),
    ],
)
def test_koekken_scheduled_tasks_run_command_when_gate_open(
    task_name: str, command_name: str, monkeypatch: pytest.MonkeyPatch, settings: object
) -> None:
    """The flip side of the no-op test above: once KOEKKEN_JOBS_ENABLED is explicitly opened, the
    task must actually call its management command, exactly like every other scheduled task."""
    import koekken.tasks as module

    settings.KOEKKEN_JOBS_ENABLED = True  # type: ignore[attr-defined]
    commands: list[str] = []
    monkeypatch.setattr(module, "call_command", commands.append)

    task: Callable[[], Any] = getattr(module, task_name).run
    task()

    assert commands == [command_name]
