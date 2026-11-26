"""Model-level architecture checks that an import checker cannot see (spec/architecture.md).

    python manage.py check_architecture          readable list, exit 1 if anything is found
    python manage.py check_architecture --json   the same as JSON, for CI's comparison with the base

A management command rather than a Django system check on purpose: a system-check error stops
`runserver` and `migrate`, and these rules are reported, not enforced, until the CI check is made
required. Needs settings but no database.
"""

import json
import sys
from dataclasses import asdict
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from core.architecture import relation_findings, storage_findings


class Command(BaseCommand):
    help = "Report relations and file storage that cross app boundaries."
    requires_system_checks: list[str] = []

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--json", action="store_true", help="Print the findings as JSON.")

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 — Django's signature
        findings = sorted([*relation_findings(), *storage_findings()])
        if options["json"]:
            self.stdout.write(json.dumps([asdict(finding) for finding in findings], indent=1))
        else:
            for finding in findings:
                self.stdout.write(f"[{finding.check}] {finding.message}")
            self.stdout.write(f"{len(findings)} finding(s).")
        if findings:
            sys.exit(1)
