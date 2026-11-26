"""The architecture checks themselves (spec/architecture.md): who a write is attributed to, what
the static checks flag, and how the CI report compares a PR with its base.

These test the tooling, not the codebase: nothing here asserts that today's code is free of
findings, since the checks are reported, not required.
"""

import importlib.util
import json
import textwrap
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from core import architecture
from residents.models import Resident
from tests import architecture_guard
from tests.architecture_guard import RuntimeFinding

REPO = Path(__file__).resolve().parents[2]


def _code_in(module: str, source: str) -> Callable[..., Any]:
    """The function `probe` from `source`, running as if it were code in `module`."""
    namespace: dict[str, Any] = {"__name__": module}
    exec(compile(textwrap.dedent(source), f"<{module}>", "exec"), namespace)
    return namespace["probe"]


UPDATE_RESIDENT = """
def probe(resident_id):
    from residents.models import Resident
    Resident.objects.filter(pk=resident_id).update(first_name="Changed")
"""
CALL = """
def probe(helper, *args):
    return helper(*args)
"""


@pytest.fixture
def findings(monkeypatch: pytest.MonkeyPatch) -> Counter[RuntimeFinding]:
    """The guard's findings during this test only, kept out of the run's own report."""
    found: Counter[RuntimeFinding] = Counter()
    monkeypatch.setattr(architecture_guard, "_found", found)
    monkeypatch.setattr(architecture_guard, "_first_test", {})
    monkeypatch.setattr(architecture_guard, "_this_test", [])
    return found


def _seen(found: Counter[RuntimeFinding]) -> set[tuple[str, str, str]]:
    return {(finding.check, finding.app, finding.subject) for finding in found}


@pytest.fixture
def resident(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="architecture@gahk.dk")


def test_a_write_to_another_apps_table_is_reported(findings: Counter, resident: Resident) -> None:
    _code_in("rooms.probe", UPDATE_RESIDENT)(resident.pk)

    assert _seen(findings) == {("db-write", "rooms", "residents_resident")}
    (finding,) = findings
    assert finding.where == "rooms.probe.probe"
    assert finding.message == "rooms writes to residents_resident, which residents owns"


def test_the_owner_writes_its_own_tables(findings: Counter, resident: Resident) -> None:
    _code_in("residents.probe", UPDATE_RESIDENT)(resident.pk)

    assert findings == Counter()


def test_kernel_helpers_count_as_their_caller(findings: Counter, resident: Resident) -> None:
    helper = _code_in("core.probe", UPDATE_RESIDENT)

    helper(resident.pk)  # called by the test itself: nobody to blame
    assert findings == Counter()

    _code_in("rooms.probe", CALL)(helper, resident.pk)
    assert _seen(findings) == {("db-write", "rooms", "residents_resident")}


def test_a_models_module_does_not_hide_its_caller(findings: Counter, resident: Resident) -> None:
    """A `save()` override in residents.models runs on behalf of whoever called it."""
    in_models = _code_in("residents.models.probe", UPDATE_RESIDENT)

    _code_in("residents.probe", CALL)(in_models, resident.pk)
    assert findings == Counter()

    _code_in("rooms.probe", CALL)(in_models, resident.pk)
    assert _seen(findings) == {("db-write", "rooms", "residents_resident")}


def test_a_cascade_is_judged_by_the_model_being_deleted(findings: Counter, make_resident: Callable) -> None:
    delete = """
    def probe(resident_id):
        from residents.models import Resident
        Resident.objects.get(pk=resident_id).delete()
    """
    # Deleting a resident also deletes (or nulls) rows in every app with a relation to residents.
    _code_in("residents.probe", delete)(make_resident(email="one@gahk.dk").pk)
    assert findings == Counter()

    _code_in("rooms.probe", delete)(make_resident(email="two@gahk.dk").pk)
    assert ("db-write", "rooms", "residents_resident") in _seen(findings)
    assert {finding.app for finding in findings} == {"rooms"}


def test_raw_sql_outside_the_allow_list_is_reported(findings: Counter, resident: Resident) -> None:
    cursor = """
    def probe():
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    """
    _code_in("core.health", cursor)()  # allow-listed in ruff-architecture.toml
    assert findings == Counter()

    _code_in("rooms.probe", cursor)()
    _code_in(
        "events.probe",
        """
        def probe():
            from residents.models import Resident
            return list(Resident.objects.raw("SELECT * FROM residents_resident"))
        """,
    )()
    assert _seen(findings) == {
        ("raw-sql", "rooms", "rooms.probe.probe"),
        ("raw-sql", "events", "events.probe.probe"),
    }


def test_storage_writes_outside_the_apps_own_prefixes_are_reported(findings: Counter) -> None:
    save = _code_in(
        "rooms.probe",
        """
        def probe(name):
            from django.core.files.base import ContentFile
            from django.core.files.storage import default_storage
            return default_storage.save(name, ContentFile(b"x"))
        """,
    )
    names = [save("roomimages/probe.txt"), save("opslag/probe.txt"), save("elsewhere/probe.txt")]

    assert _seen(findings) == {
        ("storage-write", "rooms", "opslag/"),
        ("storage-write", "rooms", "elsewhere/"),
    }
    for name in names:
        default_storage.delete(name)


def test_a_delete_under_another_apps_prefix_is_reported(findings: Counter) -> None:
    name = default_storage.save("opslag/probe.txt", ContentFile(b"x"))

    _code_in("rooms.probe", CALL)(default_storage.delete, name)

    assert _seen(findings) == {("storage-write", "rooms", "opslag/")}


def test_a_relation_without_related_name_plus_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    architecture.project_apps()  # fill the cache before `apps` is swapped out below

    def relation(name: str, hidden: bool) -> SimpleNamespace:
        return SimpleNamespace(
            name=name, is_relation=True, related_model=Resident, remote_field=SimpleNamespace(hidden=hidden)
        )

    meta = SimpleNamespace(
        app_label="rooms",
        local_fields=[relation("tenant", hidden=False), relation("visitor", hidden=True)],
        local_many_to_many=[],
    )
    model = type("Probe", (), {"__module__": "rooms.models", "_meta": meta})
    monkeypatch.setattr(architecture, "apps", SimpleNamespace(get_models=lambda: [model]))

    assert [finding.subject for finding in architecture.relation_findings()] == ["rooms.Probe.tenant"]


def test_storage_prefixes_must_be_folders_and_must_not_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    declared = {"photo-album/": "photo_album", "photo-album/videos/": "gahkflix", "cms": "cms"}
    monkeypatch.setattr(architecture, "storage_prefixes", lambda: declared)

    assert {(finding.app, finding.subject) for finding in architecture._prefix_overlaps()} == {
        ("gahkflix", "photo-album/videos/"),
        ("cms", "cms"),
    }
    assert architecture.owner_of_storage_name("photo-album/videos/1.mp4") == "gahkflix"
    assert architecture.owner_of_storage_name("/photo-album/1.jpg") == "photo_album"
    assert architecture.owner_of_storage_name("elsewhere/1.jpg") is None


def _report_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "architecture_report", REPO / "scripts" / "architecture_report.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(directory: Path, name: str, rows: list[dict[str, Any]]) -> None:
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(json.dumps(rows))


def _ruff(path: str, row: int) -> dict[str, Any]:
    return {
        "code": "TID251",
        "message": "banned",
        "filename": f"/runner/work/x/{path}",
        "location": {"row": row},
    }


def test_the_report_compares_with_the_base_ignoring_line_numbers(tmp_path: Path) -> None:
    head, base = tmp_path / "head", tmp_path / "base"
    for side in (head, base):
        _write(side, "tach.json", [])
        _write(side, "models.json", [])
        _write(side, "runtime.json", [])
    # The old finding moved down two lines (still old); one is fixed; one is new.
    _write(base, "ruff.json", [_ruff("app/cms/tasks.py", 26), _ruff("app/cms/models.py", 19)])
    _write(head, "ruff.json", [_ruff("app/cms/tasks.py", 28), _ruff("app/events/views.py", 5)])

    markdown, total, new, broken = _report_module().report(head, base)

    assert (total, new, broken) == (2, 1, [])
    assert "| Raw SQL (ruff) | 2 | 2 | 1 | 1 |" in markdown
    assert "app/events/views.py:5 TID251 banned" in markdown


def test_the_report_treats_a_missing_base_as_all_new_and_a_missing_head_as_broken(tmp_path: Path) -> None:
    head, base = tmp_path / "head", tmp_path / "base"
    _write(head, "ruff.json", [_ruff("app/cms/tasks.py", 26)])
    _write(head, "tach.json", [])
    _write(head, "models.json", [])

    markdown, total, new, broken = _report_module().report(head, base)

    assert (total, new) == (1, 1)
    assert broken == ["Runtime: writes and raw SQL seen by the tests"]
    assert "| Raw SQL (ruff) | 1 | n/a | 1 | 0 |" in markdown
