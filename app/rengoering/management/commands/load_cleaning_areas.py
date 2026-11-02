"""Seed the søndagsrengøring areas and task lists (rengoering/areas_data.py). Idempotent: areas that
already exist are left as Inspektionen has edited them, unless --reset."""

from django.core.management.base import BaseCommand, CommandParser

from rengoering.services import load_areas


class Command(BaseCommand):
    help = "Create the søndagsrengøring areas and their task lists (existing areas are kept)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Replace the task list of existing areas with the seed's (old tasks are deactivated).",
        )

    def handle(self, *args, **opts) -> None:  # noqa: ANN002, ANN003
        areas, tasks = load_areas(reset=opts["reset"])
        self.stdout.write(self.style.SUCCESS(f"Rengøring: {areas} områder og {tasks} opgaver oprettet."))
