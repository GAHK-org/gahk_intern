"""Declare a fridag -- Amendment 5 (A5.4, simplified by the 2026-10-04 supplement): a date (or a chosen
set of shift kinds on it) the kitchen does not need covering, e.g. juleaften. The normal case, not an
edge case: a periode is allocated ~122 days before it starts, so a fridag declared in the months
leading up to it is almost always against an already-generated/already-allocated month. This ONE
action creates the Fridag row(s), deletes the matching Vagt row(s) if that month was already generated
(the residents holding them are removed from those shifts and notified) and re-posts the month's
obligation if it already had obligation posted. Nothing is ever re-allocated: only that day's shifts
change. All in one atomic step. See koekken.services.declare_fridag for the full mechanics and its two
guards (no UDFOERT/ANMELDT assignment may be disturbed, no date in the past).

**Notification dispatch happens HERE, not inside the service function, and only when `not dry_run`**
(2026-10 review, F1): `declare_fridag` returns ready-to-send `(resident, audience, message)` tuples in
`FridagResult.notifications` instead of sending them itself, specifically so that a `--dry-run` --
which rolls its wrapping transaction back via `transaction.set_rollback(True)` AFTER the service
function has already returned -- can never have a real push already in flight by the time that
rollback happens. Sent inline (`background=False`), matching `events.services.
send_deadline_reminder`'s reasoning for anything reachable from a management command: a daemon thread
started from `handle()` is killed mid-send the moment the process exits.

No UI view exists for this yet; --dry-run is this action's preview, matching every other koekken
management command's own --dry-run convention: run it first to see exactly which residents would be
removed from which shifts before committing for real. The command is deliberately non-interactive.

**Recommended timing, per the stakeholder:** declare a periode's fridage BEFORE generating that
periode's shifts (i.e. before `generate_koekkenvagter` runs for it), rather than after generation or
in the middle of an already-allocated periode. Declared this early, there is nothing yet to delete or
re-post; `generate_vagter`'s own `is_fridag` seam (A5.3) simply never creates those rows in the first
place, which is the simpler of this command's two paths."""

import argparse
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.push import send
from koekken.models import VagtRegel
from koekken.services import FridagResult, KoekkenAllocationError, declare_fridag


class Command(BaseCommand):
    help = (
        "Erklær en fridag for en dato (eller udvalgte vagttyper på den) -- opret Fridag-række(r), "
        "slet evt. allerede genererede vagter og genbogfør måneden hvis den allerede havde "
        "forpligtelse bogført. Der genallokeres ikke. En fridag på en allerede allokeret dag fjerner "
        "de beboere, der var tildelt de berørte vagter, fra vagterne og sender dem besked. Kør "
        "gerne --dry-run først: den viser hvem der fjernes fra hvilke vagter uden at gennemføre "
        "noget eller sende besked. Anbefales erklæret før periodens vagter genereres -- så er der "
        "intet at slette eller genbogføre."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("date", type=str, help="YYYY-MM-DD")
        parser.add_argument(
            "--kind",
            action="append",
            dest="kinds",
            choices=[k.value for k in VagtRegel.Kind],
            default=None,
            help="Gentag for flere vagttyper. Udelades: hele dagen (alle vagttyper).",
        )
        parser.add_argument("--reason", type=str, default="", help="F.eks. 'Juleaften'.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        for_date = date.fromisoformat(str(opts["date"]))
        kinds_opt = opts["kinds"]
        kinds: list[str] = (
            list(kinds_opt) if isinstance(kinds_opt, list) else [k.value for k in VagtRegel.Kind]
        )
        reason = str(opts["reason"])
        dry_run = bool(opts["dry_run"])

        try:
            with transaction.atomic():
                result = declare_fridag(for_date, kinds, reason)
                if dry_run:
                    transaction.set_rollback(True)
        except KoekkenAllocationError as exc:
            raise CommandError(str(exc)) from exc

        # F1: dispatched only now that the transaction above is known to have actually committed --
        # never under --dry-run, which rolled it back instead.
        if not dry_run:
            for _resident, audience, message in result.notifications:
                send(audience, "Køkkenvagt", message, "/intern/koekken/", background=False)

        self._report(for_date, dry_run, result)

    def _report(self, for_date: date, dry_run: bool, result: FridagResult) -> None:
        """An actual preview, not bare counts -- on both --dry-run (so it is a useful preview) and a
        real run (as a record of what happened). The first line after the summary is the removal
        notice (supplement, point 3): every resident removed from a shift, by name and shift type.
        Its trailing sentence is chosen by `dry_run` so a dry run never claims a message was sent.
        Obligation re-posting is reported from its own field, `obligation_reposted_months`."""
        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}{for_date}: {len(result.created)} fridag-række(r) oprettet, "
                f"{result.deleted_vagter} vagt(er) slettet."
            )
        )
        if result.removed:
            who = ", ".join(
                f"{resident.full_name} ({vagt.get_kind_display().lower()})"
                for resident, vagt in result.removed
            )
            # Only claim a message when at least one removed resident has an active subscription;
            # the per-resident detail is in the subscription lines further down.
            any_reachable = any(audience.exists() for _r, audience, _m in result.notifications)
            if not any_reachable:
                trailing = "Ingen af de berørte har et aktivt notifikationsabonnement."
            elif dry_run:
                trailing = "De får besked, når kommandoen køres uden --dry-run."
            else:
                trailing = "De får besked."
            residents_count = len({resident.pk for resident, _vagt in result.removed})
            self.stdout.write(
                self.style.WARNING(
                    f"{prefix}{for_date} er allerede allokeret: {residents_count} beboer(e) "
                    f"fjernes fra deres vagter: {who}. {trailing}"
                )
            )
        else:
            self.stdout.write("  Ingen beboere var tildelt de berørte vagter.")
        if result.obligation_reposted_months:
            months = ", ".join(f"{y}-{m:02d}" for y, m in result.obligation_reposted_months)
            self.stdout.write(f"  {len(result.obligation_reposted_months)} måned(er) genbogført ({months}).")
        else:
            self.stdout.write("  Ingen måneder genbogført.")
        # Notification may not reach everyone: `core.push.send` silently does nothing for a resident
        # with no (eligible) push subscription. Only claim "notificeret" for residents whose audience
        # is genuinely non-empty; list the rest separately so the report never overclaims.
        reachable = [r for r, audience, _message in result.notifications if audience.exists()]
        unreachable = [r for r, audience, _message in result.notifications if not audience.exists()]
        if reachable:
            names = ", ".join(r.full_name for r in reachable)
            self.stdout.write(
                f"  {len(reachable)} beboer(e) {'ville være' if dry_run else 'er'} "
                f"blevet notificeret: {names}."
            )
        if unreachable:
            names = ", ".join(r.full_name for r in unreachable)
            self.stdout.write(
                f"  {len(unreachable)} beboer(e) berørt men uden notifikationsabonnement "
                f"(ingen besked sendt): {names}."
            )
