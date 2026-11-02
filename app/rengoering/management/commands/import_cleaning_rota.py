"""Import a printed rota from a file — the same parser and name matching as the import page.

    manage.py import_cleaning_rota "Rengøringslister E26.pdf" --name E26 --dry-run
    manage.py import_cleaning_rota liste.txt --name E26 --map "William P=william@example.dk" --map "Kim="

A PDF goes through `pdftotext -layout`; anything else is read as text/CSV. Nothing is saved while a
name is unresolved: the command lists them, and --map settles each one by email (or empty for
"Ingen på listen"). The real E26 list has real names and is deliberately not in the repository.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError, CommandParser

from rengoering import importing, services
from rengoering.models import CleaningArea, Rota
from residents.models import Resident


class Command(BaseCommand):
    help = "Import a søndagsrengøring rota from a PDF/text/CSV file (names must all resolve)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("file")
        parser.add_argument("--name", required=True, help='The rota\'s name, e.g. "E26".')
        parser.add_argument(
            "--map",
            action="append",
            default=[],
            metavar="NAVN=EMAIL",
            help='Settle a name by hand, e.g. "William P=william@example.dk"; "Kim=" means nobody.',
        )
        parser.add_argument("--dry-run", action="store_true", help="Parse and match, but save nothing.")

    def handle(self, *args, **opts) -> None:  # noqa: ANN002, ANN003
        path = Path(opts["file"])
        data = path.read_bytes()
        text = importing.pdf_to_text(data) if path.suffix.lower() == ".pdf" else data.decode("utf-8-sig")
        parsed = importing.parse(text)
        for error in parsed.errors:
            self.stderr.write(f"  {error}")

        areas = list(CleaningArea.objects.filter(is_active=True, kind=CleaningArea.Kind.SOENDAG))
        if not areas:
            raise CommandError("Ingen rengøringsområder. Kør load_cleaning_areas først.")
        by_header = importing.match_areas([r.header for r in parsed.rows], areas)
        skipped = [h for h, a in by_header.items() if a is None]
        if skipped:
            self.stdout.write(f"Springer over: {', '.join(skipped)}")

        people = list(services.rota_residents())
        manual = self._manual(opts["map"])
        resolved: dict[str, Resident | None] = {}
        unresolved = []
        for row in parsed.rows:
            if not row.name or row.name in resolved or not by_header[row.header]:
                continue
            if row.name in manual:
                resolved[row.name] = manual[row.name]
                continue
            match = importing.match_name(row.name, people)
            if match.resident:
                resolved[row.name] = match.resident
            else:
                options = (
                    ", ".join(f"{p.full_name} <{p.email}>" for p in match.candidates) or "ingen kandidater"
                )
                unresolved.append(f"  {row.name!r}: {options}")
        for name, who in resolved.items():
            self.stdout.write(f"  {name} → {who.full_name if who else 'Ingen'}")

        if unresolved:
            self.stderr.write("Navne der skal afgøres med --map:\n" + "\n".join(unresolved))
            if not opts["dry_run"]:
                raise CommandError(f"{len(unresolved)} navne er ikke afgjort; intet er gemt.")
        planned = importing.build_assignments(parsed.rows, by_header, resolved)
        if opts["dry_run"]:
            self.stdout.write(self.style.SUCCESS(f"Tørløb: {len(planned)} vagter ville blive oprettet."))
            return
        if Rota.objects.filter(name__iexact=opts["name"]).exists():
            raise CommandError(f"Der findes allerede en liste ved navn {opts['name']}.")
        try:
            rota = services.save_rota(opts["name"], planned, None)
        except services.RotaClash as clash:
            raise CommandError(f"Søndage der allerede er på en anden liste: {clash}") from clash
        self.stdout.write(self.style.SUCCESS(f"{rota.name}: {len(planned)} vagter importeret."))

    def _manual(self, pairs: list[str]) -> dict[str, Resident | None]:
        out: dict[str, Resident | None] = {}
        for pair in pairs:
            name, sep, email = pair.partition("=")
            if not sep:
                raise CommandError(f"--map skal være NAVN=EMAIL, ikke {pair!r}.")
            if not email.strip():
                out[name.strip()] = None
                continue
            try:
                out[name.strip()] = Resident.objects.get_by_natural_key(email.strip())
            except Resident.DoesNotExist:
                raise CommandError(f"Ingen beboer med email {email!r}.") from None
        return out
