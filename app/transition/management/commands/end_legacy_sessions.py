"""End every session the old code saved: `manage.py end_legacy_sessions [--dry-run]`. TEMPORARY.

Run in phase 2, right before gahk.dk's DNS moves to its webhost (DEPLOY.md, "Transition"). The old
code's session cookie is on Domain=.gahk.dk, so from the switch on every browser that still holds one
sends it to the webhost, which must never hold a key that works on intern. The new code marks every
session it saves (transition.middleware.MARKER); a session without the mark — or one that does not
decode — is the old code's, and is deleted. Users who reached intern.gahk.dk during phase 1 had
theirs carried over to a new, marked session already and stay logged in.

Idempotent: a second run finds nothing more to end.
"""

from importlib import import_module
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser

from transition.middleware import MARKER

BATCH = 1000


class Command(BaseCommand):
    help = "Delete every session not saved by this version of intern (the old code's sessions)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Count only; delete nothing.")

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401
        store = import_module(settings.SESSION_ENGINE).SessionStore
        model = store.get_model_class()
        decoder = store()
        legacy: list[str] = []
        kept = 0
        for key, data in model.objects.values_list("session_key", "session_data").iterator(chunk_size=BATCH):
            if decoder.decode(data).get(MARKER):
                kept += 1
            else:
                legacy.append(key)
        if options["dry_run"]:
            self.stdout.write(f"Dry run: would end {len(legacy)} old session(s) and keep {kept}.")
            return
        for start in range(0, len(legacy), BATCH):
            model.objects.filter(session_key__in=legacy[start : start + BATCH]).delete()
        self.stdout.write(self.style.SUCCESS(f"Ended {len(legacy)} old session(s); kept {kept}."))
