"""The runtime half of the architecture checks (spec/architecture.md): what the tests actually do.

Tach sees imports, `manage.py check_architecture` sees model declarations. Neither sees `.save()` on
an instance some other app handed you, a write through a reverse relation, `apps.get_model(...)`,
`.raw()` or a storage name built at runtime. This plugin watches every SQL statement and every
storage save/delete while the suite runs, and reports:

  db-write       an INSERT/UPDATE/DELETE on another app's table
  storage-write  a save/delete under another app's storage prefix, or under none at all
  raw-sql        SQL that did not come from the ORM, outside ruff-architecture.toml's allow-list

Who did it is read off the call stack: the innermost frame of a project app, with two kinds of
frame skipped because they act on behalf of their caller — the kernel (`core`) and any app's
`models` module (a `save()` override runs for whoever called it). Test code, demo seeds and
migrations are exempt. A cascade issued by Django's deletion collector is judged by the model whose
`delete()` started it: the app that declared `on_delete` agreed to it.

Reporting only, by default: findings are listed at the end of the run and, with
ARCHITECTURE_REPORT=<path>, written as JSON for CI (.github/workflows/architecture.yml). With
ARCHITECTURE_STRICT=1 a test that makes a new finding fails. Never loaded outside pytest, so
production pays nothing for it; it only sees the paths the tests run.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path
from types import FrameType
from typing import Any, BinaryIO

import pytest
from django.conf import settings
from django.core.files.storage import FileSystemStorage, Storage
from django.db import connections

from core import architecture

STRICT = os.environ.get("ARCHITECTURE_STRICT") == "1"
REPORT = os.environ.get("ARCHITECTURE_REPORT", "")
SUMMARY_LINES = 40

_WRITE = re.compile(r'^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+"?(\w+)"?', re.IGNORECASE)
# Code that sets up data rather than being the app under test.
_FIXTURE_CODE = re.compile(
    r"^(tests(\..*)?|conftest|\w+\.migrations(\..*)?|\w+\.demo|core\.management\.commands\.seed_demo)$"
)


@dataclass(frozen=True, order=True)
class RuntimeFinding:
    check: str  # "db-write" | "storage-write" | "raw-sql"
    app: str  # the app whose code did it
    subject: str  # the table, the storage prefix, or the code that ran raw SQL
    where: str  # module.function the finding is attributed to
    message: str


_found: Counter[RuntimeFinding] = Counter()
_first_test: dict[RuntimeFinding, str] = {}
_this_test: list[RuntimeFinding] = []
_current_test = ""


def _module(frame: FrameType) -> str:
    return str(frame.f_globals.get("__name__", ""))


def _where(frame: FrameType) -> str:
    return f"{_module(frame)}.{frame.f_code.co_qualname}"


def _writer(frame: FrameType | None, owner: str | None) -> tuple[str, FrameType] | None:
    """The app a write is attributed to, with its frame, when that app is not `owner`; None when
    the write is allowed. See the module docstring for which frames are skipped."""
    while frame is not None:
        module = _module(frame)
        if _FIXTURE_CODE.match(module):
            return None
        app = architecture.app_of_module(module)
        if app is not None and not (module == f"{app}.models" or module.startswith(f"{app}.models.")):
            if app == owner:
                return None
            if app != architecture.KERNEL:
                return app, frame
        frame = frame.f_back
    return None


def _cascade_origin(frame: FrameType | None) -> str | None:
    """When only Django lies between this write and a `delete()` further out, the app that owns the
    model being deleted: the write is that delete's cascade (or its SET_NULL)."""
    while frame is not None:
        module = _module(frame)
        if not module.startswith("django."):
            return None
        if frame.f_code.co_name == "delete" and module in ("django.db.models.query", "django.db.models.base"):
            target = frame.f_locals.get("self")
            model = getattr(target, "model", None) if module == "django.db.models.query" else type(target)
            return architecture.app_of_model(model) if isinstance(model, type) else None
        frame = frame.f_back
    return None


@cache
def _raw_sql_allowed() -> frozenset[str]:
    """Modules allowed raw SQL: the per-file-ignores of ruff-architecture.toml, as module names."""
    config = tomllib.loads((Path(settings.BASE_DIR) / "ruff-architecture.toml").read_text())
    ignores = config["lint"]["per-file-ignores"]
    return frozenset(
        path.removesuffix(".py").replace("/", ".")
        for path, codes in ignores.items()
        if path.endswith(".py") and "TID251" in codes
    )


def _record(check: str, app: str, subject: str, frame: FrameType, message: str) -> None:
    finding = RuntimeFinding(check, app, subject, _where(frame), message)
    _found[finding] += 1
    _first_test.setdefault(finding, _current_test)
    _this_test.append(finding)


def _check_query(sql: str, frame: FrameType | None) -> None:
    if (match := _WRITE.match(sql)) and (owner := architecture.table_owners().get(match.group(1))):
        if hit := _writer(frame, _cascade_origin(frame) or owner):
            app, at = hit
            table = match.group(1)
            _record("db-write", app, table, at, f"{app} writes to {table}, which {owner} owns")

    # Raw SQL: the code that called cursor.execute() is not Django's.
    caller = frame
    while caller is not None and _module(caller).startswith("django.db.backends"):
        caller = caller.f_back
    if caller is None:
        return
    module = _module(caller)
    if module == "django.db.models.sql.query" and caller.f_code.co_name == "_execute_query":
        # Model.objects.raw(): Django's RawQuery ran it, on behalf of the first project code out.
        while caller is not None and architecture.app_of_module(module := _module(caller)) is None:
            if _FIXTURE_CODE.match(module):
                return
            caller = caller.f_back
        if caller is None:
            return
    elif module.startswith("django."):
        return
    app = architecture.app_of_module(module)
    if app is None or _FIXTURE_CODE.match(module) or module in _raw_sql_allowed():
        return
    _record("raw-sql", app, _where(caller), caller, f"{app} runs raw SQL outside the kernel's allow-list")


# Django's execute_wrapper protocol: params and the result are whatever the database driver takes.
def _on_query(
    execute: Callable[..., Any],
    sql: str,
    params: Any,  # noqa: ANN401
    many: bool,
    context: dict[str, Any],
) -> Any:  # noqa: ANN401
    _check_query(str(sql), sys._getframe(1))
    return execute(sql, params, many, context)


def _check_storage(name: str, frame: FrameType | None, *, saving: bool) -> None:
    """A save or delete under another app's prefix; and a save under no prefix at all (a delete of
    such a name is left alone: tests give files bare names like "a.jpg")."""
    owner = architecture.owner_of_storage_name(name)
    if owner is None and not saving:
        return
    if hit := _writer(frame, owner):
        app, at = hit
        if owner is None:
            folder = name.lstrip("/").partition("/")[0] + "/"
            _record("storage-write", app, folder, at, f"{app} writes {folder}, outside every storage prefix")
        else:
            prefix = max(
                (p for p in architecture.storage_prefixes() if name.lstrip("/").startswith(p)), key=len
            )
            _record("storage-write", app, prefix, at, f"{app} writes under {prefix}, which {owner} owns")


@pytest.fixture(scope="session", autouse=True)
def _guard_storage() -> Iterator[None]:
    """Every storage the suite uses is a FileSystemStorage (tests/conftest.py), so wrapping
    Storage.save and FileSystemStorage.delete sees every file written or removed."""
    save = Storage.save
    delete = FileSystemStorage.delete

    def guarded_save(
        self: Storage, name: str | None, content: BinaryIO, max_length: int | None = None
    ) -> str:
        _check_storage(name or str(getattr(content, "name", "") or ""), sys._getframe(1), saving=True)
        return save(self, name, content, max_length)

    def guarded_delete(self: FileSystemStorage, name: str) -> None:
        _check_storage(name, sys._getframe(1), saving=False)
        delete(self, name)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Storage, "save", guarded_save)
        patch.setattr(FileSystemStorage, "delete", guarded_delete)
        yield


@pytest.fixture(autouse=True)
def _guard_database(request: pytest.FixtureRequest) -> Iterator[None]:
    global _current_test  # noqa: PLW0603 — one test at a time; the summary names where each began
    _current_test = request.node.nodeid
    _this_test.clear()
    with ExitStack() as stack:
        for alias in connections:
            stack.enter_context(connections[alias].execute_wrapper(_on_query))
        yield
    if STRICT and _this_test:
        found = "\n".join(sorted({finding.message for finding in _this_test}))
        pytest.fail(f"Crosses an app boundary (spec/architecture.md):\n{found}")


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if not _found:
        return
    terminalreporter.section("architecture")
    findings = sorted(_found)
    for finding in findings[:SUMMARY_LINES]:
        terminalreporter.write_line(f"[{finding.check}] {finding.message}  ({finding.where})")
    if len(findings) > SUMMARY_LINES:
        terminalreporter.write_line(
            f"… and {len(findings) - SUMMARY_LINES} more (ARCHITECTURE_REPORT=<file> lists all)"
        )
    terminalreporter.write_line(
        f"{len(findings)} distinct finding(s) crossing app boundaries; see spec/architecture.md."
        + ("" if STRICT else " Reported only; ARCHITECTURE_STRICT=1 fails the tests that make them.")
    )


def pytest_sessionfinish(session: pytest.Session) -> None:
    if not REPORT:
        return
    rows = [
        {**asdict(finding), "count": count, "first_test": _first_test[finding]}
        for finding, count in sorted(_found.items())
    ]
    Path(REPORT).write_text(json.dumps(rows, indent=1, ensure_ascii=False))
