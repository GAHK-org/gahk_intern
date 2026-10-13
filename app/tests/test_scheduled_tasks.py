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
    ],
)
def test_scheduled_task_runs_its_management_command(
    module_name: str, task_name: str, command_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module(module_name)
    commands: list[str] = []
    monkeypatch.setattr(module, "call_command", commands.append)

    task: Callable[[], Any] = getattr(module, task_name).run
    task()

    assert commands == [command_name]
