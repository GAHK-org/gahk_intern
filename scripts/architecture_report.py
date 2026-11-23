"""Compare the architecture checks of a pull request with its base (spec/architecture.md).

    python scripts/architecture_report.py <head dir> <base dir> [--fail-on any|new]

Each directory holds what .github/workflows/architecture.yml collected from one checkout:

    tach.json      uv run tach check --output json
    ruff.json      uv run ruff check --config ruff-architecture.toml --output-format json
    models.json    python manage.py check_architecture --json
    runtime.json   the test suite's findings (ARCHITECTURE_REPORT, tests/architecture_guard.py)

Prints a Markdown report: totals per check, then what this PR adds and removes. Findings are
compared without line numbers, so an unrelated edit above an old violation does not make it "new".
A missing or unreadable file on the base side means the base predates that check, and everything
counts as new; on the head side it means the check itself broke, which fails the report.

Exits 1 when the head has any finding (`--fail-on any`, today's setting: the check is reported but
not required) or only when it has new ones (`--fail-on new`, for when it becomes required).
"""

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

# (key, line shown in the report). The key leaves out line numbers on purpose.
Item = tuple[str, str]

CHECKS = {
    "tach.json": "Imports (Tach)",
    "ruff.json": "Raw SQL (ruff)",
    "models.json": "Relations and storage (check_architecture)",
    "runtime.json": "Runtime: writes and raw SQL seen by the tests",
}
SHOWN = 150


def _tach(rows: list[dict[str, Any]]) -> list[Item]:
    items = []
    for row in rows:
        located = row.get("Located") or {}
        details = located.get("details") or row.get("Global", {}).get("details") or {}
        code = details.get("Code") or details.get("Configuration") or details
        kind, fields = (
            next(iter(code.items()))
            if isinstance(code, dict) and code
            else (str(code), {})
        )
        fields = fields if isinstance(fields, dict) else {}
        dependency = fields.get("dependency", "")
        user = fields.get("usage_module", "")
        if kind == "PrivateDependency":
            text = f"`{user}` imports `{dependency}`, which is not part of the `{fields.get('definition_module')}` api"
        elif kind == "UndeclaredDependency":
            text = f"`{user}` imports `{dependency}`, but `{fields.get('definition_module')}` is not in its depends_on"
        elif kind == "LayerViolation":
            text = (
                f"`{user}` ({fields.get('usage_layer')}) imports `{dependency}` from a higher layer "
                f"({fields.get('definition_layer')})"
            )
        else:
            text = f"{kind}: {json.dumps(fields, sort_keys=True)}"
        path = located.get("file_path", "")
        key = f"{path}|{kind}|{json.dumps(fields, sort_keys=True)}"
        items.append((key, f"{path}:{located.get('line_number', '?')} {text}"))
    return items


def _relative(filename: str) -> str:
    match = re.search(r"(?:^|/)(app/.*)$", filename)
    return match.group(1) if match else filename


def _ruff(rows: list[dict[str, Any]]) -> list[Item]:
    items = []
    for row in rows:
        path = _relative(row.get("filename", ""))
        message = f"{row.get('code')} {row.get('message')}"
        items.append(
            (
                f"{path}|{message}",
                f"{path}:{row.get('location', {}).get('row', '?')} {message}",
            )
        )
    return items


def _models(rows: list[dict[str, Any]]) -> list[Item]:
    return [
        (f"{row['check']}|{row['subject']}", f"[{row['check']}] {row['message']}")
        for row in rows
    ]


def _runtime(rows: list[dict[str, Any]]) -> list[Item]:
    return [
        (
            f"{row['check']}|{row['app']}|{row['subject']}|{row['where']}",
            f"[{row['check']}] {row['message']} in `{row['where']}` (first seen in `{row['first_test']}`)",
        )
        for row in rows
    ]


PARSERS: dict[str, Callable[[list[dict[str, Any]]], list[Item]]] = {
    "tach.json": _tach,
    "ruff.json": _ruff,
    "models.json": _models,
    "runtime.json": _runtime,
}


def load(directory: Path, name: str) -> list[Item] | None:
    """The findings in one file, or None when it is missing or not a JSON list."""
    try:
        rows = json.loads((directory / name).read_text())
    except (OSError, ValueError):
        return None
    return PARSERS[name](rows) if isinstance(rows, list) else None


def _listing(title: str, lines: list[str]) -> list[str]:
    if not lines:
        return []
    out = [f"<details><summary>{title} ({len(lines)})</summary>", "", "```"]
    out += sorted(lines)[:SHOWN]
    if len(lines) > SHOWN:
        out.append(f"… and {len(lines) - SHOWN} more")
    return [*out, "```", "", "</details>", ""]


def report(head: Path, base: Path) -> tuple[str, int, int, list[str]]:
    """The Markdown report, the head's total, the number of new findings, and broken checks."""
    table = ["| Check | This PR | Base | New | Fixed |", "|---|---:|---:|---:|---:|"]
    sections: list[str] = []
    broken: list[str] = []
    no_base: list[str] = []
    total = new_total = 0
    for name, title in CHECKS.items():
        head_items = load(head, name)
        base_items = load(base, name)
        if base_items is None:
            no_base.append(title)
        if head_items is None:
            broken.append(title)
            table.append(f"| {title} | did not run | | | |")
            continue
        head_count = Counter(key for key, _ in head_items)
        base_count = Counter(key for key, _ in base_items or [])
        added = head_count - base_count
        removed = base_count - head_count
        shown = {key: line for key, line in [*(base_items or []), *head_items]}
        new_lines = [shown[key] for key, count in added.items() for _ in range(count)]
        fixed_lines = [
            shown[key] for key, count in removed.items() for _ in range(count)
        ]
        total += len(head_items)
        new_total += len(new_lines)
        base_cell = "n/a" if base_items is None else str(len(base_items))
        table.append(
            f"| {title} | {len(head_items)} | {base_cell} | {len(new_lines)} | {len(fixed_lines)} |"
        )
        sections += _listing(f"{title}: new in this PR", new_lines)
        sections += _listing(f"{title}: fixed in this PR", fixed_lines)
        if base_items is not None:  # without a base, "new" already lists everything
            sections += _listing(
                f"{title}: everything", [line for _, line in head_items]
            )
    lines = [
        "## Architecture",
        "",
        (
            "Code that crosses an app boundary (spec/architecture.md). Not a required check yet: "
            "the totals are expected to be high, but please do not add new findings."
        ),
        "",
        *table,
        "",
    ]
    if no_base:
        lines += [
            (
                f"The base has no results for {', '.join(no_base)}: it predates that check, "
                "so all its findings count as new."
            ),
            "",
        ]
    if new_total:
        lines += [
            f"**This PR adds {new_total} finding(s).** Each is listed under *new in this PR*.",
            "",
        ]
    if broken:
        lines += [
            f"**Broken:** {', '.join(broken)} produced no readable output on this PR.",
            "",
        ]
    return "\n".join(lines + sections), total, new_total, broken


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("head", type=Path)
    parser.add_argument("base", type=Path)
    parser.add_argument("--fail-on", choices=["any", "new"], default="any")
    args = parser.parse_args()
    markdown, total, new_total, broken = report(args.head, args.base)
    print(markdown)
    if broken:
        return 1
    return 1 if (total if args.fail_on == "any" else new_total) else 0


if __name__ == "__main__":
    sys.exit(main())
